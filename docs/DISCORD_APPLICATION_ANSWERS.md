# Discord application answers (Message Content and Server Members intents)

These match what the code now does (branch fix/privacy-retention-mydata).

**Why do you need the Message Content intent?**
The bot reads message text only while a message is being processed: to award XP, to run server-configured auto-moderation (blocked words, invite links, scam links, repeated-message spam) and to answer prefix commands and triggers an admin set up. The text is not stored in our database. The spam filter keeps a short one-way hash (not the words) for under 10 minutes to detect repeats. When Scam Shield removes a known scam message, a copy goes to a private Discord channel run by the operator so the scam rules can be improved. Message content is never used to train AI or machine learning models.

**Why do you need the Server Members intent?**
To welcome new members, run verification and the join gate, assign roles, track which invite was used (only if an admin turns it on), and look up members for moderation and leveling features.

**What do you store, and for how long?**
Discord IDs for features a person uses (XP, coins, warns, settings). Short-lived records are purged automatically: flood hashes after 10 minutes, AI command logs after 90 days, Scam Shield hit records (no message text) after 180 days, listing visit hashes after 30 days.

**How can people delete their data?**
/mydata (server or DM) shows what is held and deletes it after typing DELETE MY DATA. Payments, plans, usage counters and moderation records are kept. Policy: https://prime-bot-site.pages.dev/privacy/
