"""What changed, kept as data for the same reason the docs are.

Grouped by the day the work landed rather than by release, because there are no releases: the
bot is deployed continuously. Written for a server owner deciding whether this thing is looked
after, so entries say what it does for them, not which file moved.

Newest first. Add to the top.
"""

# kind -> (icon, label). "new" is a feature, "better" is an existing one improved, "fixed" is
# something that was wrong.
KINDS = {
    "new": ("✨", "New"),
    "better": ("🔧", "Better"),
    "fixed": ("🐛", "Fixed"),
}

ENTRIES = [
    {
        "date": "2026-08-08",
        "title": "Support, previews and a searchable reference",
        "changes": [
            ("new", "A support page. Open a ticket without leaving the site, get a direct "
                    "message when it's answered, and read the whole thread in one place."),
            ("new", "A status page that says whether the bot is running, updating itself "
                    "every twenty seconds."),
            ("new", "Shared links now unfurl into a proper card in Discord instead of a bare "
                    "grey box."),
            ("better", "The documentation can be searched. Sixty-odd commands was more than "
                       "anybody was going to scroll through."),
            ("better", "Every heading in the docs has its own link, so an answer can point at "
                       "the exact place."),
            ("better", "The dashboard tells you when a form has unsaved changes, and offers "
                       "to put it back."),
            ("better", "Five settings tabs instead of seven, and a rule's own settings stay "
                       "out of sight until you switch it on."),
        ],
    },
    {
        "date": "2026-08-08",
        "title": "Automod, and the Discovery checklist",
        "changes": [
            ("new", "Automod: nine rules covering banned words, invites, links, mass "
                    "mentions, floods, repeats, shouting, emoji and walls of text. Each is "
                    "separate, and everything is off until you turn it on."),
            ("new", "<code>/discovery</code> checks your server against what Server Discovery "
                    "asks for and says what is still in the way."),
            ("new", "The bot now says hello when it's added to a server, rather than joining "
                    "in silence."),
            ("better", "Automod warnings and timeouts become numbered cases in the same "
                       "history as anything a moderator did by hand."),
            ("better", "Kicks and bans can be automod responses too, with a limit on how many "
                       "can happen in an hour so one bad word list can't empty a server."),
        ],
    },
    {
        "date": "2026-08-08",
        "title": "The server log",
        "changes": [
            ("new", "A server log covering thirteen kinds of event, from deleted messages to "
                    "voice movement. Send it all to one channel, or send any of it wherever "
                    "you want."),
            ("new", "<code>/logging setup</code> builds the log channels for you, hidden from "
                    "everybody but staff, and switches it all on."),
            ("better", "All four logs answer to <code>/logging</code> now. They used to have "
                       "four different naming conventions between them."),
            ("better", "Survey \"nudges\" are called reminders, which is a word people arrive "
                       "already knowing."),
            ("fixed", "A ban placed with <code>/ban</code> was recorded twice, once as a case "
                      "and once in the server log."),
        ],
    },
    {
        "date": "2026-08-08",
        "title": "Roles people can give themselves",
        "changes": [
            ("new", "Role buttons: a message with a button per role, built from the "
                    "dashboard. Buttons rather than reactions, so nothing breaks when an "
                    "emoji is deleted and a click that doesn't work says why."),
            ("new", "Autorole, with up to ten roles handed out the moment somebody joins."),
            ("better", "The dashboard won't offer a role the bot couldn't actually hand out, "
                       "and says which rule is in the way."),
            ("fixed", "Autorole now waits for people to finish the rules screen. Roles given "
                      "before that are thrown away by Discord, so servers with a rules gate "
                      "were getting nothing."),
        ],
    },
    {
        "date": "2026-08-08",
        "title": "A dashboard worth using",
        "changes": [
            ("new", "Documentation covering every command, what it does and who can run it."),
            ("new", "Terms of service and a privacy policy, linked from every page."),
            ("better", "The landing page leads with Server Discovery, which is the point of "
                       "the bot."),
            ("better", "Settings moved into tabs rather than one long column of cards."),
            ("fixed", "A malformed redirect address is now caught and explained, instead of "
                      "Discord rejecting the login with no clue why."),
        ],
    },
    {
        "date": "2026-08-07",
        "title": "The dashboard arrives",
        "changes": [
            ("new", "A web dashboard. Log in with Discord, pick a server, change its settings "
                    "without remembering a command."),
            ("new", "Welcome and goodbye messages, written by you, sent to a channel or by "
                    "direct message."),
            ("fixed", "<code>/welcome set</code> crashed outright. It had never actually been "
                      "run, only reviewed."),
        ],
    },
    {
        "date": "2026-08-04",
        "title": "Retention, measured properly",
        "changes": [
            ("new", "<code>/retention</code>: how many of the people who joined are still "
                    "here, grouped by hour, day, week or month."),
            ("better", "Settings are cached and shared between features, so a busy server "
                       "costs one database read rather than one per message."),
            ("better", "The bot asks Discord for only the data it uses, which keeps it inside "
                       "what Discord approves for a bot this size."),
            ("fixed", "A cohort could be reminded up to twelve times in a day. One missing "
                      "line, swallowed by a broad error handler."),
        ],
    },
]
