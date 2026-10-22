# path: utils/dash_schema.py

"""
Settings catalogue + validation for the web dashboard (dashboard/ on Cloudflare Pages).

Each module maps to one existing database get_*/set_*_config pair, so the dashboard
reads and writes exactly what the slash commands and in-Discord wizards use. The
frontend renders forms straight from this catalogue, so adding a setting is one entry
here and nothing else.

Pure functions only (no I/O): everything is unit-tested in tests/unit/test_dash_schema.py.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

MANAGE_GUILD = 0x20
ADMINISTRATOR = 0x8
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def F(key, label, type_, help_="", **kw) -> dict:
    d = {"key": key, "label": label, "type": type_, "help": help_}
    d.update(kw)
    return d


def opts(*pairs) -> list:
    return [[v, l] for v, l in pairs]


MODULES: List[dict] = [
    {
        "id": "welcome", "title": "Welcome", "category": "Onboarding", "icon": "wave",
        "desc": "Greet new members with a welcome card and message.",
        "get": "get_welcome_config", "set": "set_welcome_config",
        "fields": [
            F("enabled", "Welcome messages", "toggle", "Post a welcome when someone joins."),
            F("channel_id", "Welcome channel", "channel", "Where the welcome is posted.", kind="text"),
            F("message_template", "Message", "textarea", "Placeholders: {member}, {guild}, {count}.", maxlen=500),
            F("use_template", "Designed card", "toggle", "On: artwork card with a theme. Off: plain colour card (uses the colours below)."),
            F("card_theme", "Card theme", "select", "Artwork used by the designed card. Only Wolf is free.",
              options=opts(("wolf", "Wolf (free)"), ("reaper", "Reaper"), ("shadow", "Shadow"), ("sorcerer", "Sorcerer"),
                           ("spider", "Spider"), ("spider_pro", "Spider Pro")),
              premium_values=["reaper", "shadow", "sorcerer", "spider", "spider_pro"]),
            F("avatar_shape", "Avatar shape", "select", "Shape of the member's picture on the card.",
              options=opts(("circle", "Circle"), ("rounded_square", "Rounded square"), ("square", "Square"),
                           ("hexagon", "Hexagon"), ("diamond", "Diamond"))),
            F("accent_color", "Accent colour", "color", "Used by the plain colour card."),
            F("background_color", "Background colour", "color", "Used by the plain colour card."),
        ],
        "designer": "welcome",
        "actions": [{"id": "welcome_test", "label": "Send test welcome",
                     "help": "Posts a sample card for you in the welcome channel (saved settings)."}],
    },
    {
        "id": "verification", "title": "Join verification", "category": "Onboarding", "icon": "shield-check",
        "desc": "Hold new members in an Unverified role until they pass a check.",
        "get": "get_verification_config", "set": "set_verification_config", "no_quick": True,
        "note": "The verify button message is posted by the bot. Run /setupverification in Discord once, then fine-tune everything here. Premium servers also get a custom panel, log channel, auto-kick, minimum account age and an after-verify message.",
        "fields": [
            F("enabled", "Verification", "toggle", "Turn the join check on or off."),
            F("mode", "Check type", "select", "Captcha sends members to a Cloudflare Turnstile page.",
              options=opts(("button", "Button click"), ("captcha", "Captcha (Cloudflare)"))),
            F("channel_id", "Verify channel", "channel", "The channel holding the verify button.", kind="text"),
            F("unverified_role_id", "Unverified role", "role", "Given on join, removed after passing."),
            F("verified_role_id", "Verified role", "role", "Given after passing."),
            F("timeout_seconds", "Time limit (seconds)", "number", "How long a member has to verify.", min=30, max=3600),
            F("max_attempts", "Max attempts", "number", "Wrong answers allowed before a kick.", min=1, max=10),
            F("panel_title", "Panel title", "text", "Premium: your own title on the verify message.", maxlen=100, premium=True),
            F("panel_text", "Panel text", "textarea", "Premium: your own text on the verify message.", maxlen=600, premium=True),
            F("panel_color", "Panel colour", "color", "Premium: colour stripe of the verify message. Clear the box to go back to the default.",
              as_int=True, premium=True),
            F("panel_image_url", "Panel banner", "text", "Premium: a direct https:// link to a banner image.",
              maxlen=300, https_only=True, premium=True),
            F("log_channel_id", "Verification log", "channel", "Premium: posts who verified and who was kicked.", kind="text", premium=True),
            F("kick_after_minutes", "Kick if not verified", "select", "Premium: remove members who never verify.",
              options=opts((0, "Off"), (10, "10 minutes"), (30, "30 minutes"), (60, "1 hour"), (360, "6 hours"), (1440, "24 hours")),
              numeric=True, null_zero=True, premium=True),
            F("min_account_age_days", "Minimum account age", "select", "Premium: turn away accounts younger than this.",
              options=opts((0, "Off"), (1, "1 day"), (3, "3 days"), (7, "7 days"), (30, "30 days")),
              numeric=True, null_zero=True, premium=True),
            F("welcome_text", "Message after verifying", "textarea", "Premium: DM sent once a member passes. Placeholders: {user}, {server}.",
              maxlen=500, premium=True),
        ],
        "actions": [{"id": "verify_panel", "label": "Post verify panel",
                     "help": "Posts the verify button message in the verify channel (needs the channel and Unverified role saved). Does not lock any channels; /setupverification does that."}],
    },
    {
        "id": "antiraid", "title": "Anti-raid", "category": "Security", "icon": "siren",
        "desc": "Detect join spikes and respond automatically.",
        "get": "get_antiraid_config", "set": "set_antiraid_config",
        "fields": [
            F("enabled", "Anti-raid", "toggle", "Watch for sudden join spikes."),
            F("sensitivity", "Sensitivity", "select", "How fast joins must arrive to count as a raid.",
              options=opts(("relaxed", "Relaxed: 12 joins in 60s"), ("balanced", "Balanced: 7 joins in 30s"),
                           ("strict", "Strict: 4 joins in 15s"))),
            F("response", "When a raid starts", "select", "",
              options=opts(("alert", "Alert staff only"), ("lockdown", "Alert and lock the server down"))),
            F("joiner_action", "New joiners during a raid", "select", "",
              options=opts(("none", "Leave them alone"), ("timeout", "Timeout for 1 hour"), ("kick", "Kick"),
                           ("quarantine", "Quarantine for review (Premium)")), premium_values=["quarantine"]),
            F("lockdown_minutes", "Lockdown length", "select", "",
              options=opts((5, "5 minutes"), (15, "15 minutes"), (30, "30 minutes"), (60, "1 hour"), (180, "3 hours")), numeric=True),
            F("log_channel_id", "Alert channel", "channel", "Where raid alerts go.", kind="text"),
            F("alert_role_id", "Role to ping", "role", "Pinged when a raid starts."),
            F("filter_age_days", "Minimum account age", "select", "Premium: catch brand-new accounts at the door.",
              options=opts((0, "Off"), (1, "1 day"), (3, "3 days"), (7, "7 days"), (14, "14 days"), (30, "30 days")),
              numeric=True, premium=True),
            F("filter_default_avatar", "Flag default avatars", "toggle", "Premium.", premium=True),
            F("filter_suspicious_name", "Flag suspicious names", "toggle", "Premium.", premium=True),
            F("filter_action", "Filter action", "select", "Premium: what happens to flagged accounts.",
              options=opts(("flag", "Flag in the log"), ("quarantine", "Quarantine for review"), ("kick", "Kick")), premium=True),
        ],
    },
    {
        "id": "automod", "title": "Auto-moderation", "category": "Security", "icon": "gavel",
        "desc": "Filter words, invites, mention spam and message floods.",
        "get": "get_automod_config", "set": "set_automod_config",
        "fields": [
            F("action", "Punishment", "select", "What happens when a rule is broken.",
              options=opts(("delete", "Delete the message"), ("warn", "Delete and warn"),
                           ("timeout", "Timeout the member"), ("kick", "Kick the member"))),
            F("timeout_minutes", "Timeout length (minutes)", "number", "Used when the punishment is Timeout.", min=1, max=40320),
            F("log_channel_id", "Log channel", "channel", "Where actions are recorded.", kind="text"),
            F("word_filter_enabled", "Word filter", "toggle", "Block the words listed below."),
            F("banned_words", "Banned words", "list", "One per line.", maxitems=200, maxlen=60),
            F("anti_invite_enabled", "Block invite links", "toggle", "Delete other servers' invites."),
            F("anti_mention_enabled", "Mention spam", "toggle", "Punish mass pings."),
            F("anti_mention_threshold", "Mentions allowed", "number", "Per message.", min=2, max=50),
            F("spam_enabled", "Flood protection", "toggle", "Punish rapid-fire messages."),
            F("spam_flood_threshold", "Messages allowed", "number", "Within the window below.", min=3, max=50),
            F("spam_flood_window_seconds", "Window (seconds)", "number", "", min=3, max=120),
            F("min_account_age_hours", "Minimum account age (hours)", "number", "0 turns this off.", min=0, max=8760),
        ],
    },
    {
        "id": "modlog", "title": "Server logs", "category": "Security", "icon": "scroll",
        "desc": "Choose which server events are written to your log channel.",
        "get": "get_automod_config", "set": "set_automod_config",
        "fields": [
            F("log_server_enabled", "Server changes", "toggle"),
            F("log_channels_enabled", "Channel changes", "toggle"),
            F("log_roles_enabled", "Role changes", "toggle"),
            F("log_members_enabled", "Member joins and leaves", "toggle"),
            F("log_moderation_enabled", "Moderation actions", "toggle"),
            F("log_voice_enabled", "Voice activity", "toggle"),
            F("log_invites_enabled", "Invites", "toggle"),
        ],
    },
    {
        "id": "honeypot", "title": "Honeypot", "category": "Security", "icon": "bug",
        "desc": "A trap channel that catches spam bots and hacked accounts.",
        "get": "get_honeypot_config", "set": "set_honeypot_config",
        "fields": [
            F("enabled", "Honeypot", "toggle", "Act on anyone who posts in the trap channel."),
            F("action", "Punishment", "select", "Premium: free servers always ban.",
              options=opts(("ban", "Ban"), ("kick", "Kick"), ("timeout", "Timeout (28 days)")), premium=True),
            F("delete_seconds", "Delete their messages from", "select", "Premium: how much history to wipe.",
              options=opts((0, "Don't delete"), (3600, "Last hour"), (86400, "Last 24 hours"), (604800, "Last 7 days")),
              numeric=True, premium=True),
            F("log_channel_id", "Log channel", "channel", "", kind="text"),
            F("alert_role_id", "Role to ping", "role", ""),
        ],
    },
    {
        "id": "scamshield", "title": "Scam Shield", "category": "Security", "icon": "shield-check",
        "desc": "Catch scam and phishing links and deal with whoever posted them.",
        "get": "get_scam_shield_guild", "set": "set_scam_shield_guild",
        "note": "The bot reads these settings every few minutes, so a change can take up to 5 minutes to apply. "
                "Quarantine needs a quarantine role set up in Discord first (Server Panel).",
        "fields": [
            F("enabled", "Scam Shield", "toggle", "Scan messages for known scam and phishing links."),
            F("punish_action", "When a scam is caught", "select", "What happens to the person who posted it. The message is always deleted.",
              options=opts(("delete", "Delete the message only"), ("timeout", "Delete and time out (24h)"),
                           ("quarantine", "Delete and quarantine"), ("kick", "Delete and kick"), ("ban", "Delete and ban"))),
            F("allowed_domains", "Allowed domains", "list", "Links to these domains are never flagged (for example example.com). Up to 50.",
              maxlen=100, maxitems=50),
        ],
    },
    {
        "id": "joingate", "title": "Join gate", "category": "Security", "icon": "door",
        "desc": "Screen accounts at the door by age and avatar.",
        "get": "get_join_gate_config", "set": "set_join_gate_config",
        "fields": [
            F("enabled", "Join gate", "toggle"),
            F("min_age_days", "Minimum account age (days)", "number", "", min=0, max=365),
            F("block_default_avatar", "Block default avatars", "toggle"),
            F("action", "Action", "select", "", options=opts(("alert", "Alert staff"), ("kick", "Kick"))),
        ],
    },
    {
        "id": "leveling", "title": "Leveling", "category": "Community", "icon": "trophy",
        "desc": "XP, level-up cards and leaderboards.",
        "get": "get_leveling_config", "set": "set_leveling_config",
        "fields": [
            F("announce_channel_id", "Level-up channel", "channel", "Where level-ups are announced.", kind="text"),
            F("xp_rate", "XP speed", "select", "",
              options=opts(("slow", "Slow (0.5x)"), ("default", "Normal (1x)"), ("fast", "Fast (1.5x)"))),
            F("card_style", "Level-up style", "select", "",
              options=opts(("card", "Illustrated card"), ("text", "Text only"), ("off", "No announcement"))),
            F("leaderboard_autopost_channel_id", "Auto-post leaderboard in", "channel", "Leave empty to turn off.", kind="text"),
        ],
    },
    {
        "id": "voicexp", "title": "Voice XP", "category": "Community", "icon": "mic",
        "desc": "Reward time spent in voice channels.",
        "get": "get_voice_xp_config", "set": "set_voice_xp_config", "explicit_kwargs": True,
        "fields": [
            F("enabled", "Voice XP", "toggle"),
            F("xp_per_minute", "XP per minute", "number", "", min=1, max=100),
            F("afk_channel_excluded", "Skip the AFK channel", "toggle"),
        ],
    },
    {
        "id": "starboard", "title": "Starboard", "category": "Community", "icon": "star",
        "desc": "Pin the best messages to a showcase channel.",
        "get": "get_starboard_config", "set": "set_starboard_config", "explicit_kwargs": True,
        "fields": [
            F("channel_id", "Starboard channel", "channel", "Empty turns the starboard off.", kind="text"),
            F("threshold", "Reactions needed", "number", "", min=1, max=50),
            F("emoji", "Emoji", "text", "A single emoji.", maxlen=16),
        ],
    },
    {
        "id": "tickets", "title": "Tickets", "category": "Community", "icon": "ticket",
        "desc": "Private support channels for your members.",
        "get": "get_ticket_config", "set": "set_ticket_config",
        "fields": [
            F("panel_channel_id", "Panel channel", "channel", "Where the Open Ticket button message is posted.", kind="text"),
            F("support_role_id", "Support role", "role", "Can see and answer tickets."),
            F("category_id", "Ticket category", "channel", "New tickets are created here.", kind="category"),
            F("welcome_message", "Opening message", "textarea", "Posted inside each new ticket.", maxlen=1000),
        ],
        "actions": [{"id": "ticket_panel", "label": "Post ticket panel",
                     "help": "Posts the Open Ticket button message in the panel channel (saved settings)."}],
    },
    {
        "id": "suggestions", "title": "Suggestions", "category": "Community", "icon": "bulb",
        "desc": "Where approved suggestions are announced.",
        "get": "get_suggestion_config", "set": "set_suggestion_config", "explicit_kwargs": True,
        "fields": [
            F("approved_log_channel_id", "Approved suggestions channel", "channel", "", kind="text"),
        ],
    },
    {
        "id": "customrole", "title": "Custom role", "category": "Community", "icon": "users",
        "desc": "Let members who bought the perk style their own role.",
        "get": "get_custom_role_config", "set": "set_custom_role_config",
        "fields": [
            F("enabled", "Custom role perk", "toggle", "Turn off to stop members creating or restyling custom roles. Existing roles stay as they are."),
        ],
    },
    {
        "id": "invites", "title": "Invite tracker", "category": "Community", "icon": "link",
        "desc": "See who invited each member.",
        "get": "get_invite_tracker_config", "set": "set_invite_tracker_config",
        "fields": [
            F("enabled", "Invite tracking", "toggle"),
            F("channel_id", "Invite log channel", "channel", "", kind="text"),
            F("leaderboard_autopost_channel_id", "Auto-post leaderboard in", "channel", "Leave empty to turn off.", kind="text"),
        ],
    },
    {
        "id": "birthdays", "title": "Birthdays", "category": "Community", "icon": "heart",
        "desc": "Announce member birthdays in a channel.",
        "get": "get_birthday_config", "set": "set_birthday_config",
        "note": "Members add their own birthday with /birthday. Banner, footer and extra messages are still set in Discord.",
        "fields": [
            F("enabled", "Birthday announcements", "toggle", "Post a message on each member's birthday."),
            F("channel_id", "Announcement channel", "channel", "Where birthdays are announced.", kind="text"),
            F("message", "Message", "textarea", "Leave empty for the default message.", maxlen=500),
            F("ping_role_id", "Role to ping", "role", "Optional. Mentioned in the announcement."),
            F("birthday_role_id", "Birthday role", "role", "Given for the day, then removed.", premium=True),
            F("utc_offset_hours", "Time zone (UTC offset)", "number", "Whole hours from UTC. Ghana is 0.", min=-12, max=14),
            F("announce_hour", "Announce at (hour, 0-23)", "number", "Local hour in the time zone above.", min=0, max=23),
        ],
    },
    {
        "id": "economy", "title": "Economy", "category": "Fun", "icon": "coins",
        "desc": "Your server's currency and how people earn it.",
        "get": "get_economy_config", "set": "set_economy_config",
        "fields": [
            F("currency_name", "Currency name", "text", "", maxlen=24),
            F("currency_symbol", "Currency symbol", "text", "An emoji or short symbol.", maxlen=16),
            F("daily_amount", "Daily reward", "number", "", min=0, max=1000000),
            F("work_min", "Work: minimum", "number", "", min=0, max=1000000),
            F("work_max", "Work: maximum", "number", "", min=0, max=1000000),
            F("beg_min", "Beg: minimum", "number", "", min=0, max=100000),
            F("beg_max", "Beg: maximum", "number", "", min=0, max=100000),
            F("rob_cooldown_hours", "Rob cooldown (hours)", "number", "", min=0, max=168),
            F("rob_success_chance", "Rob success chance (%)", "number", "", min=0, max=100),
            F("vote_bonus_enabled", "Vote bonus", "toggle", "Bonus for voting on Top.gg."),
            F("vote_bonus_amount", "Vote bonus amount", "number", "", min=0, max=1000000),
            F("vote_cooldown_hours", "Vote cooldown (hours)", "number", "", min=1, max=168),
        ],
    },
    {
        "id": "cat", "title": "Cat game", "category": "Fun", "icon": "paw",
        "desc": "Cats that appear in your server for members to adopt, plus cat fights.",
        "get": "get_cat_config", "set": "set_cat_config",
        "note": "Same settings as /catsetup. Free servers get 1 spawn channel and about 120-140 cats a day; Premium gets up to 3 channels and 250-300. Pace can only lower that amount.",
        "fields": [
            F("enabled", "Cat game", "toggle", "Turn automatic cats on or off in this server."),
            F("channel_1", "Spawn channel", "channel", "Where cats appear. The bot needs View Channel, Send Messages and Embed Links there.", kind="text"),
            F("channel_2", "Second spawn channel", "channel", "Premium. Cats rotate between your channels.", kind="text", premium=True),
            F("channel_3", "Third spawn channel", "channel", "Premium.", kind="text", premium=True),
            F("pace_percent", "Cats per day", "select", "A share of your plan's daily amount. You can ask for fewer cats, never more.",
              options=opts(("100", "100% (full amount)"), ("75", "75%"), ("50", "50%"), ("25", "25%"), ("10", "10%")), numeric=True),
            F("active_start", "Cats start at (UTC)", "select", "Spawn hours. The daily amount is squeezed into this window.",
              options=opts(("0", "00:00 UTC"), ("1", "01:00 UTC"), ("2", "02:00 UTC"), ("3", "03:00 UTC"), ("4", "04:00 UTC"), ("5", "05:00 UTC"), ("6", "06:00 UTC"), ("7", "07:00 UTC"), ("8", "08:00 UTC"), ("9", "09:00 UTC"), ("10", "10:00 UTC"), ("11", "11:00 UTC"), ("12", "12:00 UTC"), ("13", "13:00 UTC"), ("14", "14:00 UTC"), ("15", "15:00 UTC"), ("16", "16:00 UTC"), ("17", "17:00 UTC"), ("18", "18:00 UTC"), ("19", "19:00 UTC"), ("20", "20:00 UTC"), ("21", "21:00 UTC"), ("22", "22:00 UTC"), ("23", "23:00 UTC")), numeric=True),
            F("active_end", "Cats stop at (UTC)", "select", "Same as start for the whole day (00:00 to 24:00).",
              options=opts(("1", "01:00 UTC"), ("2", "02:00 UTC"), ("3", "03:00 UTC"), ("4", "04:00 UTC"), ("5", "05:00 UTC"), ("6", "06:00 UTC"), ("7", "07:00 UTC"), ("8", "08:00 UTC"), ("9", "09:00 UTC"), ("10", "10:00 UTC"), ("11", "11:00 UTC"), ("12", "12:00 UTC"), ("13", "13:00 UTC"), ("14", "14:00 UTC"), ("15", "15:00 UTC"), ("16", "16:00 UTC"), ("17", "17:00 UTC"), ("18", "18:00 UTC"), ("19", "19:00 UTC"), ("20", "20:00 UTC"), ("21", "21:00 UTC"), ("22", "22:00 UTC"), ("23", "23:00 UTC"), ("24", "24:00 UTC")), numeric=True),
            F("announce_mode", "Public posts", "select", "How much the bot announces in the channel (adoptions, big moments).",
              options=opts(("all", "Everything"), ("milestones", "Only big moments"), ("off", "Nothing"))),
            F("fights_cross_server", "Cross-server fights", "toggle", "Let requests on your server's fight board be answered from other servers. Play coins only."),
            F("fights_show_server", "Show server name on the board", "toggle", "Show this server's name on the public fight board."),
        ],
    },
    {
        "id": "autopost", "title": "Auto-post", "category": "Community", "icon": "scroll", "no_quick": True,
        "desc": "Post a rotating tip about the bot's features on a schedule.",
        "get": "get_autopost_settings_config", "set": "set_autopost_settings_config",
        "note": "Same settings as /autopost. The tips come from the bot's shared library; you only choose where and how often.",
        "fields": [
            F("enabled", "Auto-post", "toggle", "Post a bot tip in the channel below on a schedule."),
            F("channel_id", "Channel", "channel", "Where the tips are posted. Needed before it can be turned on.", kind="text"),
            F("interval_hours", "Post every (hours)", "number", "1 to 720 hours.", min=1, max=720),
        ],
    },
    {
        "id": "animefeed", "title": "Anime feed", "category": "Fun", "icon": "mega",
        "desc": "Post new anime and donghua episodes in a channel as they air.",
        "get": "get_anime_feed_config", "set": "set_anime_feed_config",
        "note": "Only episodes that air after you turn this on are posted. The bot needs Send Messages and Embed Links in the channel, "
                "and a ping role must be mentionable. Changes can take up to 30 minutes to reach the posting worker.",
        "fields": [
            F("enabled", "Anime feed", "toggle", "Needs a channel. Stays off until one is saved."),
            F("channel_id", "Feed channel", "channel", "Where new episodes are posted.", kind="text"),
            F("ping_role_id", "Role to ping", "role", "Optional. Mentioned on each new episode."),
            F("content_kinds", "What to post", "select", "Which kinds of shows appear.",
              options=opts(("both", "Anime and donghua"), ("anime", "Anime only"), ("donghua", "Donghua only"))),
        ],
    },
    {
        "id": "bumpnet", "title": "Bump network", "category": "Community", "icon": "link",
        "desc": "Receive bump posts from other servers and choose which ones.",
        "get": "get_bump_settings_config", "set": "set_bump_settings_config", "no_quick": True,
        "note": "Same settings as /bumpsetup. The owner can switch the whole bump feature off; if so nothing is posted whatever you set here.",
        "fields": [
            F("receives_bumps", "Receive bumps", "toggle", "Let the bump network post in your bump channel."),
            F("bump_channel_id", "Bump channel", "channel", "Where incoming bumps are posted. Pick a channel to change it.", kind="text"),
            F("language", "Language filter", "select", "Only receive bumps in this language.",
              options=opts(("any", "Any language"), ("en", "English"), ("fr", "French"), ("es", "Spanish"),
                           ("pt", "Portuguese"), ("ar", "Arabic"))),
            F("nsfw_opt_in", "Allow NSFW listings", "toggle", "Off keeps 18+ servers out of your bump channel."),
            F("intensity_level", "Intensity", "select", "How many incoming bumps you want.",
              options=opts(("1", "1 - Low"), ("2", "2 - Light"), ("3", "3 - Normal"), ("4", "4 - Frequent"), ("5", "5 - High"))),
        ],
    },
]


BIRTHDAY_MODULE: dict = {
    "id": "birthdays", "title": "Birthdays", "category": "Community", "icon": "star",
    "desc": "Announce member birthdays once a day. Members add their own date with /birthday.",
    "get": "get_birthday_config", "set": "set_birthday_config",
    "note": "Members set their own birthday in Discord (month and day only, no year). Premium adds a styled announcement, a birthday role and extra messages.",
    "fields": [
        F("enabled", "Birthday announcements", "toggle", "Needs a channel."),
        F("channel_id", "Announcement channel", "channel", "Where birthdays are announced.", kind="text"),
        F("ping_role_id", "Role to ping", "role", "Optional. Pinged with the announcement."),
        F("message", "Message", "textarea", "Placeholders: {user}, {name}, {server}. Empty uses the default.", maxlen=500),
        F("utc_offset_hours", "Time zone (UTC offset)", "number", "Whole hours, -12 to 14. Ghana is 0.", min=-12, max=14),
        F("announce_hour", "Announce at hour", "number", "Local hour of day, 0 to 23.", min=0, max=23),
        F("embed_title", "Title", "text", "Premium: title of the styled announcement.", maxlen=80, premium=True),
        F("embed_color", "Colour", "color", "Premium. Clear the box for the default pink.", as_int=True, premium=True),
        F("embed_image_url", "Banner image", "text", "Premium: an https link ending in .png, .jpg, .gif or .webp.", maxlen=300, https_only=True, premium=True),
        F("embed_footer", "Footer", "text", "Premium.", maxlen=100, premium=True),
        F("show_avatar", "Show their avatar", "toggle", "Premium: the birthday person's picture on the announcement.", null_true=True, premium=True),
        F("birthday_role_id", "Birthday role", "role", "Premium: given for the day, removed the next day. The bot's role must sit above it.", premium=True),
        F("extra_messages", "More messages", "list", "Premium: up to 5, one per line. One is picked at random each day.", maxlen=300, maxitems=5, premium=True),
    ],
}
MODULES.append(BIRTHDAY_MODULE)

BY_ID: Dict[str, dict] = {m["id"]: m for m in MODULES}
CATEGORIES = ["Onboarding", "Security", "Community", "Fun"]


def public_schema() -> dict:
    """What the browser needs to render forms: no DB method names."""
    mods = []
    for m in MODULES:
        mods.append({k: v for k, v in m.items() if k not in ("get", "set", "explicit_kwargs")})
    return {"categories": CATEGORIES, "modules": mods}


# ───────────────────────── permissions ─────────────────────────

def can_manage(owner_id: Optional[int], user_id: int, member_role_ids: List[int],
               roles_by_id: Dict[int, int], guild_id: int) -> bool:
    """roles_by_id maps role id -> permission bitfield. Mirrors Discord's own rule:
    owner, or Administrator, or Manage Server."""
    if owner_id is not None and int(owner_id) == int(user_id):
        return True
    perms = int(roles_by_id.get(int(guild_id), 0))
    for rid in member_role_ids:
        perms |= int(roles_by_id.get(int(rid), 0))
    return bool(perms & (ADMINISTRATOR | MANAGE_GUILD))


KICK_MEMBERS = 0x2
MODERATE_MEMBERS = 1 << 40        # "Timeout Members": what /warn and /unwarn require in Discord
BAN_MEMBERS = 0x4
RAID_REASON_PREFIX = "[anti-raid]"          # same marker modules/antiraid_pro.py writes
RAID_OPS = ("approve", "kick", "ban")


def has_permission(owner_id: Optional[int], user_id: int, member_role_ids: List[int],
                   roles_by_id: Dict[int, int], guild_id: int, mask: int) -> bool:
    """Owner, Administrator, or any bit in `mask` held through @everyone or the member's roles."""
    if owner_id is not None and int(owner_id) == int(user_id):
        return True
    perms = int(roles_by_id.get(int(guild_id), 0))
    for rid in member_role_ids:
        perms |= int(roles_by_id.get(int(rid), 0))
    return bool(perms & (ADMINISTRATOR | mask))


def top_position(member_role_ids: List[int], positions: Dict[int, int]) -> int:
    return max([int(positions.get(int(r), 0)) for r in member_role_ids] or [0])


def raid_op_allowed(op: str, may_review: bool, may_ban: bool, may_kick: bool) -> Optional[str]:
    """None when allowed, else the reason. Mirrors the in-Discord review: releasing needs Ban
    Members or Manage Server; banning needs Ban Members; kicking needs Kick Members."""
    if op not in RAID_OPS:
        return "Unknown action."
    if op == "approve" and not (may_review or may_ban):
        return "You need Manage Server or Ban Members to release people."
    if op == "ban" and not may_ban:
        return "You need the Ban Members permission to ban people."
    if op == "kick" and not may_kick:
        return "You need the Kick Members permission to kick people."
    return None


def is_raid_row(row: dict) -> bool:
    return str(row.get("reason") or "").startswith(RAID_REASON_PREFIX)


def snowflake_ms(user_id: int) -> int:
    return (int(user_id) >> 22) + 1420070400000


def guild_list_manageable(guilds: List[dict]) -> List[dict]:
    """From Discord's /users/@me/guilds payload: only servers the user can manage."""
    out = []
    for g in guilds or []:
        try:
            perms = int(g.get("permissions") or 0)
        except (TypeError, ValueError):
            perms = 0
        if g.get("owner") or perms & (ADMINISTRATOR | MANAGE_GUILD):
            out.append({"id": str(g["id"]), "name": g.get("name") or "Server", "icon": g.get("icon"),
                        "owner": bool(g.get("owner"))})
    return out


# ───────────────────────── values in / out ─────────────────────────

def export_values(module: dict, cfg: dict) -> dict:
    """DB row -> JSON-safe dict of just this module's fields. Snowflakes become strings
    (JavaScript numbers lose precision above 2^53)."""
    out = {}
    for f in module["fields"]:
        v = cfg.get(f["key"])
        if f["type"] in ("channel", "role"):
            v = str(v) if v else None
        elif f["type"] == "list":
            v = list(v or [])
        elif f["type"] == "toggle":
            v = True if (v is None and f.get("null_true")) else bool(v)
        elif f["type"] == "color" and f.get("as_int"):
            v = "#%06x" % int(v) if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 0xFFFFFF else None
        elif f["type"] == "select" and f.get("null_zero") and v is None:
            v = 0
        out[f["key"]] = v
    return out


class ValidationError(ValueError):
    pass


def _coerce(f: dict, raw: Any, channels: Dict[str, set], roles: set) -> Any:
    t, label = f["type"], f["label"]
    if t == "toggle":
        if not isinstance(raw, bool):
            raise ValidationError(f"{label}: must be on or off.")
        return raw
    if t == "number":
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise ValidationError(f"{label}: enter a number.")
        try:
            n = int(float(raw))
        except ValueError:
            raise ValidationError(f"{label}: enter a number.")
        if n < f["min"] or n > f["max"]:
            raise ValidationError(f"{label}: must be between {f['min']} and {f['max']}.")
        return n
    if t == "select":
        for value, _ in f["options"]:
            if str(value) == str(raw):
                n = int(value) if f.get("numeric") else value
                return None if (f.get("null_zero") and n == 0) else n
        raise ValidationError(f"{label}: pick one of the listed options.")
    if t in ("text", "textarea"):
        if raw is None:
            raw = ""
        if not isinstance(raw, str):
            raise ValidationError(f"{label}: must be text.")
        s = raw.strip()
        if len(s) > f.get("maxlen", 200):
            raise ValidationError(f"{label}: keep it under {f.get('maxlen', 200)} characters.")
        if s and f.get("https_only") and (not s.lower().startswith("https://") or any(c.isspace() for c in s)):
            raise ValidationError(f"{label}: use a direct https:// link.")
        return s if s else None
    if t == "color":
        if f.get("as_int"):
            if raw in (None, ""):
                return None
            if not isinstance(raw, str) or not _HEX.match(raw):
                raise ValidationError(f"{label}: use a colour like #5865F2.")
            return int(raw[1:], 16)
        if not isinstance(raw, str) or not _HEX.match(raw):
            raise ValidationError(f"{label}: use a colour like #5865F2.")
        return raw.lower()
    if t == "list":
        if not isinstance(raw, list):
            raise ValidationError(f"{label}: must be a list.")
        items, seen = [], set()
        for x in raw:
            if not isinstance(x, str):
                raise ValidationError(f"{label}: entries must be text.")
            s = x.strip()
            if not s or s.lower() in seen:
                continue
            if len(s) > f.get("maxlen", 60):
                raise ValidationError(f"{label}: each entry must be under {f.get('maxlen', 60)} characters.")
            seen.add(s.lower())
            items.append(s)
        if len(items) > f.get("maxitems", 200):
            raise ValidationError(f"{label}: at most {f.get('maxitems', 200)} entries.")
        return items
    if t in ("channel", "role"):
        if raw in (None, ""):
            return None
        s = str(raw)
        if not s.isdigit():
            raise ValidationError(f"{label}: pick one from the list.")
        pool = roles if t == "role" else channels.get(f.get("kind", "text"), set())
        if s not in pool:
            raise ValidationError(f"{label}: that {t} isn't in this server.")
        return int(s)
    raise ValidationError(f"{label}: unsupported field.")


def validate_values(module: dict, values: Any, channels: Dict[str, set], roles: set,
                    is_premium: bool) -> Tuple[Dict[str, Any], List[str]]:
    """Only keys present in `values` are validated and returned, so the browser can
    send just what changed. Unknown keys are rejected, never silently written."""
    clean, errors = {}, []
    if not isinstance(values, dict) or not values:
        return {}, ["Nothing to save."]
    fields = {f["key"]: f for f in module["fields"]}
    for key, raw in values.items():
        f = fields.get(key)
        if f is None:
            errors.append(f"Unknown setting: {str(key)[:40]}")
            continue
        try:
            v = _coerce(f, raw, channels, roles)
        except ValidationError as e:
            errors.append(str(e))
            continue
        if not is_premium:
            if f.get("premium") or (v in (f.get("premium_values") or [])):
                errors.append(f"{f['label']}: this needs Premium.")
                continue
        clean[key] = v
    if not errors:
        for lo, hi in (("work_min", "work_max"), ("beg_min", "beg_max")):
            if module["id"] == "economy" and lo in clean and hi in clean and clean[lo] > clean[hi]:
                errors.append("Minimum can't be higher than maximum.")
    return clean, errors


# ───────────────────────── reset to defaults + import / export ─────────────────────────

EXPORT_FORMAT = "prime-bot-settings"
EXPORT_VERSION = 1
IMPORT_MAX_BYTES = 48 * 1024


def default_values(module: dict, default_cfg: dict) -> Tuple[Dict[str, Any], List[str]]:
    """Values that put a module back to factory settings. `default_cfg` is what the module's
    getter returns for a server with no saved row. A field whose default can't be expressed
    through the dashboard (e.g. an unset number) is left alone and named in the second item."""
    exported = export_values(module, default_cfg or {})
    clean, left = {}, []
    for f in module["fields"]:
        try:
            clean[f["key"]] = _coerce(f, exported.get(f["key"]), {}, set())
        except ValidationError:
            left.append(f["label"])
    return clean, left


def export_payload(module: dict, cfg: dict, exported_at: str) -> dict:
    """The JSON a user downloads. Only declared keys, same shape the dashboard saves."""
    return {"format": EXPORT_FORMAT, "version": EXPORT_VERSION, "module": module["id"],
            "exported_at": exported_at, "values": export_values(module, cfg)}


def import_values(module: dict, payload: Any, channels: Dict[str, set], roles: set,
                  is_premium: bool) -> Tuple[Dict[str, Any], List[str], Optional[str]]:
    """Check an uploaded settings file against THIS server. Returns (values, skipped, error).

    Nothing is written: the dashboard loads `values` into the form as unsaved changes, and
    the normal save path validates them again. Anything that doesn't fit this server (a
    channel or role from another server, a Premium option on a free server, an out-of-range
    number, a key this version doesn't know) is skipped and named, never guessed at."""
    if not isinstance(payload, dict) or payload.get("format") != EXPORT_FORMAT:
        return {}, [], "That isn't a PRIME BOT settings file."
    if payload.get("version") != EXPORT_VERSION:
        return {}, [], "That settings file is from a different version."
    if payload.get("module") != module["id"]:
        return {}, [], f"That file is for a different module ({str(payload.get('module'))[:40]}), not {module['title']}."
    values = payload.get("values")
    if not isinstance(values, dict) or not values:
        return {}, [], "That settings file is empty."
    fields = {f["key"]: f for f in module["fields"]}
    clean, skipped = {}, []
    for key, raw in values.items():
        f = fields.get(key)
        if f is None:
            skipped.append(f"{str(key)[:40]}: not a setting here")
            continue
        try:
            v = _coerce(f, raw, channels, roles)
        except ValidationError as e:
            skipped.append(str(e))
            continue
        if not is_premium and (f.get("premium") or v in (f.get("premium_values") or [])):
            skipped.append(f"{f['label']}: needs Premium")
            continue
        clean[key] = v
    if not clean:
        return {}, skipped, "Nothing in that file fits this server."
    return clean, skipped, None


# ───────────────────────── drop box (owner -> every dashboard admin) ─────────────────────────

DROPBOX_KINDS = ("info", "update", "warning", "maintenance")
DROPBOX_TITLE_MAX, DROPBOX_BODY_MAX = 100, 2000
DROPBOX_AUDIENCES = ("all", "premium", "min_members", "guild")   # who gets DM-pushed / sees a targeted message


def validate_dropbox(raw: Any) -> Tuple[Optional[dict], Optional[str]]:
    """Validate an owner's drop box message. Returns (clean, error)."""
    if not isinstance(raw, dict):
        return None, "Invalid message."
    title = str(raw.get("title") or "").strip()
    body = str(raw.get("body") or "").strip()
    kind = str(raw.get("kind") or "info")
    if not title:
        return None, "Title is required."
    if len(title) > DROPBOX_TITLE_MAX:
        return None, f"Title must be at most {DROPBOX_TITLE_MAX} characters."
    if not body:
        return None, "Message is required."
    if len(body) > DROPBOX_BODY_MAX:
        return None, f"Message must be at most {DROPBOX_BODY_MAX} characters."
    if kind not in DROPBOX_KINDS:
        return None, "Unknown message type."
    hours = raw.get("expires_hours")
    if hours in (None, ""):
        hours = None
    else:
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 1 <= hours <= 24 * 90:
            return None, "Expiry must be between 1 hour and 90 days."
        hours = int(hours)
    audience = str(raw.get("audience") or "all")
    if audience not in DROPBOX_AUDIENCES:
        return None, "Unknown audience."
    min_members, target_guild = None, None
    if audience == "min_members":
        mm = raw.get("min_members")
        if isinstance(mm, bool) or not isinstance(mm, (int, float)) or not 1 <= mm <= 10_000_000:
            return None, "Minimum members must be between 1 and 10,000,000."
        min_members = int(mm)
    if audience == "guild":
        gid = str(raw.get("target_guild_id") or "").strip()
        if not gid.isdigit() or not 15 <= len(gid) <= 20:
            return None, "Enter a valid server ID."
        target_guild = int(gid)
    return {"title": title, "body": body, "kind": kind,
            "announce": bool(raw.get("announce")), "expires_hours": hours,
            "push_dm": bool(raw.get("push_dm")), "audience": audience,
            "min_members": min_members, "target_guild_id": target_guild}, None


def format_dropbox_dm(title: str, body: str, kind: str) -> str:
    """Discord DM text for a drop box message (Discord's limit is 2000 characters)."""
    icon = {"info": "📢", "update": "✨", "warning": "⚠️", "maintenance": "🛠️"}.get(kind, "📢")
    head = f"{icon} **{title}**\n\n"
    tail = "\n\n_Sent from the PRIME BOT dashboard._"
    room = 2000 - len(head) - len(tail)
    if len(body) > room:
        body = body[:max(0, room - 1)] + "…"
    return head + body + tail


# ───────────────────────── audit log ─────────────────────────

AUDIT_VALUE_MAX = 200
AUDIT_RETENTION_DAYS = 180


def _audit_val(v: Any) -> Any:
    if isinstance(v, str):
        return v if len(v) <= AUDIT_VALUE_MAX else v[:AUDIT_VALUE_MAX] + "…"
    if isinstance(v, list):
        return [_audit_val(x) for x in v[:50]]
    return v


def diff_values(module: dict, before: dict, after: dict) -> Dict[str, dict]:
    """{key: {"from": old, "to": new}} for declared fields whose value changed.
    Only schema keys are ever recorded, and long text is truncated."""
    out = {}
    for f in module["fields"]:
        k = f["key"]
        if before.get(k) != after.get(k):
            out[k] = {"from": _audit_val(before.get(k)), "to": _audit_val(after.get(k))}
    return out


# ───────────────────────── bot actions (the bot posts something) ─────────────────────────

BOT_ACTIONS = {"verify_panel": "verification", "ticket_panel": "tickets", "welcome_test": "welcome"}


RAID_REASON_MAX = 160


def raid_row_view(row: dict, member: Optional[dict]) -> dict:
    """One quarantined person as the review screen shows it. Text is plain; the page renders it
    with textContent. `member` is Discord's guild-member object, or None if they left."""
    uid = int(row["user_id"])
    user = (member or {}).get("user") or {}
    name = user.get("global_name") or user.get("username") or "Unknown user"
    avatar = (f"https://cdn.discordapp.com/avatars/{uid}/{user['avatar']}.png?size=64" if user.get("avatar")
              else f"https://cdn.discordapp.com/embed/avatars/{(uid >> 22) % 6}.png")
    created = row.get("created_at")
    reason = str(row.get("reason") or "")[len(RAID_REASON_PREFIX):].strip()
    return {"user_id": str(uid), "name": str(name)[:40], "avatar_url": avatar, "in_server": member is not None,
            "account_created_ms": snowflake_ms(uid), "reason": reason[:RAID_REASON_MAX],
            "quarantined_at": created.isoformat() if hasattr(created, "isoformat") else None,
            "roles_held": len(row.get("saved_role_ids") or [])}
# ───────────────────────── scheduled messages (same table the /schedule command uses) ─────────────────────────

SCHEDULE_MODES = ("once", "interval", "daily")
SCHEDULE_TEXT_MAX = 2000
SCHEDULE_MIN_INTERVAL_MIN = 5            # the command allows 1m; the dashboard is stricter to avoid channel spam
SCHEDULE_MAX_INTERVAL_MIN = 60 * 24 * 365
SCHEDULE_MAX_DELAY_MIN = 60 * 24 * 365
SCHEDULE_MAX_ACTIVE = 25
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def validate_schedule(raw: Any, text_channels: set, now) -> Tuple[Optional[dict], Optional[str]]:
    """-> ({channel_id, content, run_at, interval_seconds}, None) or (None, error).
    `now` is a timezone-aware datetime (injected so this stays pure and testable)."""
    from datetime import timedelta
    if not isinstance(raw, dict):
        return None, "Invalid request."
    cid = str(raw.get("channel_id") or "")
    if not cid.isdigit() or cid not in text_channels:
        return None, "Pick a text channel from this server."
    text = raw.get("content")
    if not isinstance(text, str) or not text.strip():
        return None, "Write the message to post."
    text = text.strip()
    if len(text) > SCHEDULE_TEXT_MAX:
        return None, f"Keep the message under {SCHEDULE_TEXT_MAX} characters."
    mode = raw.get("mode")

    def minutes(key, label, lo, hi):
        v = raw.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            return None, f"{label}: enter a number."
        try:
            n = int(float(v))
        except ValueError:
            return None, f"{label}: enter a number."
        if n < lo or n > hi:
            return None, f"{label}: must be between {lo} and {hi} minutes."
        return n, None

    if mode == "once":
        n, err = minutes("minutes", "Delay", 1, SCHEDULE_MAX_DELAY_MIN)
        if err:
            return None, err
        return {"channel_id": int(cid), "content": text, "run_at": now + timedelta(minutes=n), "interval_seconds": None}, None
    if mode == "interval":
        n, err = minutes("minutes", "Repeat every", SCHEDULE_MIN_INTERVAL_MIN, SCHEDULE_MAX_INTERVAL_MIN)
        if err:
            return None, err
        return {"channel_id": int(cid), "content": text, "run_at": now + timedelta(minutes=n), "interval_seconds": n * 60}, None
    if mode == "daily":
        m = _TIME_RE.match(str(raw.get("time_utc") or "").strip())
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            return None, "Use a 24-hour UTC time like 09:00."
        run_at = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if run_at <= now:
            run_at += timedelta(days=1)
        return {"channel_id": int(cid), "content": text, "run_at": run_at, "interval_seconds": 86400}, None
    return None, "Pick once, repeating or daily."


ANNOUNCEMENT_MAX_ACTIVE = 25


def validate_announcement(raw: Any, text_channels: set, now) -> Tuple[Optional[dict], Optional[str]]:
    """Same checks as a scheduled message (channel, text, once/repeat/daily). -> ({channel_id, message, run_at,
    interval_minutes}, None) or (None, error). The /announce command repeats in whole minutes."""
    if isinstance(raw, dict) and raw.get("mode") == "daily":
        return None, "Announcements can post once or repeat every few minutes. For a fixed daily time use a scheduled message."
    clean, err = validate_schedule(raw, text_channels, now)
    if err:
        return None, err
    sec = clean["interval_seconds"]
    return {"channel_id": clean["channel_id"], "message": clean["content"], "run_at": clean["run_at"],
            "interval_minutes": (sec // 60) if sec else None}, None


def announcement_row_view(row: dict) -> dict:
    """An announcement row in the same shape as a scheduled message, so the page renders both alike."""
    iv = row.get("interval_minutes")
    return schedule_row_view({"id": row.get("id"), "channel_id": row.get("channel_id"), "content": row.get("message"),
                              "next_run_at": row.get("next_run_at"), "interval_seconds": int(iv) * 60 if iv else None,
                              "enabled": row.get("active", True), "created_by": row.get("created_by")})


def schedule_row_view(row: dict) -> dict:
    iso = lambda d: d.isoformat() if hasattr(d, "isoformat") else None
    iv = row.get("interval_seconds")
    return {"id": str(row["id"]), "channel_id": str(row["channel_id"]), "content": str(row.get("content") or "")[:SCHEDULE_TEXT_MAX],
            "next_run_at": iso(row.get("next_run_at")), "interval_seconds": int(iv) if iv else None,
            "enabled": bool(row.get("enabled")), "created_by": str(row.get("created_by")) if row.get("created_by") else None}
TICKET_STATUSES = ("open", "closed")
TICKET_MSG_MAX = 2000
TICKET_HISTORY_MAX = 500


def ticket_row_view(row: dict) -> dict:
    iso = lambda d: d.isoformat() if hasattr(d, "isoformat") else None
    return {"id": str(row["id"]), "opener_id": str(row["opener_id"]),
            "claimed_by": str(row["claimed_by"]) if row.get("claimed_by") else None,
            "status": row.get("status") if row.get("status") in TICKET_STATUSES else "open",
            "created_at": iso(row.get("created_at")), "closed_at": iso(row.get("closed_at"))}


def ticket_message_view(msg: dict) -> dict:
    """One Discord message as the transcript viewer shows it: plain text only, bounded."""
    author = msg.get("author") or {}
    return {"id": str(msg.get("id")), "at": msg.get("timestamp"),
            "author": str(author.get("global_name") or author.get("username") or "Unknown")[:40],
            "bot": bool(author.get("bot")),
            "text": str(msg.get("content") or "")[:TICKET_MSG_MAX],
            "files": [str(a.get("filename") or "file")[:80] for a in (msg.get("attachments") or [])][:10],
            "embeds": len(msg.get("embeds") or [])}


MOD_REASON_MAX = 300
MOD_KIND_RE = re.compile(r"^[a-z_]{1,32}$")


def _clean_text(raw, limit: int) -> str:
    """Moderator-typed text shown back to admins: control characters out, bounded. The page also uses textContent."""
    return "".join(ch for ch in str(raw or "") if ch == "\n" or ch >= " ")[:limit]


def mod_case_view(row: dict) -> dict:
    iso = lambda d: d.isoformat() if hasattr(d, "isoformat") else None
    return {"id": str(row["id"]), "kind": str(row.get("action_type") or "")[:32],
            "target_id": str(row["target_user_id"]) if row.get("target_user_id") else None,
            "by": str(row["performed_by"]) if row.get("performed_by") else None,
            "reason": _clean_text(row.get("reason"), MOD_REASON_MAX), "at": iso(row.get("created_at"))}


ANALYTICS_DAYS = (7, 30, 90)


def parse_analytics_days(raw) -> int:
    """Window for the analytics page: only 7, 30 or 90 days. Empty means 30."""
    if raw in (None, ""):
        return 30
    try:
        n = int(str(raw).strip())
    except ValueError:
        raise ValueError("Invalid time range.")
    if n not in ANALYTICS_DAYS:
        raise ValueError("Invalid time range.")
    return n


def analytics_view(data: dict, members) -> dict:
    """Shape db.dash_analytics output for JSON: snowflakes as strings, ints as ints, nothing else passes through."""
    iso = lambda d: d.isoformat() if hasattr(d, "isoformat") else None
    num = lambda v: int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0
    series = [{"day": str(r.get("day"))[:10], "joined": num(r.get("joined")), "left": num(r.get("left"))}
              for r in data.get("series", [])]
    return {"days": num(data.get("days")), "members": members if isinstance(members, int) else None, "series": series,
            "joined": sum(r["joined"] for r in series), "left": sum(r["left"] for r in series),
            "tracking_since": iso(data.get("tracking_since")), "ranked": num(data.get("ranked")),
            "active": {"d1": num(data.get("active_1d")), "d7": num(data.get("active_7d")), "d30": num(data.get("active_30d"))},
            "top_members": [{"id": str(r["user_id"]), "xp": num(r.get("total_xp")), "level": num(r.get("level"))}
                            for r in data.get("top_members", [])],
            "top_inviters": [{"id": str(r["user_id"]), "joins": num(r.get("joins")), "net": num(r.get("net"))}
                             for r in data.get("top_inviters", [])]}


def mod_warn_view(row: dict) -> dict:
    iso = lambda d: d.isoformat() if hasattr(d, "isoformat") else None
    return {"id": str(row["id"]), "by": str(row["warned_by"]) if row.get("warned_by") else None,
            "reason": _clean_text(row.get("reason"), MOD_REASON_MAX), "at": iso(row.get("created_at"))}


def parse_warn_id(raw):
    """One warn id (user_warns.id is a 32-bit SERIAL). Returns int or raises ValueError. Digits only, no signs or spaces inside."""
    t = str(raw if raw is not None else "").strip()
    if not t.isdigit() or not 0 < int(t) < 2 ** 31:
        raise ValueError("That warn isn't valid.")
    return int(t)


def parse_mod_user(raw):
    """A Discord snowflake (10 to 20 digits that fit a BIGINT) or None. Raises ValueError on garbage."""
    if raw in (None, ""):
        return None
    s = str(raw).strip()
    if not (s.isdigit() and 10 <= len(s) <= 20 and int(s) < 2 ** 63):
        raise ValueError("Enter a Discord user ID (10 to 20 digits).")
    return int(s)


GIVEAWAY_STATUSES = ("active", "ended")
GIVEAWAY_PRIZE_MAX = 200


def giveaway_view(row: dict) -> dict:
    """One giveaway for the web page. Snowflakes are strings; entrant ids are never sent, only a count."""
    iso = lambda d: d.isoformat() if hasattr(d, "isoformat") else None
    entrants = row.get("entrant_ids")
    return {"id": str(row["id"]), "channel_id": str(row["channel_id"]) if row.get("channel_id") else None,
            "message_id": str(row["message_id"]) if row.get("message_id") else None,
            "host_id": str(row["host_id"]) if row.get("host_id") else None,
            "prize": _clean_text(row.get("prize"), GIVEAWAY_PRIZE_MAX), "winner_count": int(row.get("winner_count") or 1),
            "entrants": int(row["entrant_count"]) if row.get("entrant_count") is not None else len(entrants or []),
            "winner_ids": [str(w) for w in (row.get("winner_ids") or [])][:50],
            "status": row.get("status") if row.get("status") in GIVEAWAY_STATUSES else "ended",
            "ends_at": iso(row.get("ends_at"))}


# ───────────────────────── autoresponders (trigger -> reply pairs) ─────────────────────────

AUTORESPONDER_MAX = 25            # the command has no cap; the dashboard is stricter to keep the per-message scan short
AUTORESPONDER_TRIGGER_MAX = 100
AUTORESPONDER_RESPONSE_MAX = 1500
_MASS_MENTION_RE = re.compile(r"@(everyone|here)\b|<@&\d+>", re.IGNORECASE)


def validate_autoresponder(raw: Any, existing_triggers=()) -> Tuple[Optional[dict], Optional[str]]:
    """-> ({trigger, response}, None) or (None, error). The trigger is stored lower-case, like /autoresponder add.
    Mass mentions (@everyone, @here, role pings) are refused so a reply can't be used to ping a whole server."""
    if not isinstance(raw, dict):
        return None, "Nothing to save."
    trigger, response = raw.get("trigger"), raw.get("response")
    if not isinstance(trigger, str) or not isinstance(response, str):
        return None, "Enter a trigger and a reply."
    trigger, response = trigger.strip().lower(), response.strip()
    if not trigger or not response:
        return None, "Enter a trigger and a reply."
    if len(trigger) > AUTORESPONDER_TRIGGER_MAX:
        return None, f"The trigger must be under {AUTORESPONDER_TRIGGER_MAX} characters."
    if len(response) > AUTORESPONDER_RESPONSE_MAX:
        return None, f"The reply must be under {AUTORESPONDER_RESPONSE_MAX} characters."
    if _MASS_MENTION_RE.search(response):
        return None, "Replies can't contain @everyone, @here or role mentions."
    if trigger in {str(t).lower() for t in existing_triggers}:
        return None, "That trigger already exists."
    return {"trigger": trigger, "response": response}, None


def autoresponder_row_view(row: dict) -> dict:
    return {"id": str(row["id"]), "trigger": str(row.get("trigger") or "")[:AUTORESPONDER_TRIGGER_MAX],
            "response": str(row.get("response") or "")[:AUTORESPONDER_RESPONSE_MAX],
            "created_by": str(row["created_by"]) if row.get("created_by") else None}


# ───────────────────────── applications (read-only) ─────────────────────────

APPLICATION_STATUSES = ("pending", "accepted", "declined")
APPLICATION_ANSWER_MAX = 1000


def application_form_view(f: dict) -> dict:
    iso = lambda d: d.isoformat() if hasattr(d, "isoformat") else None
    return {"id": str(f["id"]), "title": str(f.get("title") or "")[:100], "status": f.get("status"),
            "pending": int(f.get("pending") or 0), "questions": [str(q)[:100] for q in (f.get("questions") or [])][:10],
            "review_channel_id": str(f["review_channel_id"]) if f.get("review_channel_id") else None,
            "accept_role_id": str(f["accept_role_id"]) if f.get("accept_role_id") else None,
            "updated_at": iso(f.get("updated_at"))}


def application_submission_view(r: dict) -> dict:
    iso = lambda d: d.isoformat() if hasattr(d, "isoformat") else None
    return {"id": str(r["id"]), "form_id": str(r["form_id"]), "user_id": str(r["user_id"]),
            "status": r.get("status") if r.get("status") in APPLICATION_STATUSES else "pending",
            "answers": [{"q": str(a.get("q", ""))[:100], "a": str(a.get("a", ""))[:APPLICATION_ANSWER_MAX]}
                        for a in (r.get("answers") or []) if isinstance(a, dict)][:10],
            "decided_by": str(r["decided_by"]) if r.get("decided_by") else None,
            "created_at": iso(r.get("created_at")), "decided_at": iso(r.get("decided_at"))}


# ───────────────────────── quarantine role (needs the bot's role hierarchy) ─────────────────────────

MANAGE_ROLES = 0x10000000


def quarantine_role_problem(guild_id, role_id, roles: list, bot_role_ids: list):
    """Same rules as modules.server_panel_quarantine.role_problem, from Discord's raw role objects.
    roles: [{id, position, managed, permissions}]; bot_role_ids: the bot member's role ids. -> reason or None."""
    by_id = {str(r["id"]): r for r in roles}
    role = by_id.get(str(role_id))
    if role is None:
        return "The quarantine role was deleted. Pick a new one."
    mine = [by_id[str(i)] for i in bot_role_ids if str(i) in by_id]
    everyone = by_id.get(str(guild_id))
    perms = 0
    for r in mine + ([everyone] if everyone else []):
        try:
            perms |= int(r.get("permissions") or 0)
        except (TypeError, ValueError):
            pass
    if not (perms & MANAGE_ROLES or perms & ADMINISTRATOR):
        return "I need the Manage Roles permission to quarantine anyone."
    if role.get("managed") or str(role_id) == str(guild_id):
        return "That role can't be handed out by the bot. Pick a normal role."
    if not mine:
        return "Move my role above the quarantine role in Server Settings > Roles."
    top = max(mine, key=lambda r: (int(r.get("position") or 0), -int(r["id"])))
    below = (int(role.get("position") or 0), -int(role["id"])) < (int(top.get("position") or 0), -int(top["id"]))
    return None if below else "Move my role above the quarantine role in Server Settings > Roles."
