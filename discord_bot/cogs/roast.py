# path: discord_bot/cogs/roast.py

"""
Auto-triggered roast battles — no slash command starts this, the bot
proposes it on its own.

Two triggers, both checked by one poller tick (`_poller`, every
POLL_INTERVAL_SECONDS):
  1. Inactivity: guild has had no messages for >= its configured
     inactivity_minutes (default 60, see discord_roast_config).
  2. Random chance: even while active, every random_check_minutes the bot
     rolls random_chance_percent odds to propose one anyway — this is the
     "randomly" behavior from the spec, independent of inactivity.
Either firing sends a DM to every member with Administrator permission in
that guild, with a target picker (dropdown of guild members) and a channel
picker (dropdown of text channels). Whoever picks a target+channel first
locks the challenge — the DM is edited to reflect that in every admin's
inbox so there's no race where two admins both send challenges.

Flow after an admin picks target+channel:
  1. Row created in discord_roast_battles, status='pending', expires_at =
     now + CHALLENGE_EXPIRY_MINUTES.
  2. Bot DMs the target with an Accept/Decline view.
  3a. No response within 30 min -> _poller expires it, bot auto-wins,
      posts an announcement in the chosen channel, status='expired'.
  3b. Target declines -> status='ended', quiet cancel, no announcement
      (declining isn't a loss condition, just a no).
  3c. Target accepts -> status='active', bot posts the first roast in the
      channel with a RoastBattleView attached (Join Roast / Quit Roast).
  4. Every subsequent target message in that channel while status='active'
     gets a comeback roast from the bot (channel-scoped listener,
     `on_message`). Nothing else in the codebase currently listens for
     replies inside a specific active row like this, so state lives in
     `self._active_by_channel: dict[channel_id, battle_id]` for O(1)
     lookup on every message instead of a query per message.
  5. Joining: anyone who REPLIES to one of the bot's roast messages in the
     channel gets auto-pulled into joined_ids and roasted back — no
     button needed, lower friction than hunting for a "Join Roast" click.
  6. Quit Roast: pressable by the target, anyone in joined_ids, or any
     Administrator. Sets status='ended', disables the view, removes the
     channel from the active map.

Punchlines: PUNCHLINE_BANK below seeds both the AI system prompt (a few
random examples per call, so the model's roast style matches what was
supplied) and doubles as the offline fallback pool if GROQ_API_KEY is
unset or the API call fails.
"""

import asyncio
import logging
import os
import random
import re
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord.ext import commands, tasks

from config import DISCORD_CLONE_ADMIN_IDS
from database import db
from discord_bot.cogs._dm_support import GuildOnlyCog

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
ROAST_MODEL = "llama-3.1-70b-versatile"

POLL_INTERVAL_SECONDS = 60
# Minimum time between admin roast-suggestion DMs, regardless of trigger.
PROPOSAL_COOLDOWN_MINUTES = 60 * 24 * 2  # 2 days
# "Remind me later" snooze — shorter than the normal cooldown so the admin
# actually gets asked again soon instead of waiting out the full 2 days.
SNOOZE_MINUTES = 60 * 6  # 6 hours
CHALLENGE_EXPIRY_MINUTES = 30
DEFAULT_INACTIVITY_MINUTES = 60
DEFAULT_RANDOM_CHECK_MINUTES = 30
DEFAULT_RANDOM_CHANCE_PERCENT = 10
BOT_CONCEDE_CHANCE_PERCENT = 5  # odds the bot "takes the L" instead of roasting back

PUNCHLINE_BANK = [
    "You move through life like autocorrect — confident and always wrong.",
    "You're the reason we have warning labels on shampoo.",
    "Two billion years of evolution, and you turned out like this.",
    "Were you born this way, or did you take lessons?",
    "Your confidence and your results have never met.",
    "You're as useless as the \"g\" in lasagna.",
    "You bring joy... mostly to comedians.",
    "You are the human equivalent of a participation award.",
    "I'd take a bullet for you. From a water gun.",
    "You treat your own advice like terms and conditions — for others only.",
    "You built like a windshield wiper.",
    "You're the type of person to respond to spam emails.",
    "You're not stupid — you just have bad luck with thinking.",
    "I'll never forget the first time we met, but I'll keep trying.",
    "Every time I think you can't get any dumber, you prove me wrong.",
    "Our friendship is all about balance. You start talking... I stop listening.",
    "If laziness were a competition, you'd come in second because you'd be too lazy to compete.",
    "Do you exist to annoy people?",
    "If I give you a dollar, will you leave?",
    "You skipped the \"being normal\" gene.",
    "Congratulations on getting your PhD in annoyance.",
    "Let's play a game. For the rest of the week, don't talk to me.",
    "You're like a cloud. When you disappear, it's a beautiful day.",
    "Don't you ever get exhausted from talking about yourself all the time?",
    "Shock me. Say something intelligent.",
    "I'm not insulting you. I'm describing you.",
    "If you had two brains, you'd be twice as stupid.",
    "Remember when I asked for your opinion? Me either.",
    "Whoever told you to be yourself gave you really bad advice.",
    "You have your entire life to be a jerk. Why not take today off?",
    "I would say you're dumb as a rock, but at least a rock can hold the door open.",

    # --- added from roast_lines.zip (harder pack) ---
    "I'd roast you, but my mom said I'm not allowed to burn trash.",
    "You're not stupid — you just have bad luck thinking.",
    "I'd agree with you, but then we'd both be wrong.",
    "You're like a cloud — when you disappear, it's a beautiful day.",
    "I've met some sour people, but you're basically a lemon with Wi-Fi.",
    "Don't worry, I'm sure your personality will improve. Eventually. Maybe.",
    "You're proof that even evolution takes a day off.",
    "I'd call you a joke, but jokes actually have a point.",
    "You have the energy of a low-battery notification.",
    "Talking to you is like reading terms and conditions — pointless, confusing, and nobody wants to.",
    "You're my best friend, which is why I feel comfortable telling you — you're a disaster.",
    "We've been friends so long, I feel responsible for your terrible decisions.",
    "I love you like a brother, which is why I roast you like one.",
    "You're the kind of friend people warn you about in fairy tales.",
    "Being your friend is basically community service.",
    "You're not a bad person — you're just a great example of what not to do.",
    "I'd take a bullet for you. Mostly to end the conversation.",
    "You've grown so much… in the wrong direction.",
    "Your hairline is so far back, it's still loading.",
    "Your fashion sense called — it wants an apology.",
    "You're not ugly, you're just… aesthetically challenged.",
    "You have the charisma of a parking ticket.",
    "I've seen better comebacks in a boomerang video.",
    "You're the human equivalent of a Monday morning.",
    "Not even Google could find a reason to like you.",
    "Your confidence is impressive for someone so consistently wrong.",
    "Oh, I'm sorry — did my eye-roll break your fragile feelings?",
    "I'd explain it to you, but I left my crayons at home.",
    "Interesting opinion. I'll file that under \"Things No One Asked.\"",
    "You should come with a mute button.",
    "Keep talking — I need the white noise.",
    "That's a bold statement from someone still figuring out basic WiFi.",
    "Oh, were you still speaking? My brain auto-corrected to silence.",
    "Bold of you to assume I care. Spoiler: I don't.",
    "You're a solid 10… on the pH scale. Acidic.",
    "Be yourself. Just… less.",
    "I'd roast you more, but you're already burned enough by life.",
    "Yikes. And I mean that wholeheartedly.",
    "You're not the dumbest person alive, but you better hope they don't die.",
    "Personality: not found. Please restart.",
    "You radiate \"unfinished homework\" energy.",
    "Life called. It wants a refund.",
    "Irrelevant.",
    "Lowercase.",
    "Buffering…",
    "Participation. (As in, participation trophy energy.)",
    "Autocorrect.",
    "Draft.",
    "Unread.",
    "Ctrl+Z. (As in, someone needs to undo you.)",
    "I'd argue with you, but I'm not fluent in nonsense.",
    "You raise a fair point — if we were living in a fantasy.",
    "Ah yes, the confidence of someone who Googled for 5 minutes.",
    "You're entitled to your wrong opinion.",
    "I don't have the time or the crayons to explain this to you.",
    "That's a great point. For a kindergartener.",
    "Shall we try logic, or are you still warming up?",
    "Keep talking. It gives me time to think of something smarter.",
    "You're the reason they add instructions on shampoo bottles.",
    "I'd love a battle of wits, but you appear unarmed.",
    "Every time you speak, I understand why some animals eat their young.",
    "Your argument is like your WiFi signal — weak and unreliable.",
    "I'm not saying you're wrong, but I am saying that quietly to myself.",
    "Please, continue. I always yawn when I'm fascinated.",
    "You should've just left it at \"no comment.\"",
    "A debate with you is just arguing with a magic 8-ball.",
    "Big talk from someone who peaked in 2014.",
    "Excuse me while I pretend to care.",
    "Sir, this is a Wendy's.",
    "Your vibe is \"checkout line at 4:59 PM on a Friday.\"",
    "I'm not ignoring you. I'm just prioritizing everything else.",
    "Must be exhausting being that confidently incorrect.",
    "You had one job.",
    "Noted. And immediately discarded.",
    "I'd say stay in your lane, but you don't even have one.",
    "Your hate motivates me. So thanks for that.",
    "I'm not here for your validation — or your opinion.",
    "You're a great reminder that some people just shouldn't talk.",
    "I've blocked people for less, but you're almost entertaining.",
    "Hating me is a full-time job. Hope the benefits are good.",
    "I live rent-free in your head, apparently. You're welcome.",
    "The hate in your eyes is giving \"secretly obsessed.\"",
    "I'd say it was nice talking to you, but I value honesty.",
    "That's actually impressive — being that wrong takes effort.",
    "Thanks for sharing. No one will ever repeat that.",
    "And with that, my time spent caring is officially over.",
    "You really came all the way here to be this wrong?",
    "Well. That happened. Moving on.",
    "I'm not even going to dignify that with a clap.",
    "*drops mic* *picks it back up* — it's too expensive to leave near you.",
    "You're the kind of friend who shows up late and eats first.",
    "I keep you around because no one else would tolerate me either.",
    "You're always there when you need something.",
    "We've been friends so long, I've run out of new ways to be disappointed.",
    "You're my person — my cautionary tale, but still my person.",
    "You've seen me at my worst and somehow stayed. That says more about you than me.",
    "True friendship is roasting someone and still picking up when they call.",
    "I'd take a bullet for you. From a water gun. Maybe.",
    "You're the reason our parents set rules.",
    "I was the favorite. Then you came along and lowered the bar.",
    "Growing up with you was free exposure therapy.",
    "You're proof that Mom's second attempt wasn't an upgrade.",
    "I'd say you're like a brother to me, but you actually are — which is the problem.",
    "You're not adopted. Unfortunately, you're just like this.",
    "Siblings are just people you get to roast for life without consequences.",
    "You're my favorite sibling. (There's only one of you, so the bar is low.)",
    "Family reunions are just roast sessions we call \"catching up.\"",
    "You're the cousin who makes my parents say \"at least you're not like that.\"",
    "We share DNA, which is honestly terrifying.",
    "You're proof that genetics is a lottery — and some of us got the scratch card.",
    "Cousins: close enough to mock, far enough to not feel guilty.",
    "I roast you with love. Mostly.",
    "You're like a sibling but with a 50-mile safety radius.",
    "I see you once a year and somehow you've always gotten worse.",
    "I'd insult you, but nature already did the heavy lifting.",
    "You're not annoying — you're just consistently extra.",
    "You're the \"new phone who dis\" of my life.",
    "I appreciate you, mostly as a reminder of who not to be.",
    "You're the kind of person WiFi disconnects from on purpose.",
    "Good friends roast each other. Great friends never stop.",
    "You bring so much joy when you leave the room.",
    "I say this with love: please reconsider.",
    "You have the energy of someone who unironically says \"per my last email.\"",
    "I'm not saying you're boring, but even your hobbies need a nap.",
    "You're so wholesome, even your burns come with a warranty.",
    "You're one in a million — which means there are 8,000 of you.",
    "You speak your mind. I wish you had more of it to share.",
    "Your potential is untapped. So is your common sense.",
    "I respect you. That was a tough sentence to finish.",
    "You're built different. Not better — just different.",
    "I have tremendous respect for you. Tragically, that's where it ends.",
    "Your confidence is a masterpiece of self-deception.",
    "You dress well for someone whose opinions are this under-seasoned.",
    "I say this with grace: you are a lot.",
    "A classy roast doesn't need volume — just precision.",
    "You're the kind of person who brings sparkling water to a roast battle.",
    "Sophistication suits you. Too bad it's just the outfit.",
    "Well-spoken. Deeply wrong. Impeccably dressed about it.",
    "You're very… unique. (Pause.) In the clinical sense.",
    "I mean this kindly — you talk a lot for someone with so little to say.",
    "You're not wrong. You're just not exactly right, either.",
    "I admire your courage to speak without preparation.",
    "What a bold take from a predictable person.",
    "You're doing wonderfully — relative to my lowered expectations.",
    "Your energy is… noted.",
    "Bless your heart. Truly.",
    "You're so silly, even your jokes need a tutorial.",
    "You've got the patience of a phone on 1% battery.",
    "You're not bad at this. You're just training. Still training.",
    "You're the reason \"try again\" exists.",
    "Your brain is in airplane mode — but we believe in you.",
    "You're growing up so fast — mostly in height, which is where it's stopping for now.",
    "You have big ideas for someone with such tiny pockets.",
    "Keep trying! Rome wasn't roasted in a day.",
    "I'd invite you to come outside, but your energy kills plants.",
    "Your vibe is the last 5% of a dying phone — technically functional, mostly annoying.",
    "You're not the villain. You're the character everyone forgets halfway through.",
    "You bring so much to the table — mostly awkward silences.",
    "You have the kind of face that radio was invented for.",
    "I'd say you're a ray of sunshine, but that would be libel.",
    "You're what happens when someone gives up but keeps going anyway.",
    "Your personality is a jumpscare in slow motion.",
    "You think you're cool, but you're room-temperature at best.",
    "Your selfies have more filters than your personality has layers.",
    "You reply to your own tweets. That's all I need to know.",
    "Your TikTok dances are a public health concern.",
    "You text in paragraphs when a \"k\" would do.",
    "You unironically say \"it is what it is\" — and that says everything.",
    "Your humor belongs in a 2010 group chat.",
    "You're giving off main character energy with background character results.",
    "Congrats on sucking the fun out of another room.",
    "You're the reason people develop trust issues.",
    "Your presence is the conversational equivalent of buffering.",
    "You walked in and somehow made it quieter and louder at the same time.",
    "Fun fact: no one was thinking about you until you brought it up.",
    "You're the plot twist no one asked for.",
    "You're like a speed bump — slowing everyone else down for no reason.",
    "Your vibe is \"unexpected charges on a bill.\"",
    "The audacity, truly, of someone with your track record.",
    "You speak as if you have never been wrong before. Incredible.",
    "Your ego has a much better CV than your actual life.",
    "You really woke up today and chose delusion. Respect the commitment.",
    "Confidence is admirable. This, however, is performance art.",
    "You've overestimated yourself so consistently, it's almost a skill.",
    "A little humility wouldn't kill you. I think. Hard to know with you.",
    "You're giving \"I peaked in my imagination.\"",
    "Serving looks. Withholding personality.",
    "Not all that glitters is gold. Some of it is just your ring light.",
    "Posted this before thinking. Classic.",
    "Filters: 47. Self-awareness: 0.",
    "Living my best life — it's still loading, though.",
    "Caption says \"no cap\" — the cap is enormous.",
    "Bought the outfit. Couldn't afford the attitude.",
    "The pose said \"model.\" The caption said \"mid.\"",
    "Went viral for the wrong reasons. We've all been there. (Only you, actually.)",
    "POV: you thought this would be a good idea.",
    "Stitched this and lost faith in myself.",
    "Your FYP said \"yes.\" Your comment section said \"no.\"",
    "Living for the views. The views are not returning the favor.",
    "Duet-ed this and felt secondhand confusion.",
    "This is the content? This is the content.",
    "200K views on a mistake — iconic, actually.",
    "\"In a meeting\" — actually just avoiding you specifically.",
    "My status is \"busy\" but I still saw your message. I chose this.",
    "Last seen: before your essay of a text arrived.",
    "Online. Definitely not going to reply to that.",
    "Typing… (gave up) … typing… (gave up again)",
    "Read receipts off because of people exactly like you.",
    "Status: invisible. From you especially.",
    "If my status says \"available,\" it's lying for everyone except you.",
    "The reel said \"glow up.\" The comments said \"undo.\"",
    "Spent 3 hours on this. It shows in a bad way.",
    "Trending audio. Non-trending content.",
    "This reel asked for a chance and the algorithm said \"no.\"",
    "Cinematic quality. Reality show drama.",
    "I appreciate the effort. The execution, less so.",
    "Someone said \"post it\" — fire that person.",
    "The transitions are smooth. Everything else is chaos.",
    "You went first, which means you're also going home first.",
    "I'm not saying you lost the battle — but history will.",
    "Bold move opening with that. Bolder move thinking it worked.",
    "I've been roasted by better. Your mom, for example.",
    "You brought a water pistol to a bonfire. Respect the effort.",
    "That was a roast? I've felt warmer from a broken radiator.",
    "My comeback is just silence because that was beneath response.",
    "Round 1: you tried. Round 2: I showed up.",
    "Nobody said you couldn't leave the chat. Just saying.",
    "You type like you speak — too much, too long, too often.",
    "We all saw it. Nobody's going to acknowledge it. Moving on.",
    "Muted but still getting the screenshots. That's your legacy.",
    "You're the admin who shouldn't be the admin.",
    "Group chat rule: if it's longer than 3 lines, use email.",
    "You screenshot your own messages. We know.",
    "Your \"good morning\" messages hit different — differently bad.",
    "I didn't come to play. You did. And you still lost.",
    "This was a battle. It became a one-sided documentary.",
    "I gave you chances. You gave me content.",
    "You peaked at \"hello\" and it was downhill from there.",
    "My words were measured. Yours were panicked.",
    "The crowd saw it. The crowd will remember it.",
    "You wanted smoke. You got a wildfire.",
    "The battle is over. The roast continues indefinitely.",
    "Go ahead. Roast me. I've heard worse from my own thoughts.",
    "Do your worst — I set the bar low so you'd feel included.",
    "I'm offering myself up because watching you try will be hilarious.",
    "Come on then. I've got snacks.",
    "I am my own biggest critic, so honestly, get in line.",
    "Hit me. Metaphorically. (And even then, aim better than usual.)",
    "You can't roast someone who's already self-aware at this level.",
    "Roast accepted. Damage: minimal. Confidence: unchanged.",
    "You're the human version of Comic Sans — trying too hard and still wrong.",
    "You're like an expired coupon — technically existed, but no one's using you.",
    "Your logic has the shelf life of warm sushi.",
    "You're a limited edition of bad decisions.",
    "You're an unpaid internship in human form.",
    "Your vibe is a software update nobody asked for.",
    "You speak fluent nonsense with zero accent.",
    "You're the rough draft before the good idea.",
    "I'd fact-check your statements, but fiction has no sources.",
    "You're confidently incorrect — which is almost impressive.",
    "You don't think outside the box. You don't know what a box is.",
    "Your opinion arrived unrequested and will leave the same way.",
    "I heard your argument and raised you: absolutely nothing.",
    "You've mastered the art of saying a lot while saying nothing.",
    "You're not a bad person — you're just a great lesson.",
    "Everything you say comes with an invisible asterisk. \\*This is wrong.",
    "Wow, you're so brave for wearing that.",
    "You really commit to your… choices. Respect.",
    "That's such a unique perspective. Truly unlike anything anyone has said correctly.",
    "I love that you don't care what people think. Must be freeing.",
    "You're so real. Unfiltered. Perhaps too unfiltered.",
    "Your energy is one of a kind. Thankfully.",
    "It's so great that you try. Every time. Despite everything.",
    "You have a very distinctive presence. Very. Distinctive.",
    "You must be a keyboard — because you're not my type.",
    "Are you a bank loan? Because you have my interest… in leaving.",
    "You're like a broken pencil — absolutely pointless.",
    "You must be a light switch — everyone ignores you until they need something.",
    "Are you a parking ticket? Because you have \"fine\" written all over you. (The irony.)",
    "You're like WiFi in the countryside — weak and always dropping.",
    "You're the CAPS LOCK of personalities — loud and accidentally on.",
    "You must be a calculator — full of problems.",
    "I'd roast you harder, but I don't want to start a controlled burn.",
    "Your superpower is making everyone around you look better.",
    "You're the reason I believe in silent mode.",
    "You're not wrong — you're just not welcome.",
    "Somewhere, someone is missing you. We're all confused.",
    "I tried to think of something nice to say. I'll get back to you.",
    "You're proof that nice guys finish last — you're not nice, and you still do.",
    "You didn't just miss the point. You missed the entire target.",
    "Living life on expert mode (still losing).",
    "Outstanding in ways nobody wanted.",
    "The audacity without the credentials.",
    "Running on pure delusion and a strong WiFi signal.",
    "Showing up and somehow still being absent.",
    "Bold fashion. Timid existence.",
    "Thriving in the comments. Struggling everywhere else.",
    "The character. The chaos. The caption.",
    "You're why people go on silent mode.",
    "You could've just not said that.",
    "Your self-awareness is in airplane mode.",
    "Bold move. Wrong move.",
    "You're a lot to deal with in a text.",
    "I'd argue but I'm conserving energy.",
    "You showed up. We'll give you that.",
    "Yikes doesn't cover it, but it's a start.",
    "\"You're giving what you thought was main character, but the audience voted you off.\"",
    "\"In a world full of plot twists, you're the one nobody saw coming — or wanted.\"",
    "\"Your vibe is a buffering screen in the middle of the most important moment.\"",
    "\"You walked into 2026 with 2012 energy. It's giving fossil fuel.\"",
    "\"Serving expired confidence with a side of unsolicited opinions.\"",
    "\"The algorithm would skip you. That's the honest review.\"",
    "\"You're not the villain, you're not the hero — you're the unskippable ad.\"",
    "\"Boldly mediocre in a world that expected at least average.\"",
    "POV: you thought the comment section was your friend.",
    "Nobody asked but they said it anyway — a 2026 tradition.",
    "The ratio doesn't lie. Your tweet does.",
    "You became a meme without even trying — which makes it worse.",
    "Posting in your flop era and calling it a rebrand.",
    "The internet remembered. The internet always remembers.",
    "Went viral for a reason. The reason is bad.",
    "Your comment aged like milk left in a hot car.",
    "You: *confident.* Everyone watching:",
    "That wasn't it, chief. That really, genuinely, was not it.",
    "*achievement unlocked:* Being Wrong With Confidence.",
    "You're an NPC and the game is literally rigged for you.",
    "The audacity loading screen is at 100% and still going.",
    "POV: you just said that in public.",
    "Error 404: self-awareness not found.",
    "Big \"hold my beer\" energy. Tinier results.",
    "The caption said everything. The likes said nothing.",
    "You're chronically online and somehow still out of the loop.",
    "You post a lot for someone with so little to say.",
    "Your presence in comment sections is a jumpscare.",
    "The algorithm timed you out. Honestly, relatable.",
    "You're giving \"main character\" with \"supporting role\" reach.",
    "The glow-up is still processing. Please wait.",
    "You didn't log off — you just gave everyone else a reason to.",
]

CONCEDE_LINES = [
    "Okay okay, you got me with that one. 😭",
    "I have no comeback for that. You win this round.",
    "Bro really cooked me. Taking the L on that.",
    "That one actually hurt my circuits. GG.",
    "I'm a bot and even I felt that.",
    "Alright, that was actually kind of fire. Respect.",
]

ROAST_SYSTEM_PROMPT = (
    "You are a savage but PLAYFUL roast-battle comedian bot in a Discord "
    "server. Write ONE short, punchy roast (1-3 sentences, under 300 "
    "characters) aimed at the given display name, in the same style as "
    "these examples:\n"
    + "\n".join(f"- {line}" for line in random.sample(PUNCHLINE_BANK, 6))
    + "\n\nHard limits, never cross these:\n"
    "- No slurs, no racism, no sexism, no homophobia/transphobia\n"
    "- Nothing about real physical appearance, disability, or body weight\n"
    "- Nothing about family deaths, tragedy, self-harm, or mental health\n"
    "- No sexual content\n"
    "Keep it clever wordplay/comeback energy, not genuine cruelty."
)


async def _generate_roast(display_name: str, context: str = "", pick_fresh=None) -> str:
    # Plan-conscious: the user's supplied punchlines are the primary
    # source now (zero API cost), not just an offline fallback. Groq only
    # gets called for the "context" case (target/joiner said something
    # specific worth roasting back at) where a canned line can't react to
    # what was actually said — and even then, if the call fails or the key
    # is missing, it falls back to the bank like before.
    # `pick_fresh`, when given, selects from PUNCHLINE_BANK without
    # repeating a line already used this battle (see RoastCog._pick_fresh_line)
    # instead of a plain random.choice that can hit the same handful
    # repeatedly while others in the bank never come up.
    fallback = (lambda: pick_fresh(PUNCHLINE_BANK)) if pick_fresh else (lambda: random.choice(PUNCHLINE_BANK))

    if not context:
        return fallback()

    if not GROQ_API_KEY:
        return fallback()
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
            user_msg = f"Roast {display_name}."
            if context:
                user_msg += f" They just said: \"{context[:200]}\" — you can roast that too."
            payload = {
                "model": ROAST_MODEL,
                "messages": [
                    {"role": "system", "content": ROAST_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                "temperature": 0.95,
                "max_tokens": 120,
            }
            async with session.post(
                GROQ_ENDPOINT, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    return text.strip() or fallback()
                logger.warning(f"[v0] roast generation failed: HTTP {resp.status}")
                return fallback()
    except Exception as e:
        logger.warning(f"[v0] roast generation error: {e}")
        return fallback()


def _clone_id_of(bot: commands.Bot):
    return getattr(bot, "clone_id", None)


def _is_admin_member(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or member.id in DISCORD_CLONE_ADMIN_IDS


def _target_options(guild: discord.Guild) -> list[discord.SelectOption]:
    members = [m for m in guild.members if not m.bot][:25]
    return [discord.SelectOption(label=m.display_name, value=str(m.id)) for m in members] or \
        [discord.SelectOption(label="No eligible members", value="none")]


def _channel_options(guild: discord.Guild) -> list[discord.SelectOption]:
    channels = [c for c in guild.text_channels if c.permissions_for(guild.me).send_messages][:25]
    return [discord.SelectOption(label=f"#{c.name}"[:100], value=str(c.id)) for c in channels] or \
        [discord.SelectOption(label="No eligible channels", value="none")]


def _picker_status_embed(guild: discord.Guild, target: discord.Member | None, channel: discord.TextChannel | None) -> discord.Embed:
    embed = discord.Embed(
        title=f"🔥 Roast Opportunity — {guild.name}",
        description="This server looks quiet. Want to start a roast? Pick a target and channel below.",
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"Server ID: {guild.id}")
    embed.add_field(name="Target", value=target.mention if target else "*not picked yet*", inline=True)
    embed.add_field(name="Channel", value=f"#{channel.name}" if channel else "*not picked yet*", inline=True)
    return embed


def build_target_picker_view(guild: discord.Guild, admin_id: int, target_id: int = 0, channel_id: int = 0) -> discord.ui.View:
    """Restart-safe replacement for the old RoastTargetPickerView: every
    child is a DynamicItem (see ROAST_DYNAMIC_ITEMS below) whose custom_id
    carries guild_id/admin_id and the picks made SO FAR, so a fresh view
    can be reconstructed by from_custom_id() even if the bot restarted
    since this message was sent — no more dead "didn't respond in time"
    buttons on old DMs. Trade-off: a restart between picking the target
    and picking the channel loses that in-progress pick (the rebuilt
    picker starts fresh for whichever half wasn't in the custom_id yet)
    since nothing is persisted to the DB — acceptable given how rare a
    mid-pick restart is, versus a permanently dead button before this fix.
    """
    view = discord.ui.View(timeout=None)
    view.add_item(_RoastPickTargetSelect(guild.id, admin_id, channel_id, _target_options(guild)))
    view.add_item(_RoastPickChannelSelect(guild.id, admin_id, target_id, _channel_options(guild)))
    view.add_item(_RoastPickConfirmButton(guild.id, admin_id, target_id, channel_id))
    view.add_item(_RoastPickRemindButton(guild.id, admin_id))
    view.add_item(_RoastPickDontAskButton(guild.id, admin_id))
    return view


def _disabled_picker_view(guild: discord.Guild, admin_id: int, target_id: int, channel_id: int) -> discord.ui.View:
    view = build_target_picker_view(guild, admin_id, target_id, channel_id)
    for child in view.children:
        child.disabled = True
    return view


class _RoastPickTargetSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"^roastpick_target:(\d+):(\d+):(\d+)$"):
    def __init__(self, guild_id: int, admin_id: int, channel_id: int, options: list[discord.SelectOption]):
        self.guild_id = guild_id
        self.admin_id = admin_id
        self.channel_id = channel_id
        super().__init__(discord.ui.Select(
            options=options,
            custom_id=f"roastpick_target:{guild_id}:{admin_id}:{channel_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match[str]"):
        guild_id, admin_id, channel_id = int(match[1]), int(match[2]), int(match[3])
        guild = interaction.client.get_guild(guild_id)
        options = _target_options(guild) if guild else [discord.SelectOption(label="Server unavailable", value="none")]
        return cls(guild_id, admin_id, channel_id, options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("This picker isn't yours.", ephemeral=True)
            return
        guild = interaction.client.get_guild(self.guild_id)
        if guild is None:
            await interaction.response.send_message("Can't reach that server anymore.", ephemeral=True)
            return
        val = self.item.values[0]
        if val == "none":
            await interaction.response.send_message("No eligible members.", ephemeral=True)
            return
        target = guild.get_member(int(val))
        if target is None:
            await interaction.response.send_message("Couldn't find that member — try again.", ephemeral=True)
            return
        channel = guild.get_channel(self.channel_id) if self.channel_id else None
        view = build_target_picker_view(guild, self.admin_id, target.id, self.channel_id)
        await interaction.response.edit_message(embed=_picker_status_embed(guild, target, channel), view=view)


class _RoastPickChannelSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"^roastpick_channel:(\d+):(\d+):(\d+)$"):
    def __init__(self, guild_id: int, admin_id: int, target_id: int, options: list[discord.SelectOption]):
        self.guild_id = guild_id
        self.admin_id = admin_id
        self.target_id = target_id
        super().__init__(discord.ui.Select(
            options=options,
            custom_id=f"roastpick_channel:{guild_id}:{admin_id}:{target_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match[str]"):
        guild_id, admin_id, target_id = int(match[1]), int(match[2]), int(match[3])
        guild = interaction.client.get_guild(guild_id)
        options = _channel_options(guild) if guild else [discord.SelectOption(label="Server unavailable", value="none")]
        return cls(guild_id, admin_id, target_id, options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("This picker isn't yours.", ephemeral=True)
            return
        guild = interaction.client.get_guild(self.guild_id)
        if guild is None:
            await interaction.response.send_message("Can't reach that server anymore.", ephemeral=True)
            return
        val = self.item.values[0]
        if val == "none":
            await interaction.response.send_message("No eligible channels.", ephemeral=True)
            return
        channel = guild.get_channel(int(val))
        if channel is None:
            await interaction.response.send_message("Couldn't find that channel — try again.", ephemeral=True)
            return
        target = guild.get_member(self.target_id) if self.target_id else None
        view = build_target_picker_view(guild, self.admin_id, self.target_id, channel.id)
        await interaction.response.edit_message(embed=_picker_status_embed(guild, target, channel), view=view)


class _RoastPickConfirmButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^roastpick_confirm:(\d+):(\d+):(\d+):(\d+)$"):
    def __init__(self, guild_id: int, admin_id: int, target_id: int, channel_id: int):
        self.guild_id = guild_id
        self.admin_id = admin_id
        self.target_id = target_id
        self.channel_id = channel_id
        super().__init__(discord.ui.Button(
            label="Send Challenge 🔥", style=discord.ButtonStyle.danger,
            disabled=not (target_id and channel_id),
            custom_id=f"roastpick_confirm:{guild_id}:{admin_id}:{target_id}:{channel_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match[str]"):
        return cls(int(match[1]), int(match[2]), int(match[3]), int(match[4]))

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("This picker isn't yours.", ephemeral=True)
            return
        if not self.target_id or not self.channel_id:
            await interaction.response.send_message("Pick both a target and a channel first.", ephemeral=True)
            return
        cog = interaction.client.get_cog("RoastCog")
        guild = interaction.client.get_guild(self.guild_id)
        if cog is None or guild is None:
            await interaction.response.send_message("Roast arena is offline right now, try again shortly.", ephemeral=True)
            return
        target = guild.get_member(self.target_id)
        channel = guild.get_channel(self.channel_id)
        if not target or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "That target or channel isn't available anymore — pick again.", ephemeral=True
            )
            return
        try:
            await interaction.response.edit_message(
                content=f"✅ Challenge sent to {target.mention} in #{channel.name}.",
                embed=None,
                view=_disabled_picker_view(guild, self.admin_id, self.target_id, self.channel_id),
            )
            logger.info(
                f"[roast] challenge queued guild={guild.id} target={target.id} "
                f"channel={channel.id} by_admin={interaction.user.id}"
            )
            await cog.start_challenge(
                guild=guild, target=target, channel=channel, proposed_by_admin_id=interaction.user.id,
            )
            await cog._resolve_proposal_round(
                guild.id, interaction.message.id,
                f"🔥 {interaction.user.display_name} already sent a challenge for this round.",
            )
        except discord.NotFound:
            logger.warning(f"[roast] start_challenge: interaction expired guild={guild.id}")
        except Exception as e:
            logger.exception(f"[roast] start_challenge failed guild={guild.id}: {e!r}")
            try:
                await interaction.followup.send(f"⚠️ Failed to send the challenge — check Railway logs. ({e!r})", ephemeral=True)
            except discord.HTTPException:
                pass


class _RoastPickRemindButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^roastpick_remind:(\d+):(\d+)$"):
    def __init__(self, guild_id: int, admin_id: int):
        self.guild_id = guild_id
        self.admin_id = admin_id
        super().__init__(discord.ui.Button(
            label="Remind Me Later", style=discord.ButtonStyle.secondary,
            custom_id=f"roastpick_remind:{guild_id}:{admin_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match[str]"):
        return cls(int(match[1]), int(match[2]))

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("This picker isn't yours.", ephemeral=True)
            return
        cog = interaction.client.get_cog("RoastCog")
        guild = interaction.client.get_guild(self.guild_id)
        if cog is None or guild is None:
            await interaction.response.send_message("Roast arena is offline right now, try again shortly.", ephemeral=True)
            return
        try:
            await interaction.response.edit_message(
                content=f"⏰ Okay, I'll check back in about {SNOOZE_MINUTES // 60}h.",
                embed=None,
                view=_disabled_picker_view(guild, self.admin_id, 0, 0),
            )
            clone_id = _clone_id_of(cog.bot)
            await db.execute(
                f"""
                INSERT INTO discord_roast_activity (guild_id, clone_id, last_roast_proposed_at)
                VALUES ($1, $2, NOW() - INTERVAL '{PROPOSAL_COOLDOWN_MINUTES - SNOOZE_MINUTES} minutes')
                ON CONFLICT (guild_id, COALESCE(clone_id, -1))
                DO UPDATE SET last_roast_proposed_at = NOW() - INTERVAL '{PROPOSAL_COOLDOWN_MINUTES - SNOOZE_MINUTES} minutes'
                """,
                guild.id, clone_id,
            )
            await cog._resolve_proposal_round(
                guild.id, interaction.message.id,
                f"⏰ {interaction.user.display_name} already snoozed this round — I'll check back later.",
            )
            logger.info(f"[roast] admin={interaction.user.id} snoozed guild={guild.id}")
        except discord.NotFound:
            # Interaction token expired/invalidated (10062) partway through —
            # a sibling click already resolved this round, or the ack came
            # in too late. The DB/round-state work above may be partially
            # applied; there's no valid interaction left to respond on, so
            # just log and stop instead of throwing on the followup too.
            logger.warning(f"[roast] remind_later: interaction expired guild={self.guild_id}")
        except Exception as e:
            # logger.exception() attaches the full traceback via exc_info,
            # but the one-line message itself used to say nothing about
            # *what* failed — if a log viewer truncates or collapses the
            # multi-line traceback (or it scrolls past), there's nothing
            # left to grep for. Putting repr(e) directly in the message
            # guarantees the actual exception type/args survive in the
            # single searchable line, even with the traceback gone.
            logger.exception(f"[roast] remind_later failed guild={self.guild_id}: {e!r}")
            try:
                await interaction.followup.send(
                    f"⚠️ Something went wrong — check Railway logs. ({e!r})", ephemeral=True
                )
            except discord.HTTPException:
                pass


class _RoastPickDontAskButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^roastpick_dontask:(\d+):(\d+)$"):
    def __init__(self, guild_id: int, admin_id: int):
        self.guild_id = guild_id
        self.admin_id = admin_id
        super().__init__(discord.ui.Button(
            label="Don't Ask Again", style=discord.ButtonStyle.secondary,
            custom_id=f"roastpick_dontask:{guild_id}:{admin_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match[str]"):
        return cls(int(match[1]), int(match[2]))

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("This picker isn't yours.", ephemeral=True)
            return
        cog = interaction.client.get_cog("RoastCog")
        guild = interaction.client.get_guild(self.guild_id)
        if cog is None or guild is None:
            await interaction.response.send_message("Roast arena is offline right now, try again shortly.", ephemeral=True)
            return
        try:
            await interaction.response.edit_message(
                content="🔕 Got it, I won't suggest auto-roasts for this server anymore. "
                        "Re-enable anytime with `/roast configure enabled:True`.",
                embed=None,
                view=_disabled_picker_view(guild, self.admin_id, 0, 0),
            )
            clone_id = _clone_id_of(cog.bot)
            current = await cog.get_config(guild.id, clone_id)
            await db.execute(
                """
                INSERT INTO discord_roast_config (guild_id, clone_id, inactivity_minutes, random_chance_percent, enabled)
                VALUES ($1, $2, $3, $4, FALSE)
                ON CONFLICT (guild_id, COALESCE(clone_id, -1))
                DO UPDATE SET enabled = FALSE
                """,
                guild.id, clone_id, current["inactivity_minutes"], current["random_chance_percent"],
            )
            await cog._resolve_proposal_round(
                guild.id, interaction.message.id,
                f"🔕 {interaction.user.display_name} already turned off auto-roasts for this server.",
            )
            logger.info(f"[roast] admin={interaction.user.id} disabled auto-roast guild={guild.id}")
        except discord.NotFound:
            # Same 10062 race as remind_later above — interaction went stale
            # mid-callback (sibling already resolved the round, or the ack
            # landed too late). Nothing valid left to respond on.
            logger.warning(f"[roast] dont_ask_again: interaction expired guild={self.guild_id}")
        except Exception as e:
            logger.exception(f"[roast] dont_ask_again failed guild={self.guild_id}: {e!r}")
            try:
                await interaction.followup.send(f"⚠️ Something went wrong — check Railway logs. ({e!r})", ephemeral=True)
            except discord.HTTPException:
                pass



class RoastMemberRequestView(discord.ui.View):
    """Sent as the response to /setup roastme — any member (not just
    admins) can request a roast on someone, but unlike an admin's own
    /setup roaststart this doesn't go straight to the target. Confirming
    here creates a battle row with status='awaiting_approval' and DMs
    every admin an Approve/Deny prompt (RoastApprovalView); only on
    approval does the target actually get challenged."""

    def __init__(self, cog: "RoastCog", guild: discord.Guild, requester_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.requester_id = requester_id
        self.chosen_target: discord.Member | None = None
        self.chosen_channel: discord.TextChannel | None = None

        members = [m for m in guild.members if not m.bot][:25]
        self.target_select = discord.ui.Select(
            placeholder=f"Pick who you want the bot to roast in {guild.name}...",
            options=[discord.SelectOption(label=m.display_name, value=str(m.id)) for m in members] or
                    [discord.SelectOption(label="No eligible members", value="none")],
            row=0,
        )
        self.target_select.callback = self._on_target
        self.add_item(self.target_select)

        channels = [c for c in guild.text_channels if c.permissions_for(guild.me).send_messages][:25]
        self.channel_select = discord.ui.Select(
            placeholder=f"Pick a channel in {guild.name}...",
            options=[discord.SelectOption(label=f"#{c.name}"[:100], value=str(c.id)) for c in channels] or
                    [discord.SelectOption(label="No eligible channels", value="none")],
            row=1,
        )
        self.channel_select.callback = self._on_channel
        self.add_item(self.channel_select)

        self.confirm_btn = discord.ui.Button(label="Request Roast 🔥", style=discord.ButtonStyle.danger, row=2, disabled=True)
        self.confirm_btn.callback = self._on_confirm
        self.add_item(self.confirm_btn)

    def _status_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"🔥 Request a Roast — {self.guild.name}",
            description="Pick who you want roasted and where. An admin has to approve before it goes out.",
            color=discord.Color.gold(),
        )
        embed.add_field(name="Target", value=self.chosen_target.mention if self.chosen_target else "*not picked yet*", inline=True)
        embed.add_field(name="Channel", value=f"#{self.chosen_channel.name}" if self.chosen_channel else "*not picked yet*", inline=True)
        return embed

    async def _on_target(self, interaction: discord.Interaction):
        try:
            val = self.target_select.values[0]
            if val == "none":
                await interaction.response.send_message("No eligible members.", ephemeral=True)
                return
            self.chosen_target = self.guild.get_member(int(val))
            self.confirm_btn.disabled = not (self.chosen_target and self.chosen_channel)
            await interaction.response.edit_message(embed=self._status_embed(), view=self)
        except Exception as e:
            logger.exception(f"[roast] member request target picker failed guild={self.guild.id}: {e!r}")
            if not interaction.response.is_done():
                await interaction.response.send_message(f"⚠️ Something went wrong — check Railway logs. ({e!r})", ephemeral=True)

    async def _on_channel(self, interaction: discord.Interaction):
        try:
            val = self.channel_select.values[0]
            if val == "none":
                await interaction.response.send_message("No eligible channels.", ephemeral=True)
                return
            self.chosen_channel = self.guild.get_channel(int(val))
            self.confirm_btn.disabled = not (self.chosen_target and self.chosen_channel)
            await interaction.response.edit_message(embed=self._status_embed(), view=self)
        except Exception as e:
            logger.exception(f"[roast] member request channel picker failed guild={self.guild.id}: {e!r}")
            if not interaction.response.is_done():
                await interaction.response.send_message(f"⚠️ Something went wrong — check Railway logs. ({e!r})", ephemeral=True)

    async def _on_confirm(self, interaction: discord.Interaction):
        try:
            if not self.chosen_target or not self.chosen_channel:
                await interaction.response.send_message("Pick both a target and a channel first.", ephemeral=True)
                return
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                content="📨 Request sent to the admins for approval.", embed=None, view=self,
            )
            await self.cog.create_member_request(
                guild=self.guild,
                target=self.chosen_target,
                channel=self.chosen_channel,
                requester_id=interaction.user.id,
            )
        except Exception as e:
            logger.exception(f"[roast] member roast request failed guild={self.guild.id}: {e!r}")
            if not interaction.response.is_done():
                await interaction.response.send_message(f"⚠️ Failed to send the request — check Railway logs. ({e!r})", ephemeral=True)
        finally:
            self.stop()


class _RoastApproveButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^roast_approve:(\d+)$"):
    """DM'd to every admin when a member requests a roast. DynamicItem
    (timeout=None) rather than a plain View(timeout=1800) so this survives
    a bot restart within the 30-minute approval window — same class of bug
    as the fixed ChallengeView (see discover_players.py). Names for the
    confirmation text are resolved from the battle row at click time
    instead of being carried on the instance, since a DynamicItem is
    rebuilt fresh from its custom_id on every dispatch."""

    def __init__(self, battle_id: int):
        self.battle_id = battle_id
        super().__init__(discord.ui.Button(
            label="Approve ✅", style=discord.ButtonStyle.success,
            custom_id=f"roast_approve:{battle_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match"):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("RoastCog")
        if cog is None:
            await interaction.response.send_message("This feature is temporarily unavailable — please try again in a moment.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            allowed, battle = await cog._is_admin_for_battle(interaction.user.id, self.battle_id)
            if not allowed:
                await interaction.edit_original_response(content="🚫 Admins only.")
                return

            target_name, requester_name = await cog._battle_names(battle) if battle else ("someone", "someone")
            ok = await cog.approve_member_request(self.battle_id, interaction.user.id)
            if not ok:
                await interaction.edit_original_response(content="⚠️ Already resolved, expired, or unavailable.", view=_disabled_roast_approval_view(self.battle_id))
                return
            await interaction.edit_original_response(
                content=f"✅ Approved — {target_name} has been challenged.",
                view=_disabled_roast_approval_view(self.battle_id),
            )
        except Exception as e:
            logger.exception(f"[roast] approval failed battle_id={self.battle_id}: {e!r}")
            try:
                await interaction.edit_original_response(content=f"⚠️ Something went wrong — check Railway logs. ({e!r})")
            except Exception:
                pass


class _RoastDenyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^roast_deny:(\d+)$"):
    def __init__(self, battle_id: int):
        self.battle_id = battle_id
        super().__init__(discord.ui.Button(
            label="Deny ❌", style=discord.ButtonStyle.secondary,
            custom_id=f"roast_deny:{battle_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match"):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("RoastCog")
        if cog is None:
            await interaction.response.send_message("This feature is temporarily unavailable — please try again in a moment.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            allowed, battle = await cog._is_admin_for_battle(interaction.user.id, self.battle_id)
            if not allowed:
                await interaction.edit_original_response(content="🚫 Admins only.")
                return

            _, requester_name = await cog._battle_names(battle) if battle else ("someone", "someone")
            ok = await cog.deny_member_request(self.battle_id, interaction.user.id)
            if not ok:
                await interaction.edit_original_response(content="⚠️ Already resolved or expired.", view=_disabled_roast_approval_view(self.battle_id))
                return
            await interaction.edit_original_response(
                content=f"❌ Denied {requester_name}'s roast request.",
                view=_disabled_roast_approval_view(self.battle_id),
            )
        except Exception as e:
            logger.exception(f"[roast] denial failed battle_id={self.battle_id}: {e!r}")
            try:
                await interaction.edit_original_response(content=f"⚠️ Something went wrong — check Railway logs. ({e!r})")
            except Exception:
                pass


def _disabled_roast_approval_view(battle_id: int) -> "RoastApprovalView":
    view = RoastApprovalView(battle_id)
    for child in view.children:
        child.item.disabled = True
    return view


class RoastApprovalView(discord.ui.View):
    """DMed to every admin when a member requests a roast via /setup
    roastme. Any admin approving or denying resolves it for all of them —
    the message is only edited for the admin who acted, but the DB status
    change means a second admin clicking their own copy just gets told
    it's already resolved.

    timeout=None + DynamicItem buttons (see _RoastApproveButton) so this
    survives a bot restart instead of expiring in-memory."""

    def __init__(self, battle_id: int):
        super().__init__(timeout=None)
        self.battle_id = battle_id
        self.add_item(_RoastApproveButton(battle_id))
        self.add_item(_RoastDenyButton(battle_id))


class _RoastAcceptButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^roast_accept:(\d+)$"):
    """DM'd to the challenge target. DynamicItem (timeout=None) for the
    same restart-survival reason as _RoastApproveButton above — target
    identity is checked against the battle row's target_id at click time
    rather than an instance attribute."""

    def __init__(self, battle_id: int):
        self.battle_id = battle_id
        super().__init__(discord.ui.Button(
            label="Accept Challenge 🔥", style=discord.ButtonStyle.danger,
            custom_id=f"roast_accept:{battle_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match"):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("RoastCog")
        if cog is None:
            await interaction.response.send_message("This feature is temporarily unavailable — please try again in a moment.", ephemeral=True)
            return
        battle = await cog.get_battle(self.battle_id)
        if not battle or interaction.user.id != battle["target_id"]:
            await interaction.response.send_message("This challenge isn't yours to answer.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            ok = await cog.accept_battle(self.battle_id)
            if not ok:
                await interaction.edit_original_response(content="⏰ This challenge already expired.", view=_disabled_roast_accept_view(self.battle_id))
                return
            await interaction.edit_original_response(content="✅ Accepted! Head to the server, it's on.", view=_disabled_roast_accept_view(self.battle_id))
        except Exception as e:
            logger.exception(f"[roast] accept button failed battle_id={self.battle_id}: {e!r}")
            try:
                await interaction.followup.send(f"⚠️ Something went wrong accepting — check Railway logs. ({e!r})", ephemeral=True)
            except Exception:
                pass


class _RoastDeclineButton(discord.ui.DynamicItem[discord.ui.Button], template=r"^roast_decline:(\d+)$"):
    def __init__(self, battle_id: int):
        self.battle_id = battle_id
        super().__init__(discord.ui.Button(
            label="Decline", style=discord.ButtonStyle.secondary,
            custom_id=f"roast_decline:{battle_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: "re.Match"):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("RoastCog")
        if cog is None:
            await interaction.response.send_message("This feature is temporarily unavailable — please try again in a moment.", ephemeral=True)
            return
        battle = await cog.get_battle(self.battle_id)
        if not battle or interaction.user.id != battle["target_id"]:
            await interaction.response.send_message("This challenge isn't yours to answer.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            await cog.decline_battle(self.battle_id)
            await interaction.edit_original_response(content="😌 Declined. No roast today.", view=_disabled_roast_accept_view(self.battle_id))
        except Exception as e:
            logger.exception(f"[roast] decline button failed battle_id={self.battle_id}: {e!r}")
            try:
                await interaction.followup.send(f"⚠️ Something went wrong declining — check Railway logs. ({e!r})", ephemeral=True)
            except Exception:
                pass


def _disabled_roast_accept_view(battle_id: int) -> "RoastAcceptView":
    view = RoastAcceptView(battle_id)
    for child in view.children:
        child.item.disabled = True
    return view


class RoastAcceptView(discord.ui.View):
    """Sent to the target's DM. Only the target can press these.
    timeout=None + DynamicItem buttons — see _RoastAcceptButton."""

    def __init__(self, battle_id: int):
        super().__init__(timeout=None)
        self.battle_id = battle_id
        self.add_item(_RoastAcceptButton(battle_id))
        self.add_item(_RoastDeclineButton(battle_id))


class RoastCancelPendingView(discord.ui.View):
    """Shown when someone tries to start a new roast while an existing one
    is still 'pending', 'awaiting_approval', or 'approving' — i.e. hasn't
    gone active yet, so RoastBattleView's Quit button doesn't apply (it
    only ends 'active' battles). Admin-only: an ordinary member shouldn't
    be able to kill someone else's outstanding request/challenge just by
    trying to start their own. Not persistent (default timeout) since it's
    only ever attached to the one-off ephemeral "already one running"
    reply — if the bot restarts before it's clicked, running the command
    again produces a fresh, working button."""

    def __init__(self, cog: "RoastCog", battle_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.battle_id = battle_id

    @discord.ui.button(label="End it", style=discord.ButtonStyle.danger, emoji="🛑")
    async def end_it(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        if not (isinstance(interaction.user, discord.Member) and _is_admin_member(interaction.user)):
            await interaction.followup.send("Only an admin can cancel this.", ephemeral=True)
            return
        cancelled = await self.cog.cancel_pending(self.battle_id)
        if not cancelled:
            await interaction.followup.send(
                "That request already resolved (or went active) — try starting a new roast again.", ephemeral=True,
            )
            return
        for child in self.children:
            child.disabled = True
        try:
            await interaction.edit_original_response(view=self)
        except discord.HTTPException:
            pass
        await interaction.followup.send("🛑 Cancelled — you can start a new roast now.", ephemeral=True)


ROAST_DYNAMIC_ITEMS = (
    _RoastApproveButton, _RoastDenyButton, _RoastAcceptButton, _RoastDeclineButton,
    _RoastPickTargetSelect, _RoastPickChannelSelect, _RoastPickConfirmButton,
    _RoastPickRemindButton, _RoastPickDontAskButton,
)


class RoastBattleView(discord.ui.View):
    """Attached to every roast message posted in the channel while a
    battle is active. Persistent (timeout=None) since a battle can run
    indefinitely and must survive a bot restart — cog_load re-adds it
    keyed by custom_id, which encodes the battle id.

    Only Quit Roast lives here now — joining is no longer a button click.
    Anyone who replies to one of the bot's roast messages gets pulled in
    and roasted back automatically (see on_message's reply-check), which
    is lower friction than hunting for a Join button."""

    def __init__(self, cog: "RoastCog", battle_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.battle_id = battle_id
        self.quit_btn.custom_id = f"roast:quit:{battle_id}"

    @discord.ui.button(label="Quit Roast", style=discord.ButtonStyle.secondary)
    async def quit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Same rule as Approve/Deny above: acknowledge FIRST, before any
        # DB round-trip. This used to fetch the battle (a DB await) before
        # ever calling response.*, so a slow query blew straight through
        # Discord's 3s ack window -> "didn't respond in time" -> the
        # battle stayed stuck 'active' forever -> every future /roast
        # start correctly-but-unhelpfully said "quit it first", and
        # clicking Quit hit the exact same timeout again.
        await interaction.response.defer(ephemeral=True)
        try:
            battle = await self.cog.get_battle(self.battle_id)
            if not battle or battle["status"] != "active":
                await interaction.followup.send("This roast battle already ended.", ephemeral=True)
                return
            member = interaction.user
            allowed = (
                member.id == battle["target_id"]
                or member.id in (battle["joined_ids"] or [])
                or (isinstance(member, discord.Member) and _is_admin_member(member))
            )
            if not allowed:
                await interaction.followup.send("Only someone in the roast, or an admin, can end it.", ephemeral=True)
                return
            await self.cog.end_battle(self.battle_id)
            for child in self.children:
                child.disabled = True
            await interaction.edit_original_response(view=self)
            await interaction.channel.send(f"🏳️ Roast battle ended by {member.mention}.")
        except Exception as e:
            logger.exception(f"[roast] quit button failed battle_id={self.battle_id} user={interaction.user.id}: {e!r}")
            try:
                await interaction.followup.send(f"⚠️ Couldn't end the roast — check Railway logs. ({e!r})", ephemeral=True)
            except Exception:
                pass


class RoastCog(GuildOnlyCog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._active_by_channel: dict[int, int] = {}  # channel_id -> battle_id
        # guild_id -> list of (message_id, discord.Message) for every admin
        # DM sent by the current auto-proposal round. Lets any one admin's
        # terminal action (Send Challenge / Remind Me Later / Don't Ask Again)
        # resolve every other admin's copy of the same prompt too, instead of
        # leaving them live and clickable after the round is already decided.
        # Keyed/compared by message_id (not view identity) since the picker
        # items are now DynamicItems rebuilt fresh per-dispatch — see
        # build_target_picker_view's docstring. Same in-memory-only
        # limitation as before: doesn't survive a restart, but the picker
        # itself no longer breaks when that happens (only this cosmetic
        # "disable my siblings too" step silently no-ops on a stale round).
        self._proposal_rounds: dict[int, list[discord.Message]] = {}
        # battle_id -> set of PUNCHLINE_BANK / CONCEDE_LINES lines already
        # used in that battle, so /roast cycles through the whole bank
        # before any line repeats instead of random.choice picking the
        # same handful over and over by chance. Reset once a battle ends
        # (see end_battle) — in-memory only, doesn't survive a restart,
        # but a repeat right after a restart is a minor cosmetic issue
        # compared to a battle-long "fresh each pick" cycle.
        self._used_punchlines: dict[int, set[str]] = {}
        self._used_concedes: dict[int, set[str]] = {}

    def _pick_fresh_line(self, battle_id: int, bank: list[str], used_map: dict[int, set[str]]) -> str:
        used = used_map.setdefault(battle_id, set())
        available = [line for line in bank if line not in used]
        if not available:
            # Exhausted the whole bank — start a new cycle.
            used.clear()
            available = bank
        choice = random.choice(available)
        used.add(choice)
        return choice

    async def _resolve_proposal_round(self, guild_id: int, acting_message_id: int, note: str):
        """Disable and relabel every other admin's still-open prompt for this
        guild's current proposal round once one admin has acted on theirs."""
        siblings = self._proposal_rounds.pop(guild_id, [])
        for message in siblings:
            if message.id == acting_message_id:
                continue
            try:
                await message.edit(content=note, embed=None, view=None)
            except discord.HTTPException:
                logger.info(f"[roast] couldn't resolve sibling proposal msg={message.id} guild={guild_id}")
            except Exception:
                logger.exception(f"[roast] failed resolving sibling proposal guild={guild_id}")

    async def cog_load(self):
        rows = await db.fetch("SELECT id FROM discord_roast_battles WHERE status = 'active'")
        for row in rows:
            self.bot.add_view(RoastBattleView(self, row["id"]))
        battles = await db.fetch("SELECT id, channel_id FROM discord_roast_battles WHERE status = 'active'")
        for b in battles:
            self._active_by_channel[b["channel_id"]] = b["id"]
        self._poller.start()
        logger.info(f"[roast] cog loaded, {len(rows)} active battle(s) restored, poller running every {POLL_INTERVAL_SECONDS}s")

    def cog_unload(self):
        self._poller.cancel()

    # ---------- DB-backed helpers ----------

    async def get_battle(self, battle_id: int):
        return await db.fetchrow("SELECT * FROM discord_roast_battles WHERE id = $1", battle_id)

    async def _is_admin_for_battle(self, user_id: int, battle_id: int) -> tuple[bool, object | None]:
        """Resolve a DM button click back to the guild for this roast request.

        Approval buttons are delivered in DMs, where interaction.user is a
        discord.User and therefore has no guild_permissions attribute. The
        guild_id stored on the battle is the source of truth for checking the
        user's Administrator permission. Configured clone owners remain an
        explicit global bypass.
        """
        battle = await self.get_battle(battle_id)
        if not battle:
            return False, None

        if user_id in DISCORD_CLONE_ADMIN_IDS:
            return True, battle

        guild = self.bot.get_guild(battle["guild_id"])
        if guild is None:
            logger.warning(
                f"[roast] admin check failed: guild {battle['guild_id']} not found for battle_id={battle_id}"
            )
            return False, battle

        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return False, battle

        return bool(member.guild_permissions.administrator), battle

    async def _battle_names(self, battle) -> tuple[str, str]:
        """(target_name, requester_name) for a battle row, resolved fresh
        from the guild rather than carried on a view instance — needed
        because RoastApprovalView/_RoastApproveButton are now DynamicItems
        rebuilt from just the battle_id on every dispatch (see
        RoastApprovalView's docstring)."""
        guild = self.bot.get_guild(battle["guild_id"]) if battle else None
        target_member = guild.get_member(battle["target_id"]) if guild else None
        requester_member = guild.get_member(battle["proposed_by_admin_id"]) if guild else None
        target_name = target_member.display_name if target_member else str(battle["target_id"]) if battle else "someone"
        requester_name = requester_member.display_name if requester_member else str(battle["proposed_by_admin_id"]) if battle else "someone"
        return target_name, requester_name

    async def get_config(self, guild_id: int, clone_id):
        row = await db.fetchrow(
            "SELECT * FROM discord_roast_config WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
            guild_id, clone_id,
        )
        if row:
            return row
        return {
            "inactivity_minutes": DEFAULT_INACTIVITY_MINUTES,
            "random_chance_enabled": True,
            "random_check_minutes": DEFAULT_RANDOM_CHECK_MINUTES,
            "random_chance_percent": DEFAULT_RANDOM_CHANCE_PERCENT,
            "enabled": True,
        }

    async def start_challenge(self, guild, target, channel, proposed_by_admin_id):
        clone_id = _clone_id_of(self.bot)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=CHALLENGE_EXPIRY_MINUTES)
        try:
            row = await db.fetchrow(
                """
                INSERT INTO discord_roast_battles
                    (guild_id, clone_id, channel_id, target_id, proposed_by_admin_id, status, expires_at)
                VALUES ($1, $2, $3, $4, $5, 'pending', $6)
                RETURNING id
                """,
                guild.id, clone_id, channel.id, target.id, proposed_by_admin_id, expires_at,
            )
        except Exception:
            logger.exception(f"[roast] failed to insert battle row guild={guild.id} target={target.id}")
            return None
        battle_id = row["id"]
        try:
            embed = discord.Embed(
                title="⚠️ You've Been Challenged to a Roast Battle",
                description=(
                    f"An admin in **{guild.name}** wants to roast you in #{channel.name}.\n\n"
                    "Accept and it happens live in the server. Ignore it for "
                    f"{CHALLENGE_EXPIRY_MINUTES} minutes and the bot wins by default."
                ),
                color=discord.Color.orange(),
            )
            await target.send(embed=embed, view=RoastAcceptView(battle_id))
            logger.info(f"[roast] challenge DM sent battle_id={battle_id} target={target.id}")
        except discord.Forbidden:
            await db.execute("UPDATE discord_roast_battles SET status = 'ended', resolved_at = NOW() WHERE id = $1", battle_id)
            logger.info(f"[roast] challenge to {target.id} failed: DMs closed, battle_id={battle_id} ended")
            return None
        except Exception:
            logger.exception(f"[roast] unexpected error DMing target={target.id} battle_id={battle_id}")
            await db.execute("UPDATE discord_roast_battles SET status = 'ended', resolved_at = NOW() WHERE id = $1", battle_id)
            return None
        return battle_id

    async def accept_battle(self, battle_id: int) -> bool:
        battle = await self.get_battle(battle_id)
        if not battle or battle["status"] != "pending":
            logger.warning(f"[roast] accept_battle called on invalid battle_id={battle_id} status={battle['status'] if battle else 'missing'}")
            return False
        await db.execute("UPDATE discord_roast_battles SET status = 'active' WHERE id = $1", battle_id)
        channel = self.bot.get_channel(battle["channel_id"])
        if channel is None:
            logger.warning(f"[roast] accept_battle: channel {battle['channel_id']} not found/cached, battle_id={battle_id}")
            return True
        target = channel.guild.get_member(battle["target_id"])
        if target is None:
            logger.warning(f"[roast] accept_battle: target {battle['target_id']} not found in guild, battle_id={battle_id}")
            return True
        try:
            self._active_by_channel[channel.id] = battle_id
            roast_text = await _generate_roast(
                target.display_name,
                pick_fresh=lambda bank: self._pick_fresh_line(battle_id, bank, self._used_punchlines),
            )
            embed = discord.Embed(
                title="🔥 Roast Battle — LIVE",
                description=roast_text,
                color=discord.Color.red(),
            )
            embed.set_footer(text="Reply to this message to fire back or jump in.")
            view = RoastBattleView(self, battle_id)
            self.bot.add_view(view)
            await channel.send(content=target.mention, embed=embed, view=view)
            logger.info(f"[roast] battle_id={battle_id} went active in channel={channel.id}")
        except Exception:
            logger.exception(f"[roast] failed to post opening roast battle_id={battle_id} channel={channel.id}")
        await self._notify_owners(
            f"🔥 Roast battle started in **{channel.guild.name}** (#{channel.name}) — target: {target.display_name}, battle_id={battle_id}"
        )
        return True

    async def _notify_owners(self, text: str):
        """DMs every configured bot owner (DISCORD_CLONE_ADMIN_IDS) — best
        effort, one owner's closed DMs shouldn't block the others."""
        for owner_id in DISCORD_CLONE_ADMIN_IDS:
            try:
                owner = self.bot.get_user(owner_id) or await self.bot.fetch_user(owner_id)
                await owner.send(text)
            except discord.Forbidden:
                logger.info(f"[roast] couldn't DM owner={owner_id} (DMs closed)")
            except Exception:
                logger.exception(f"[roast] failed to notify owner={owner_id}")

    async def decline_battle(self, battle_id: int):
        await db.execute("UPDATE discord_roast_battles SET status = 'ended', resolved_at = NOW() WHERE id = $1", battle_id)

    async def join_battle(self, battle_id: int, user_id: int):
        await db.execute(
            "UPDATE discord_roast_battles SET joined_ids = array_append(joined_ids, $2) WHERE id = $1",
            battle_id, user_id,
        )

    async def end_battle(self, battle_id: int):
        await db.execute(
            "UPDATE discord_roast_battles SET status = 'ended', resolved_at = NOW() WHERE id = $1", battle_id,
        )
        battle = await self.get_battle(battle_id)
        if battle and self._active_by_channel.get(battle["channel_id"]) == battle_id:
            del self._active_by_channel[battle["channel_id"]]
        self._used_punchlines.pop(battle_id, None)
        self._used_concedes.pop(battle_id, None)

    def _blocking_battle_view(self, existing) -> discord.ui.View:
        """Picks the right "end it" control for whatever's blocking a new
        roast from starting, so people aren't just told to wait — they get
        a button that actually resolves it. 'active' battles use the same
        Quit Roast control already shown in-channel (restricted to the
        target/joined members/admins); anything not yet active (pending /
        awaiting_approval / approving) uses the admin-only cancel button
        instead, since Quit Roast only recognizes 'active' battles and
        would otherwise incorrectly claim it "already ended"."""
        if existing["status"] == "active":
            return RoastBattleView(self, existing["id"])
        return RoastCancelPendingView(self, existing["id"])

    async def cancel_pending(self, battle_id: int) -> bool:
        """Force-cancels a battle still stuck in 'pending', 'awaiting_approval',
        or 'approving' — the states _expire_stale_challenges would otherwise
        only clear after CHALLENGE_EXPIRY_MINUTES. Used by the "End it"
        button offered when someone tries to start a new roast while one of
        these is already blocking them. Re-checks the status before writing
        so this can't accidentally cancel a battle that became active (or
        was already resolved) between the button being shown and clicked.
        Returns False (no-op) if the battle is no longer in one of those
        states."""
        row = await db.fetchrow(
            "UPDATE discord_roast_battles SET status = 'expired', resolved_at = NOW() "
            "WHERE id = $1 AND status IN ('pending', 'awaiting_approval', 'approving') "
            "RETURNING id",
            battle_id,
        )
        return row is not None

    # ---------- listeners ----------

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        clone_id = _clone_id_of(self.bot)
        await db.execute(
            """
            INSERT INTO discord_roast_activity (guild_id, clone_id, last_message_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (guild_id, COALESCE(clone_id, -1))
            DO UPDATE SET last_message_at = NOW()
            """,
            message.guild.id, clone_id,
        )

        battle_id = self._active_by_channel.get(message.channel.id)
        if not battle_id:
            return
        battle = await self.get_battle(battle_id)
        if not battle or battle["status"] != "active":
            self._active_by_channel.pop(message.channel.id, None)
            return

        already_in = message.author.id == battle["target_id"] or message.author.id in (battle["joined_ids"] or [])

        if not already_in:
            # Not the target and hasn't joined yet — only pull them in if
            # they're replying TO one of the bot's own roast messages.
            # This replaces the old "Join Roast" button: replying is the
            # join action now, no click needed.
            ref = message.reference
            if ref is None:
                return
            try:
                replied_to = ref.resolved or await message.channel.fetch_message(ref.message_id)
            except (discord.NotFound, discord.HTTPException):
                replied_to = None
            if replied_to is None or replied_to.author.id != self.bot.user.id:
                return
            await self.join_battle(battle_id, message.author.id)
            logger.info(f"[roast] user={message.author.id} auto-joined battle_id={battle_id} via reply")

        if random.randint(1, 100) <= BOT_CONCEDE_CHANCE_PERCENT:
            # Bot "roasted back" by the member — occasionally take the L
            # instead of always firing another roast, so it feels like a
            # real back-and-forth instead of the bot being unbeatable.
            # Skips the Groq call entirely in this branch — no point
            # generating a roast just to throw it away.
            roast_text = self._pick_fresh_line(battle_id, CONCEDE_LINES, self._used_concedes)
        else:
            roast_text = await _generate_roast(
                message.author.display_name,
                context=message.content,
                pick_fresh=lambda bank: self._pick_fresh_line(battle_id, bank, self._used_punchlines),
            )
        embed = discord.Embed(description=roast_text, color=discord.Color.red())
        try:
            # A human roast-battle comeback doesn't land in 0ms — show
            # typing and hold for a beat so it doesn't feel instant/robotic.
            async with message.channel.typing():
                await asyncio.sleep(random.uniform(1.5, 3.5))
            await message.reply(embed=embed, view=RoastBattleView(self, battle_id))
        except discord.HTTPException:
            await message.channel.send(embed=embed, view=RoastBattleView(self, battle_id))

    # ---------- background poller ----------

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def _poller(self):
        try:
            await self._expire_stale_challenges()
        except Exception:
            logger.exception("[roast] _expire_stale_challenges failed")
        try:
            await self._check_triggers()
        except Exception:
            logger.exception("[roast] _check_triggers failed")

    @_poller.before_loop
    async def _before_poller(self):
        await self.bot.wait_until_ready()

    async def _expire_stale_challenges(self):
        rows = await db.fetch(
            "SELECT * FROM discord_roast_battles WHERE status IN ('pending', 'awaiting_approval', 'approving') AND expires_at <= NOW()"
        )
        for battle in rows:
            await db.execute(
                "UPDATE discord_roast_battles SET status = 'expired', resolved_at = NOW() WHERE id = $1",
                battle["id"],
            )
            # An approval request expiring is not the same as the target
            # ignoring an actual challenge, so only pending challenges get
            # the public "bot wins" announcement.
            if battle["status"] == "pending":
                channel = self.bot.get_channel(battle["channel_id"])
                if channel:
                    target = channel.guild.get_member(battle["target_id"])
                    name = target.mention if target else "The challenged member"
                    try:
                        await channel.send(f"🏆 {name} didn't accept in time — bot wins by default. Coward. 😏")
                    except discord.HTTPException:
                        pass

    async def _check_triggers(self):
        clone_id = _clone_id_of(self.bot)
        now = datetime.now(timezone.utc)
        for guild in self.bot.guilds:
            if getattr(guild, "unavailable", False):
                continue
            config = await self.get_config(guild.id, clone_id)
            if not config["enabled"]:
                continue
            # skip guilds with an unresolved pending/active battle already
            existing = await db.fetchrow(
                "SELECT id FROM discord_roast_battles WHERE guild_id = $1 AND status IN ('pending','active','awaiting_approval') LIMIT 1",
                guild.id,
            )
            if existing:
                continue

            activity = await db.fetchrow(
                "SELECT * FROM discord_roast_activity WHERE guild_id = $1 AND clone_id IS NOT DISTINCT FROM $2",
                guild.id, clone_id,
            )
            last_message_at = activity["last_message_at"] if activity else None
            last_proposed_at = activity["last_roast_proposed_at"] if activity else None

            # Hard cooldown: never DM admins a new roast suggestion more
            # than once every PROPOSAL_COOLDOWN_MINUTES, no matter which
            # trigger below would otherwise fire.
            on_cooldown = (
                last_proposed_at is not None
                and (now - last_proposed_at).total_seconds() / 60 < PROPOSAL_COOLDOWN_MINUTES
            )

            triggered = False
            if not on_cooldown and last_message_at:
                idle_minutes = (now - last_message_at).total_seconds() / 60
                # Only propose once per idle period: skip if we already
                # proposed since the last message came in.
                already_proposed_this_idle = (
                    last_proposed_at is not None and last_proposed_at >= last_message_at
                )
                if idle_minutes >= config["inactivity_minutes"] and not already_proposed_this_idle:
                    triggered = True

            if not triggered and not on_cooldown and config["random_chance_enabled"]:
                due_for_check = (
                    last_proposed_at is None
                    or (now - last_proposed_at).total_seconds() / 60 >= config["random_check_minutes"]
                )
                if due_for_check and random.randint(1, 100) <= config["random_chance_percent"]:
                    triggered = True

            if not triggered:
                continue

            logger.info(f"[roast] trigger fired guild={guild.id}")
            await db.execute(
                """
                INSERT INTO discord_roast_activity (guild_id, clone_id, last_roast_proposed_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (guild_id, COALESCE(clone_id, -1))
                DO UPDATE SET last_roast_proposed_at = NOW()
                """,
                guild.id, clone_id,
            )
            await self._propose_to_admins(guild)

    async def _propose_to_admins(self, guild: discord.Guild):
        admins = [m for m in guild.members if not m.bot and _is_admin_member(m)]
        if not admins:
            logger.warning(f"[roast] trigger fired but no admins found guild={guild.id}")
            return
        sent = 0
        round_messages: list[discord.Message] = []
        for admin in admins:
            try:
                view = build_target_picker_view(guild, admin.id)
                msg = await admin.send(embed=_picker_status_embed(guild, None, None), view=view)
                round_messages.append(msg)
                sent += 1
            except discord.Forbidden:
                logger.info(f"[roast] couldn't DM admin={admin.id} guild={guild.id} (DMs closed)")
                continue
            except Exception:
                logger.exception(f"[roast] failed to DM admin={admin.id} guild={guild.id}")
                continue
        if round_messages:
            self._proposal_rounds[guild.id] = round_messages
        logger.info(f"[roast] proposal sent to {sent}/{len(admins)} admins guild={guild.id}")

    # ---------- member-requested roast (needs admin approval) ----------

    async def request_from_member(self, interaction: discord.Interaction):
        """Entry point for /setup roastme — open to ALL members, not just
        admins. Blocks on an existing pending/active/awaiting-approval
        battle same as the admin path, so members can't stack requests."""
        await interaction.response.defer()
        existing = await db.fetchrow(
            "SELECT id, status FROM discord_roast_battles WHERE guild_id = $1 "
            "AND status IN ('pending','active','awaiting_approval') LIMIT 1",
            interaction.guild.id,
        )
        if existing:
            await interaction.followup.send(
                f"⚠️ There's already a {existing['status']} roast battle/request in this server.",
                view=self._blocking_battle_view(existing),
                ephemeral=True,
            )
            return
        view = RoastMemberRequestView(self, interaction.guild, interaction.user.id)
        await interaction.followup.send(embed=view._status_embed(), view=view, ephemeral=True)
        logger.info(f"[roast] member request flow opened by user={interaction.user.id} guild={interaction.guild.id}")

    async def create_member_request(self, guild, target, channel, requester_id):
        clone_id = _clone_id_of(self.bot)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=CHALLENGE_EXPIRY_MINUTES)
        try:
            row = await db.fetchrow(
                """
                INSERT INTO discord_roast_battles
                    (guild_id, clone_id, channel_id, target_id, proposed_by_admin_id, status, expires_at)
                VALUES ($1, $2, $3, $4, $5, 'awaiting_approval', $6)
                RETURNING id
                """,
                guild.id, clone_id, channel.id, target.id, requester_id, expires_at,
            )
        except Exception:
            logger.exception(f"[roast] failed to insert member-request row guild={guild.id}")
            return
        battle_id = row["id"]
        requester = guild.get_member(requester_id)
        requester_name = requester.display_name if requester else str(requester_id)
        admins = [m for m in guild.members if not m.bot and _is_admin_member(m)]
        if not admins:
            logger.warning(f"[roast] member request battle_id={battle_id} has no admins to approve it")
            await db.execute("UPDATE discord_roast_battles SET status = 'ended' WHERE id = $1", battle_id)
            return
        sent = 0
        for admin in admins:
            try:
                embed = discord.Embed(
                    title=f"🔥 Roast Request — {guild.name}",
                    description=(
                        f"{requester_name} wants the bot to roast {target.display_name} "
                        f"in #{channel.name}. Approve to send the challenge."
                    ),
                    color=discord.Color.gold(),
                )
                await admin.send(embed=embed, view=RoastApprovalView(battle_id))
                sent += 1
            except discord.Forbidden:
                continue
            except Exception:
                logger.exception(f"[roast] failed to DM admin={admin.id} for approval battle_id={battle_id}")
        logger.info(f"[roast] member request battle_id={battle_id} sent to {sent}/{len(admins)} admins")

    async def approve_member_request(self, battle_id: int, admin_id: int) -> bool:
        # Atomically claim the request. If two admins click Approve, only the
        # first one can transition awaiting_approval -> approving.
        battle = await db.fetchrow(
            """
            UPDATE discord_roast_battles
               SET status = 'approving'
             WHERE id = $1
               AND status = 'awaiting_approval'
               AND expires_at > NOW()
         RETURNING *
            """,
            battle_id,
        )
        if not battle:
            return False

        guild = self.bot.get_guild(battle["guild_id"])
        if guild is None:
            logger.warning(f"[roast] approve_member_request: guild {battle['guild_id']} not found, battle_id={battle_id}")
            await db.execute(
                "UPDATE discord_roast_battles SET status = 'ended', resolved_at = NOW() WHERE id = $1",
                battle_id,
            )
            return False

        channel = self.bot.get_channel(battle["channel_id"])
        target = guild.get_member(battle["target_id"])
        if channel is None or target is None:
            logger.warning(f"[roast] approve_member_request: missing channel/target for battle_id={battle_id}")
            await db.execute(
                "UPDATE discord_roast_battles SET status = 'ended', resolved_at = NOW() WHERE id = $1",
                battle_id,
            )
            return False

        logger.info(f"[roast] member request battle_id={battle_id} approved by admin={admin_id}")
        new_battle_id = await self.start_challenge(
            guild=guild, target=target, channel=channel, proposed_by_admin_id=admin_id
        )
        if new_battle_id is None:
            # Keep the request available if creating/sending the real challenge
            # failed, rather than deleting the only source of truth.
            await db.execute(
                """
                UPDATE discord_roast_battles
                   SET status = 'awaiting_approval'
                 WHERE id = $1 AND status = 'approving'
                """,
                battle_id,
            )
            return False

        # The new pending row is now the real challenge. Resolve the approval
        # row only after the new challenge was successfully created and DM'd.
        await db.execute(
            "UPDATE discord_roast_battles SET status = 'ended', resolved_at = NOW() WHERE id = $1",
            battle_id,
        )
        return True

    async def deny_member_request(self, battle_id: int, admin_id: int) -> bool:
        row = await db.fetchrow(
            """
            UPDATE discord_roast_battles
               SET status = 'ended', resolved_at = NOW()
             WHERE id = $1
               AND status = 'awaiting_approval'
               AND expires_at > NOW()
         RETURNING id
            """,
            battle_id,
        )
        if not row:
            return False
        logger.info(f"[roast] member request battle_id={battle_id} denied by admin={admin_id}")
        return True

    # ---------- admin manual trigger ----------

    async def manual_trigger(self, interaction: discord.Interaction):
        """Lets an admin skip the inactivity/random wait and pop the
        target+channel picker immediately, right in the server instead of
        via DM. Same picker view builder as the automatic flow, and
        still respects the one-battle-per-guild guard in start_challenge's
        caller — but since this is manual, we don't need the trigger-level
        existing-battle check from _check_triggers (an admin explicitly
        asking should still be told plainly if one's already running)."""
        await interaction.response.defer(ephemeral=True)
        if not _is_admin_member(interaction.user):
            await interaction.followup.send("🚫 Admins only.", ephemeral=True)
            return
        existing = await db.fetchrow(
            "SELECT id, status FROM discord_roast_battles WHERE guild_id = $1 AND status IN ('pending','active','awaiting_approval') LIMIT 1",
            interaction.guild.id,
        )
        if existing:
            await interaction.followup.send(
                f"⚠️ There's already a {existing['status']} roast battle in this server (battle_id={existing['id']}).",
                view=self._blocking_battle_view(existing),
                ephemeral=True,
            )
            return
        view = build_target_picker_view(interaction.guild, interaction.user.id)
        await interaction.followup.send(embed=_picker_status_embed(interaction.guild, None, None), view=view, ephemeral=True)
        logger.info(f"[roast] manual trigger opened by admin={interaction.user.id} guild={interaction.guild.id}")

    # ---------- admin config ----------
    # Deliberately NOT its own top-level app_commands.command — the bot's
    # already near Discord's 100-command cap, so config lives as a
    # subcommand on the existing /setup group (setup_channels.py) instead
    # of adding a new one. See configure_from_setup() below, called from
    # there.

    async def configure(self, interaction: discord.Interaction, inactivity_minutes: int = None,
                         random_chance_percent: int = None, enabled: bool = None):
        await interaction.response.defer(ephemeral=True)
        if not _is_admin_member(interaction.user):
            await interaction.followup.send("🚫 Admins only.", ephemeral=True)
            return
        clone_id = _clone_id_of(self.bot)
        current = await self.get_config(interaction.guild.id, clone_id)
        new_inactivity = inactivity_minutes if inactivity_minutes is not None else current["inactivity_minutes"]
        new_chance = random_chance_percent if random_chance_percent is not None else current["random_chance_percent"]
        new_enabled = enabled if enabled is not None else current["enabled"]
        await db.execute(
            """
            INSERT INTO discord_roast_config (guild_id, clone_id, inactivity_minutes, random_chance_percent, enabled)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (guild_id, COALESCE(clone_id, -1))
            DO UPDATE SET inactivity_minutes = $3, random_chance_percent = $4, enabled = $5
            """,
            interaction.guild.id, clone_id, new_inactivity, new_chance, new_enabled,
        )
        await interaction.followup.send(
            f"✅ Auto-roast config updated — inactivity: {new_inactivity}m, "
            f"random chance: {new_chance}%, enabled: {new_enabled}",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(RoastCog(bot))
