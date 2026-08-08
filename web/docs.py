"""Documentation content, kept as data so the page stays consistent and is easy to amend.

This is written by hand rather than read off the bot's command tree. The two processes don't
share memory, and importing the cogs here would drag discord.py and a Mongo connection into
the web dyno for the sake of a help page. The cost is that this needs updating when commands
change; the SECTIONS below are the single place to do it.
"""

# (permission needed, label) so the same wording is used everywhere.
EVERYONE = "Anyone"
MANAGE_SERVER = "Manage Server"
MANAGE_ROLES = "Manage Roles"

SETUP = [
    {
        "title": "Add the bot",
        "body": "Use the Add to Discord button at the top of this page. Discord will ask which "
                "server, and you need Manage Server there to add it. Accept the permissions it "
                "asks for: they are what the logging and moderation features need to work.",
    },
    {
        "title": "Turn on what you want",
        "body": "Nothing runs until you switch it on. Either use the dashboard, which is the "
                "quicker route for most things, or the commands listed below. Every feature is "
                "independent, so you can use the ratings without the moderation, or the other "
                "way round.",
    },
    {
        "title": "Check it worked",
        "body": "Most features have a status command that tells you what is configured and "
                "whether the bot has the permissions it needs in the channel you picked. Run "
                "those first if something isn't behaving.",
    },
]

SECTIONS = [
    {
        "id": "ratings",
        "icon": "⭐",
        "title": "Ratings and retention",
        "blurb": "The main event. Ask new members what they think, and measure whether they "
                 "stay.",
        "setup": [
            "Go to the channel you want the survey to live in and run <code>/setchannel</code>. "
            "That posts a message with buttons from 1 to 10 and marks that channel as the one "
            "the nudges go to.",
            "That's the whole setup. From then on, everybody who joins gets a hidden role for "
            "the day they arrived, and about a week later their group gets a single ping "
            "pointing them back at the survey.",
        ],
        "notes": [
            "The nudge deletes itself two seconds after it is sent, so it shows up as a "
            "notification without leaving a mess in the channel. If you are watching for it "
            "and blink, you will miss it. Use <code>/trackping</code> to log them instead.",
            "Each member is nudged once, not repeatedly. Nudging the same people every week "
            "would drive off the members you are trying to keep.",
            "Scores are saved one per member. Somebody who taps a second number replaces their "
            "old answer rather than voting twice.",
        ],
        "commands": [
            ("/setchannel", "", "Post the survey here and use this channel for nudges.",
             MANAGE_SERVER),
            ("/ratings", "", "The average score, a breakdown, the happy and unhappy split, and "
             "the most recent votes.", MANAGE_SERVER),
            ("/retention", "[period]", "How many new members are still around, grouped hourly, "
             "daily, weekly or monthly.", MANAGE_SERVER),
            ("/forcesurvey", "[days]", "Send a nudge now instead of waiting for midday. Use "
             "<code>days:0</code> to nudge whoever joined today, which is how you test it.",
             MANAGE_SERVER),
            ("/discoveryhelp", "", "Explains the whole loop and shows this server's status.",
             EVERYONE),
        ],
    },
    {
        "id": "greetings",
        "icon": "👋",
        "title": "Welcome and goodbye messages",
        "blurb": "Messages you write, sent when somebody joins or leaves.",
        "setup": [
            "Run <code>/welcome set</code> with your message. Give it a channel to post "
            "publicly, or leave the channel out and it arrives as a direct message instead.",
            "You get a preview back straight away, so you can see exactly what a new member "
            "will see before anybody joins.",
        ],
        "notes": [
            "Placeholders you can use: <code>{user}</code> mentions them, "
            "<code>{username}</code> is their name, <code>{tag}</code> is their full username, "
            "<code>{server}</code> is the server name, <code>{count}</code> is how many members "
            "there are, and <code>{ordinal}</code> is their position, like 42nd.",
            "Slash commands are single line, so type <code>\\n</code> where you want a line "
            "break.",
            "Typing @everyone into a greeting will not ping anyone. A welcome fires on every "
            "join, so a mistake there would be loud and repeated.",
            "Goodbye messages need a channel. Discord will not reliably let a bot message "
            "somebody who has already left.",
        ],
        "commands": [
            ("/welcome set", "<message> [channel] [embed]",
             "Write the greeting and switch it on.", MANAGE_SERVER),
            ("/welcome off", "", "Stop sending it. Your wording is kept.", MANAGE_SERVER),
            ("/welcome show", "", "Current settings, the placeholder list, and a preview.",
             MANAGE_SERVER),
            ("/goodbye set", "<message> <channel> [embed]",
             "Same, for when somebody leaves.", MANAGE_SERVER),
            ("/goodbye off", "", "Stop sending it.", MANAGE_SERVER),
            ("/goodbye show", "", "Current settings and a preview.", MANAGE_SERVER),
        ],
    },
    {
        "id": "autorole",
        "icon": "🎫",
        "title": "Autorole",
        "blurb": "Roles handed out the moment somebody joins.",
        "setup": [
            "On the dashboard, open your server, find Autorole, tick the roles you want and "
            "save. From Discord, run <code>/autorole add</code> once per role.",
            "Drag the bot's own role above the roles you are handing out, in Server Settings "
            "then Roles. Discord will not let any bot give out a role that sits above its own, "
            "whatever permissions it has.",
        ],
        "notes": [
            "If your server makes people accept rules before they can talk, the roles arrive "
            "when they finish accepting rather than the second they join. That is Discord's "
            "behaviour, not a delay on our side: roles given to somebody still on the rules "
            "screen are thrown away.",
            "Roles the bot cannot give out are shown on the dashboard but cannot be ticked, "
            "with the reason next to them. That is nearly always the role order above.",
            "Delete a role in Discord and it drops off the list by itself the next time "
            "somebody joins.",
            "Up to 10 roles. They are given in a single request, so a rush of joins will not "
            "hit a rate limit.",
        ],
        "commands": [
            ("/autorole add", "<role>", "Give this role to everybody who joins.", MANAGE_ROLES),
            ("/autorole remove", "<role>", "Stop handing that one out.", MANAGE_ROLES),
            ("/autorole list", "", "What new members get, and a warning next to anything that "
             "is not working.", MANAGE_ROLES),
            ("/autorole off", "", "Pause it. Your list is kept.", MANAGE_ROLES),
            ("/autorole on", "", "Start again.", MANAGE_ROLES),
        ],
    },
    {
        "id": "rolebuttons",
        "icon": "🎛️",
        "title": "Role buttons",
        "blurb": "A message people click to give themselves roles.",
        "setup": [
            "On the dashboard, open your server, go to Role buttons and add a panel. Give it a "
            "heading, pick the channel, tick the roles, and save. The bot posts the message "
            "within a few seconds.",
            "From Discord, <code>/rolepanel create</code> makes an empty panel and "
            "<code>/rolepanel addrole</code> puts a button on it. The message appears as soon "
            "as the first role is added.",
            "As with autorole, the bot's role has to sit above every role on the panel.",
        ],
        "notes": [
            "Buttons rather than reactions. Reactions break when a custom emoji is deleted, "
            "need extra permissions, and cannot tell somebody why their click did nothing. A "
            "button replies privately, so only the person clicking sees the answer.",
            "Set a panel to one role only and picking a second swaps it for the first. Useful "
            "for a colour or a pronoun where holding several at once makes no sense.",
            "25 buttons per message, which is Discord's limit of five rows of five. Make a "
            "second panel if you need more.",
            "Edit a panel and the existing message is edited in place, so links to it keep "
            "working. Delete one and the message goes with it.",
            "The buttons keep working across restarts and updates. Nothing is held in memory.",
        ],
        "commands": [
            ("/rolepanel create", "<channel> <title> [description] [mode]",
             "Start a new panel.", MANAGE_ROLES),
            ("/rolepanel addrole", "<panel> <role> [label] [emoji]",
             "Put a role button on it.", MANAGE_ROLES),
            ("/rolepanel removerole", "<panel> <role>", "Take a button off.", MANAGE_ROLES),
            ("/rolepanel list", "", "Every panel, its roles, and anything that went wrong.",
             MANAGE_ROLES),
            ("/rolepanel delete", "<panel>", "Remove the panel and its message.", MANAGE_ROLES),
        ],
    },
    {
        "id": "moderation",
        "icon": "🔨",
        "title": "Moderation",
        "blurb": "The usual actions, except every one becomes a numbered case you can look up "
                 "later.",
        "setup": [
            "Run <code>/modlogchannel</code> and pick a channel. Every action then gets posted "
            "there with a case number, who did it, who it was done to, and why.",
            "Actions still work without a log channel and are still recorded. They just are "
            "not posted anywhere you can see.",
        ],
        "notes": [
            "Each command needs the matching Discord permission. Somebody with Ban Members can "
            "ban but not change the log channel.",
            "The bot's role has to sit above anybody you want to action, in Server Settings "
            "then Roles. If it doesn't, the command tells you that rather than failing with "
            "something cryptic.",
            "You cannot action somebody whose top role is at or above your own, and nobody can "
            "action the server owner.",
            "Durations are written naturally: <code>10m</code>, <code>2h</code>, "
            "<code>1d</code>, <code>1h30m</code>. Discord caps timeouts at 28 days.",
            "<code>/delwarn</code> marks a warning inactive rather than deleting it. It stops "
            "counting toward their total but stays visible in <code>/modlogs</code>, so "
            "clearing warnings cannot quietly erase the trail.",
        ],
        "commands": [
            ("/ban", "<member> [reason] [delete_days]",
             "Ban somebody, optionally deleting their recent messages.", "Ban Members"),
            ("/unban", "<user_id> [reason]", "Unban by user ID.", "Ban Members"),
            ("/kick", "<member> [reason]", "Remove somebody from the server.", "Kick Members"),
            ("/timeout", "<member> <duration> [reason]",
             "Mute somebody for a while.", "Timeout Members"),
            ("/untimeout", "<member> [reason]", "End a timeout early.", "Timeout Members"),
            ("/warn", "<member> <reason>", "Warn somebody. Recorded against them.",
             "Timeout Members"),
            ("/warnings", "<member>", "Their active warnings.", "Timeout Members"),
            ("/delwarn", "<case_id>", "Remove a warning by its case number.", MANAGE_SERVER),
            ("/modlogs", "<member>", "Everything on record for that member.",
             "Timeout Members"),
            ("/purge", "<amount> [member] [contains]",
             "Bulk delete recent messages, optionally only from one person or containing some "
             "text.", "Manage Messages"),
            ("/slowmode", "<duration>", "Set this channel's slowmode. Use 0 to turn it off.",
             "Manage Channels"),
            ("/lock", "[reason]", "Stop members posting in this channel.", "Manage Channels"),
            ("/unlock", "[reason]", "Let them post again.", "Manage Channels"),
            ("/modlogchannel", "[channel]",
             "Where cases get posted. Leave the channel out to switch it off.", MANAGE_SERVER),
        ],
    },
    {
        "id": "medialog",
        "icon": "🗄️",
        "title": "Deleted media logging",
        "blurb": "When an image, video or voice memo is deleted, you get the file itself.",
        "setup": [
            "Run <code>/logchannel</code> and pick where the logs should go. That is the only "
            "step.",
        ],
        "notes": [
            "Discord destroys a file the moment its message is deleted, so the bot downloads a "
            "copy while the message is still there and holds it briefly. When the message goes, "
            "that copy is posted to your log channel.",
            "This means coverage starts when you switch it on, and a file posted before the bot "
            "last restarted may not be recoverable. In that case you still get who deleted "
            "what and when, and the log says the file was not kept.",
            "Documents, archives and other file types are ignored. Only images, video and "
            "audio are tracked.",
            "Files over 8MB are not kept, but the deletion is still logged with the filename "
            "and size.",
            "Without the View Audit Log permission the bot cannot tell who deleted something, "
            "and will say so rather than guess. Note that somebody deleting their own message "
            "leaves no audit entry at all, so that case always reads as unknown.",
        ],
        "commands": [
            ("/logchannel", "<channel>", "Where deleted media gets logged.", MANAGE_SERVER),
            ("/logoff", "", "Stop logging.", MANAGE_SERVER),
            ("/logstatus", "", "What is configured, plus a permission check.", MANAGE_SERVER),
        ],
    },
    {
        "id": "trackping",
        "icon": "🔔",
        "title": "Nudge tracking",
        "blurb": "Because the nudge deletes itself, this is the only lasting record that it "
                 "fired.",
        "setup": [
            "Run <code>/trackping</code> with a channel. Every nudge then gets logged with "
            "which joining group it targeted and how many members it reached.",
        ],
        "notes": [
            "Only this bot's own nudges are logged. Ordinary pings from members are not "
            "tracked.",
            "Run <code>/trackping</code> on its own, with no channel, for a summary of the last "
            "seven days.",
        ],
        "commands": [
            ("/trackping", "[channel]",
             "Set the log channel, or leave it out to see recent activity.", MANAGE_SERVER),
            ("/trackpingoff", "", "Stop logging nudges.", MANAGE_SERVER),
        ],
    },
    {
        "id": "stats",
        "icon": "📊",
        "title": "Server statistics",
        "blurb": "Read-only, and open to everyone in the server.",
        "setup": [],
        "notes": [
            "These have short cooldowns because they scan every member. If you are told to slow "
            "down, that is why.",
            "<code>/stats activity</code> only reads channels you can already see.",
        ],
        "commands": [
            ("/stats roles", "[limit]", "The most common roles.", EVERYONE),
            ("/stats activity", "[channel] [hours]",
             "Message counts, top posters and the busiest hour.", EVERYONE),
            ("/stats playing", "[online_only] [show_examples]",
             "What people are playing right now.", EVERYONE),
            ("/stats tags", "[online_only]", "Which guild tags members are wearing.", EVERYONE),
            ("/stats badges", "[badge] [show_members]",
             "Count or list members with a profile badge.", EVERYONE),
        ],
    },
    {
        "id": "other",
        "icon": "🔧",
        "title": "Spam filter and utilities",
        "blurb": "",
        "setup": [],
        "notes": [
            "The spam filter removes batches of images posted with the filename patterns spam "
            "tends to use, such as several images all named 1.png, 2.png and so on. The file is "
            "kept in your media log so you can still see what was removed.",
            "If a command looks missing or out of date in Discord's picker, run "
            "<code>/sync</code>. If it still looks wrong, fully close and reopen Discord: the "
            "app caches the command list and does not always refresh on its own.",
        ],
        "commands": [
            ("/toggleimagespam", "", "Turn the image spam filter on or off.", MANAGE_SERVER),
            ("/imagespamstatus", "", "Whether the filter is currently running.", MANAGE_SERVER),
            ("/sync", "", "Force this server's command list to refresh.", MANAGE_SERVER),
            ("/say", "<message>", "Make the bot post a message.", "Manage Messages"),
            ("/help", "", "A browsable list of everything.", EVERYONE),
        ],
    },
]

TROUBLESHOOTING = [
    ("A command doesn't appear in Discord",
     "Run <code>/sync</code>, then fully quit and reopen Discord. The client caches the command "
     "list locally and does not always pick up changes on its own."),
    ("Nothing happens when somebody joins",
     "Greetings are off until you run <code>/welcome set</code>. Check with "
     "<code>/welcome show</code>, which will tell you it is off and show you what is stored."),
    ("A moderation command says my role is too low",
     "The bot's role has to sit above the member you are actioning. Move it up in Server "
     "Settings, then Roles."),
    ("Deleted files are not being kept",
     "Check <code>/logstatus</code>. The usual causes are a missing permission in the log "
     "channel, a file over 8MB, or a message posted before the bot last restarted."),
    ("The mod log says the deleter is unknown",
     "Either the bot is missing View Audit Log, or the person deleted their own message. "
     "Discord does not write an audit entry for that, so it genuinely cannot be known."),
    ("/forcesurvey says there was nobody to nudge",
     "It looks for people who joined exactly eight days ago. If the bot has not been in your "
     "server that long there is nobody to nudge yet. Use <code>days:0</code> to test it against "
     "whoever joined today."),
    ("A change on the dashboard has not taken effect",
     "Give it a few seconds. The bot checks for changes every ten seconds rather than on every "
     "message."),
]
