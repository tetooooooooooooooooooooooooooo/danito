"""The dashboard's view of the same MongoDB the bot uses.

Deliberately its own small module rather than importing the bot's GuildConfig: that one caches
per process and expects a bot object. Two processes sharing one cache would be a lie, so the
dashboard reads through every time and tells the bot when something changed.
"""

import datetime
import os
import re

import certifi
from bson import ObjectId
from bson.errors import InvalidId
from pymongo import MongoClient

_client = None

# Only these may be written from the web. Anything not listed here cannot be set by a form
# post, however the request is crafted.
ALLOWED_FIELDS = {
    "medialog_enabled": bool,
    "medialog_channel": "channel_or_none",
    "modlog_channel": "channel_or_none",
    "pinglog_enabled": bool,
    "pinglog_channel": "channel_or_none",
    "welcome_enabled": bool,
    "welcome_channel": "channel_or_none",
    "welcome_message": "text",
    "welcome_embed": bool,
    "goodbye_enabled": bool,
    "goodbye_channel": "channel_or_none",
    "goodbye_message": "text",
    "goodbye_embed": bool,
    "autorole_enabled": bool,
    "autorole_ids": "role_ids",
    "logging_enabled": bool,
    "log_channel": "channel_or_none",
}
MAX_TEXT = 1500

# Mirrors Cogs/Logging.EVENTS. The two processes share no code, so the keys are written out
# twice; a test asserts they still agree, because a typo here means an event the dashboard can
# switch on and the bot never sends.
LOG_EVENTS = [
    ("message_delete",   "🗑️", "Deleted messages",
     "The text of a message somebody removed."),
    ("message_edit",     "✏️", "Edited messages",
     "What it said before and after."),
    ("message_purge",    "🧹", "Bulk deletions",
     "When a moderator clears a batch of messages."),
    ("member_join",      "📥", "People joining",
     "Who arrived, and how old their account is."),
    ("member_leave",     "📤", "People leaving",
     "Who left, how long they stayed, and what roles they had."),
    ("member_ban",       "🔨", "Bans",
     "Who was banned, by whom and why."),
    ("member_unban",     "🕊️", "Unbans", "Who was let back in."),
    ("member_nickname",  "🏷️", "Nickname changes", "Before and after."),
    ("member_roles",     "🎭", "Role changes",
     "Roles given and taken away, however that happened."),
    ("voice_activity",   "🔊", "Voice channels",
     "Joining, leaving and moving between voice channels. Bots are left out."),
    ("channel_changes",  "📁", "Channels", "Channels added, removed or renamed."),
    ("role_changes",     "🎟️", "Roles", "Roles added, removed or renamed."),
    ("server_changes",   "⚙️", "Server settings",
     "The server name, icon or owner changing."),
]
LOG_EVENT_KEYS = [key for key, _, _, _ in LOG_EVENTS]

# Mirrors Cogs/AutoMod.RULES, for the same reason the log events are mirrored: the two
# processes share no code, and a typo means a rule the dashboard can switch on and the bot
# never runs. (key, icon, label, blurb, [(setting, label, min, max)])
AUTOMOD_RULES = [
    ("words", "🚫", "Banned words",
     "Words you don't want said. Matched whole, so blocking a short word won't eat longer "
     "ones that contain it.", []),
    ("invites", "✉️", "Discord invites",
     "Links to other Discord servers.", []),
    ("links", "🔗", "Links",
     "Any link at all, apart from sites you allow below.", []),
    ("mentions", "📣", "Mass mentions",
     "Pinging a crowd in one message.", [("limit", "More than", 1, 50)]),
    ("spam", "💬", "Message flood",
     "Too many messages too quickly.",
     [("count", "Messages", 2, 30), ("seconds", "In seconds", 1, 60)]),
    ("duplicates", "♻️", "Repeated messages",
     "The same message over and over.", [("count", "Repeats", 2, 20)]),
    ("caps", "🔠", "Shouting",
     "Messages mostly in capitals.",
     [("percent", "Percent caps", 40, 100), ("min_length", "Min letters", 5, 200)]),
    ("emoji", "😀", "Emoji spam",
     "Walls of emoji.", [("limit", "More than", 1, 100)]),
    ("newlines", "📜", "Wall of text",
     "Messages stretched over many lines.", [("limit", "More than", 2, 100)]),
]
AUTOMOD_RULE_KEYS = [key for key, _, _, _, _ in AUTOMOD_RULES]
AUTOMOD_ACTIONS = [
    ("delete", "Delete the message"),
    ("warn", "Delete and warn"),
    ("timeout", "Delete and time them out"),
    ("kick", "Delete and kick them"),
    ("ban", "Delete and ban them"),
]
# Kicks and bans can't be undone with a click, so there is a ceiling on how many can
# happen in an hour. Past it they become timeouts.
AUTOMOD_REMOVAL_RANGE = (1, 100)
# Mirrors Cogs/AutoMod.DEFAULTS, so an unconfigured server sees sensible numbers in the boxes
# rather than blanks. Only the thresholds are needed here; the bot owns the rest.
AUTOMOD_DEFAULTS = {
    "mentions": {"limit": 5},
    "spam": {"count": 6, "seconds": 5},
    "duplicates": {"count": 3},
    "caps": {"percent": 70, "min_length": 12},
    "emoji": {"limit": 8},
    "newlines": {"limit": 15},
}
AUTOMOD_MAX_WORDS = 100
AUTOMOD_MAX_WORD_LENGTH = 40
AUTOMOD_MAX_DOMAINS = 50
AUTOMOD_MAX_EXEMPT = 25
AUTOMOD_TIMEOUT_RANGE = (1, 10080)          # a minute to a week, Discord's own ceiling

MAX_AUTOROLES = 10
MAX_PANELS = 10
MAX_PANEL_ROLES = 25          # Discord allows five rows of five buttons on one message
MAX_LABEL = 80
PANEL_MODES = ("toggle", "single")


def db():
    global _client
    if _client is None:
        _client = MongoClient(os.environ.get("Database_Connection_String"),
                              tlsCAFile=certifi.where())
    return _client["discovery_bot"]


def settings(guild_id: int) -> dict:
    return db()["servers"].find_one({"guild_id": guild_id}) or {}


def bot_guild_ids() -> set:
    """Published by the bot, so the picker only offers servers it is actually in."""
    doc = db()["runtime"].find_one({"_id": "bot"}) or {}
    return set(doc.get("guild_ids") or [])


# The bot writes a heartbeat once a minute. The dashboard is a separate process and cannot see
# it at all, so "is it up" is really "how long since it last said so". A couple of missed beats
# is a restart or a slow deploy and not worth alarming anybody; past ten minutes it is down.
HEARTBEAT_GRACE = 180
HEARTBEAT_DOWN = 600


def bot_status() -> dict:
    """Whether the bot is up, and what it was doing when it last checked in.

    Always the same shape, including when there is nothing to report, so anything reading it
    can look at one field rather than checking which keys arrived.
    """
    unknown = {"state": "unknown", "reason": None, "seconds_quiet": None, "last_seen": None,
               "started_at": None, "uptime_seconds": None, "guilds": 0, "members": None,
               "latency_ms": None}

    try:
        doc = db()["runtime"].find_one({"_id": "bot"}) or {}
    except Exception:
        # The database being unreachable is itself worth reporting rather than a 500.
        return {**unknown, "reason": "I can't reach the database to find out."}

    last = doc.get("last_seen")
    if last is None:
        return {**unknown,
                "reason": "The bot hasn't checked in since this page was added."}

    if last.tzinfo is None:
        last = last.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    quiet = max((now - last).total_seconds(), 0)

    if quiet <= HEARTBEAT_GRACE:
        state = "up"
    elif quiet <= HEARTBEAT_DOWN:
        state = "wobbly"
    else:
        state = "down"

    started = doc.get("started_at")
    if started is not None and started.tzinfo is None:
        started = started.replace(tzinfo=datetime.timezone.utc)

    return {
        "state": state,
        "reason": None,
        "seconds_quiet": int(quiet),
        "last_seen": last,
        "started_at": started,
        "uptime_seconds": int((last - started).total_seconds()) if started else None,
        "guilds": doc.get("guild_count") or len(doc.get("guild_ids") or []),
        "members": doc.get("member_count"),
        "latency_ms": doc.get("latency_ms"),
    }


def clean(field: str, raw, valid_channels: set, valid_roles: set = frozenset()):
    """Coerce one submitted value, rejecting anything that isn't allowed.

    Channel and role ids are checked against what the guild really has, so a crafted post can't
    point the bot at a channel in a different server or hand out a role nobody chose. The role
    set passed in holds only roles the bot can actually assign, which means the hierarchy rule
    is enforced on save rather than discovered later when nothing happens.
    """
    kind = ALLOWED_FIELDS[field]
    if kind is bool:
        return bool(raw)
    if kind == "text":
        return (raw or "").strip()[:MAX_TEXT] or None
    if kind == "channel_or_none":
        if not raw:
            return None
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            return None
        return cid if cid in valid_channels else None
    if kind == "role_ids":
        out = []
        for item in (raw or []):
            try:
                rid = int(item)
            except (TypeError, ValueError):
                continue
            if rid in valid_roles and rid not in out:
                out.append(rid)
        return out[:MAX_AUTOROLES]
    raise ValueError(field)


def clean_log_events(form, valid_channels: set) -> dict:
    """Build the whole event map from a submitted form.

    Written out in full every time rather than patched, so a key the form doesn't mention is
    dropped instead of lingering. A channel that isn't in this guild becomes None, which falls
    back to the shared log channel rather than silently sending nothing.
    """
    out = {}
    for key in LOG_EVENT_KEYS:
        channel = None
        raw = form.get(f"log_ch_{key}", "")
        if raw:
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                cid = None
            channel = cid if cid in valid_channels else None
        out[key] = {"on": f"log_on_{key}" in form, "channel": channel}
    return out


def _clamp(raw, low, high, fallback):
    try:
        return max(low, min(high, int(raw)))
    except (TypeError, ValueError):
        return fallback


def _word_list(raw, limit, length) -> list:
    """A textarea or comma separated box into a clean list, deduplicated and capped."""
    out = []
    for piece in re.split(r"[,\n]", raw or ""):
        piece = piece.strip().lower()
        if piece and piece not in out:
            out.append(piece[:length])
    return out[:limit]


def clean_automod(form, valid_channels: set, valid_roles: set, existing: dict) -> dict:
    """Build the whole automod document from a submitted form.

    Rebuilt in full rather than patched, so a rule the form doesn't mention is switched off
    instead of lingering. Thresholds are clamped to the same ranges the inputs advertise, since
    a number typed straight into the request has not been near those inputs.
    """
    rules = {}
    for key, _, _, _, fields in AUTOMOD_RULES:
        was = (existing.get("rules") or {}).get(key) or {}
        action = form.get(f"am_action_{key}", "delete")
        rule = {
            "on": f"am_on_{key}" in form,
            "action": action if action in dict(AUTOMOD_ACTIONS) else "delete",
        }
        for name, _label, low, high in fields:
            rule[name] = _clamp(form.get(f"am_{key}_{name}"), low, high, was.get(name, low))
        if key == "words":
            rule["list"] = _word_list(form.get("am_words_list"),
                                      AUTOMOD_MAX_WORDS, AUTOMOD_MAX_WORD_LENGTH)
        if key == "links":
            rule["allow"] = _word_list(form.get("am_links_allow"),
                                       AUTOMOD_MAX_DOMAINS, AUTOMOD_MAX_WORD_LENGTH)
        rules[key] = rule

    def ids(field, allowed):
        out = []
        for raw in form.getlist(field):
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value in allowed and value not in out:
                out.append(value)
        return out[:AUTOMOD_MAX_EXEMPT]

    low, high = AUTOMOD_TIMEOUT_RANGE
    rlow, rhigh = AUTOMOD_REMOVAL_RANGE
    return {
        "enabled": "automod_enabled" in form,
        "notify": "automod_notify" in form,
        "exempt_staff": "automod_exempt_staff" in form,
        "exempt_roles": ids("automod_exempt_roles", valid_roles),
        "exempt_channels": ids("automod_exempt_channels", valid_channels),
        "timeout_minutes": _clamp(form.get("automod_timeout"), low, high, 10),
        "max_removals": _clamp(form.get("automod_max_removals"), rlow, rhigh, 5),
        "rules": rules,
    }


# ── the embed builder ────────────────────────────────────────────────
# Discord's own limits. Enforced here rather than left to Discord because it rejects the whole
# message for one long field and names it in a nested error tree, which is a bad way to find
# out you wrote 4,200 characters.
EMBED_MAX = {
    "content": 2000, "title": 256, "description": 4096, "author": 256,
    "footer": 2048, "field_name": 256, "field_value": 1024, "url": 1024,
}
MAX_EMBED_FIELDS = 25
# Discord counts title, description, author, footer and every field together against one
# ceiling, on top of the individual ones. Nothing warns you until it refuses.
EMBED_TOTAL = 6000

HEX_COLOUR = re.compile(r"^#?([0-9a-fA-F]{6})$")


def _link(raw, problems: list, label: str):
    """A url Discord will accept, or None with a reason worth reading.

    Checked here because Discord's answer to a bad one is to refuse the entire message, and
    the most common mistake by far is pasting something that isn't a link at all.
    """
    value = (raw or "").strip()
    if not value:
        return None
    if not value.startswith(("http://", "https://")):
        problems.append(f"{label} has to start with http:// or https://.")
        return None
    if len(value) > EMBED_MAX["url"]:
        problems.append(f"{label} is too long.")
        return None
    return value


def _cut(raw, limit: int, problems: list, label: str):
    value = (raw or "").strip()
    if not value:
        return None
    if len(value) > limit:
        problems.append(f"{label} is {len(value)} characters. The limit is {limit}.")
        return None
    return value


def clean_embed(form) -> tuple:
    """Turn the builder's form into a message Discord will accept, or say why it won't.

    Returns (payload, problems). An empty problems list means it is safe to send.
    """
    problems = []

    content = _cut(form.get("content"), EMBED_MAX["content"], problems, "The message text")
    embed = {}

    title = _cut(form.get("title"), EMBED_MAX["title"], problems, "The title")
    if title:
        embed["title"] = title
    description = _cut(form.get("description"), EMBED_MAX["description"], problems,
                       "The description")
    if description:
        embed["description"] = description

    url = _link(form.get("url"), problems, "The title link")
    if url:
        if not title:
            problems.append("A title link needs a title, or there is nothing to click.")
        else:
            embed["url"] = url

    raw_colour = (form.get("colour") or "").strip()
    if raw_colour:
        match = HEX_COLOUR.match(raw_colour)
        if not match:
            problems.append("The colour has to be a hex code like #3ddc97.")
        else:
            embed["color"] = int(match.group(1), 16)

    author_name = _cut(form.get("author_name"), EMBED_MAX["author"], problems, "The author")
    author_url = _link(form.get("author_url"), problems, "The author link")
    author_icon = _link(form.get("author_icon"), problems, "The author icon")
    if author_name:
        embed["author"] = {"name": author_name}
        if author_url:
            embed["author"]["url"] = author_url
        if author_icon:
            embed["author"]["icon_url"] = author_icon
    elif author_url or author_icon:
        problems.append("An author link or icon needs an author name to hang off.")

    footer_text = _cut(form.get("footer_text"), EMBED_MAX["footer"], problems, "The footer")
    footer_icon = _link(form.get("footer_icon"), problems, "The footer icon")
    if footer_text:
        embed["footer"] = {"text": footer_text}
        if footer_icon:
            embed["footer"]["icon_url"] = footer_icon
    elif footer_icon:
        problems.append("A footer icon needs footer text to sit beside.")

    thumbnail = _link(form.get("thumbnail"), problems, "The thumbnail")
    if thumbnail:
        embed["thumbnail"] = {"url": thumbnail}
    image = _link(form.get("image"), problems, "The image")
    if image:
        embed["image"] = {"url": image}

    if form.get("timestamp"):
        embed["timestamp"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Indexed rather than parallel lists, because an unticked checkbox submits nothing at all
    # and would silently shift every later row's inline flag onto the wrong field.
    fields = []
    for index in range(MAX_EMBED_FIELDS):
        name = _cut(form.get(f"field_name_{index}"), EMBED_MAX["field_name"], problems,
                    f"Field {index + 1}'s name")
        value = _cut(form.get(f"field_value_{index}"), EMBED_MAX["field_value"], problems,
                     f"Field {index + 1}'s text")
        if not name and not value:
            continue
        if not name or not value:
            problems.append(f"Field {index + 1} needs both a name and some text.")
            continue
        fields.append({"name": name, "value": value,
                       "inline": bool(form.get(f"field_inline_{index}"))})
    if fields:
        embed["fields"] = fields

    # Everything that counts towards Discord's shared ceiling.
    counted = sum(len(str(part)) for part in (
        embed.get("title", ""), embed.get("description", ""),
        (embed.get("author") or {}).get("name", ""),
        (embed.get("footer") or {}).get("text", ""),
    )) + sum(len(f["name"]) + len(f["value"]) for f in fields)
    if counted > EMBED_TOTAL:
        problems.append(f"The embed is {counted} characters all told. Discord's limit across "
                        f"the title, description, author, footer and fields is {EMBED_TOTAL}.")

    # An embed with only a colour is invisible, and Discord refuses a message with neither
    # text nor a real embed. Say which rather than letting it come back as a 400.
    substantial = any(key in embed for key in
                      ("title", "description", "fields", "image", "thumbnail", "author",
                       "footer"))
    if not content and not substantial:
        problems.append("There's nothing to send. Write some text, or fill in the embed.")

    payload = {}
    if content:
        payload["content"] = content
    if substantial:
        payload["embeds"] = [embed]
    # Nothing pings unless it is asked for. An admin pasting a draft with an @everyone in it
    # should not find out it was live by the sound of a thousand notifications.
    payload["allowed_mentions"] = ({"parse": ["users", "roles", "everyone"]}
                                   if form.get("allow_pings") else {"parse": []})
    return payload, problems


def save(guild_id: int, values: dict):
    """Write settings and flag the guild so the bot drops its cached copy promptly."""
    if values:
        db()["servers"].update_one({"guild_id": guild_id}, {"$set": values}, upsert=True)
    mark_dirty(guild_id)


def mark_dirty(guild_id: int):
    db()["config_dirty"].update_one(
        {"_id": guild_id},
        {"$set": {"at": datetime.datetime.now(datetime.timezone.utc)}},
        upsert=True)


# ── role panels ──────────────────────────────────────────────────────
# The dashboard has no gateway connection, so it can't post to Discord itself. It writes what
# the panel should look like and raises a flag; the bot's publish loop is what posts or edits
# the message. Every read and write below is scoped by guild_id as well as by panel id, so a
# guessed id from another server matches nothing.

def _panels():
    return db()["role_panels"]


def _oid(panel_id):
    try:
        return ObjectId(panel_id)
    except (InvalidId, TypeError):
        return None


def panels(guild_id: int) -> list:
    return list(_panels().find({"guild_id": guild_id}).sort("created_at", 1).limit(MAX_PANELS))


def panel(guild_id: int, panel_id) -> dict:
    oid = _oid(panel_id)
    if oid is None:
        return {}
    return _panels().find_one({"_id": oid, "guild_id": guild_id}) or {}


def create_panel(guild_id: int, channel_id: int, title: str, description, mode: str) -> bool:
    if _panels().count_documents({"guild_id": guild_id}) >= MAX_PANELS:
        return False
    _panels().insert_one({
        "guild_id": guild_id,
        "channel_id": channel_id,
        "message_id": None,
        "title": (title or "Pick your roles").strip()[:200],
        "description": (description or "").strip()[:MAX_TEXT] or None,
        "color": 0x3DDC97,
        "mode": mode if mode in PANEL_MODES else "toggle",
        "roles": [],
        # Nothing to post until it has a button on it.
        "needs_publish": False,
        "publish_error": None,
        "created_at": datetime.datetime.now(datetime.timezone.utc),
    })
    return True


def save_panel(guild_id: int, panel_id, channel_id: int, title: str, description,
               mode: str, roles: list) -> bool:
    """Replace a panel's contents and queue it for the bot to publish."""
    oid = _oid(panel_id)
    if oid is None:
        return False
    result = _panels().update_one(
        {"_id": oid, "guild_id": guild_id},
        {"$set": {
            "channel_id": channel_id,
            "title": (title or "Pick your roles").strip()[:200],
            "description": (description or "").strip()[:MAX_TEXT] or None,
            "mode": mode if mode in PANEL_MODES else "toggle",
            "roles": roles[:MAX_PANEL_ROLES],
            # An empty panel has nothing to show, so don't ask the bot to post one.
            "needs_publish": bool(roles),
            "publish_error": None,
        }})
    return result.matched_count == 1


# ── support tickets ──────────────────────────────────────────────────
# Written here and read by the bot, the same way role panels work: the dashboard has no
# gateway connection, so it records the ticket and the bot is what announces it.

TICKET_CATEGORIES = [
    ("broken", "Something isn't working"),
    ("howto", "How do I do something"),
    ("idea", "An idea or a request"),
    ("data", "Data, privacy or deletion"),
    ("other", "Something else"),
]
TICKET_CATEGORY_LABELS = dict(TICKET_CATEGORIES)
TICKET_STATUSES = ("open", "answered", "closed")

MAX_SUBJECT = 120
MAX_BODY = 2000
MAX_OPEN_TICKETS = 3          # per person, so one bad day can't fill the queue
TICKET_COOLDOWN = 60          # seconds between new tickets from the same person


def _tickets():
    return db()["tickets"]


def _next_ticket_number() -> int:
    """Sequential and global. find_one_and_update is atomic, so two people opening a ticket
    at the same moment can't be handed the same number."""
    doc = db()["counters"].find_one_and_update(
        {"_id": "ticket"}, {"$inc": {"seq": 1}}, upsert=True, return_document=True)
    return int(doc["seq"]) if doc else 1


def tickets_for(user_id: int, limit: int = 25) -> list:
    return list(_tickets().find({"user_id": int(user_id)})
                .sort("created_at", -1).limit(limit))


def ticket(user_id: int, number: int) -> dict:
    """Scoped by user as well as number, so a guessed number matches nothing."""
    try:
        number = int(number)
    except (TypeError, ValueError):
        return {}
    return _tickets().find_one({"number": number, "user_id": int(user_id)}) or {}


def can_open_ticket(user_id: int) -> str:
    """Empty string when they may, otherwise the reason they may not."""
    user_id = int(user_id)
    live = _tickets().count_documents(
        {"user_id": user_id, "status": {"$ne": "closed"}})
    if live >= MAX_OPEN_TICKETS:
        return (f"You already have {live} tickets open. Close one, or reply on it, rather "
                f"than starting another.")

    last = list(_tickets().find({"user_id": user_id}).sort("created_at", -1).limit(1))
    # A ticket with no timestamp can't be aged, so it doesn't get a say. Letting somebody
    # through is the right way to be wrong here: the open ticket limit above is what actually
    # stops a flood, and this only spaces them out.
    opened = _aware(last[0].get("created_at")) if last else None
    if opened is not None:
        age = (datetime.datetime.now(datetime.timezone.utc) - opened).total_seconds()
        if age < TICKET_COOLDOWN:
            return f"Give it {int(TICKET_COOLDOWN - age)} more seconds before opening another."
    return ""


def open_ticket(user_id: int, user_tag: str, category: str, subject: str, body: str,
                guild_id=None, guild_name=None) -> int:
    now = datetime.datetime.now(datetime.timezone.utc)
    number = _next_ticket_number()
    _tickets().insert_one({
        "number": number,
        "user_id": int(user_id),
        "user_tag": user_tag,
        "guild_id": int(guild_id) if guild_id else None,
        "guild_name": guild_name,
        "category": category if category in TICKET_CATEGORY_LABELS else "other",
        "subject": (subject or "").strip()[:MAX_SUBJECT] or "No subject",
        "body": (body or "").strip()[:MAX_BODY],
        "status": "open",
        "created_at": now,
        "updated_at": now,
        # The bot announces it, and clears this once it has.
        "posted": False,
        "messages": [],
    })
    return number


def reply_to_ticket(user_id: int, number: int, body: str) -> bool:
    """A reply from the person who opened it, which reopens an answered ticket."""
    found = ticket(user_id, number)
    if not found or found.get("status") == "closed":
        return False
    now = datetime.datetime.now(datetime.timezone.utc)
    _tickets().update_one(
        {"_id": found["_id"]},
        {"$set": {"status": "open", "updated_at": now, "posted": False},
         "$push": {"messages": {"from": "you", "author": found.get("user_tag", ""),
                                "body": (body or "").strip()[:MAX_BODY], "at": now}}})
    return True


def close_ticket(user_id: int, number: int) -> bool:
    found = ticket(user_id, number)
    if not found or found.get("status") == "closed":
        return False
    _tickets().update_one(
        {"_id": found["_id"]},
        {"$set": {"status": "closed",
                  "updated_at": datetime.datetime.now(datetime.timezone.utc)}})
    return True


def delete_panel(guild_id: int, panel_id) -> bool:
    """Flag the panel for removal rather than dropping the document.

    The message in Discord has to go too, and only the bot can delete it. Removing the record
    here would leave a message whose buttons work forever and answer nobody.
    """
    oid = _oid(panel_id)
    if oid is None:
        return False
    result = _panels().update_one(
        {"_id": oid, "guild_id": guild_id},
        {"$set": {"pending_delete": True, "needs_publish": False}})
    return result.matched_count == 1


# ── insights ─────────────────────────────────────────────────────────
# The bot records one document per membership spell: opened on join, closed on leave. Those
# two facts answer both questions this page asks, so nothing extra is stored and nothing is
# precomputed.
#
# Spells expire after 180 days, which is the ceiling on everything below. The page says so,
# because a number that quietly stops counting is worse than one that admits its window.
SPELL_WINDOW_DAYS = 180
INSIGHT_WINDOWS = (1, 7, 14, 30)

# Grouping for the trend chart. Weekly is the default: daily is too noisy to read a direction
# off, and monthly over a 180 day window is six bars.
TREND_PERIODS = {
    "daily": ("day", 30, "%d %b", "Last 30 days"),
    "weekly": ("week", 12, "%d %b", "Last 12 weeks"),
    "monthly": ("month", 6, "%b %Y", "Last 6 months"),
}
DEFAULT_TREND = "weekly"

# This runs on a web request. A server past the cap gets its most recent joins, which is the
# part anybody is looking at, and the page says the figures are based on a sample.
MAX_SPELLS_READ = 20000


def _memberships():
    return db()["memberships"]


def _aware(dt):
    """pymongo hands back naive UTC datetimes, and comparing one to an aware now raises.

    The one definition in this module. There were briefly two, this one and another under the
    tickets, and since a later def silently replaces an earlier one every caller was getting
    whichever happened to be further down the file. They disagreed about None, so the ticket
    cooldown was one missing timestamp away from a TypeError. Callers handle None themselves.
    """
    if dt is None:
        return None
    return dt.replace(tzinfo=datetime.timezone.utc) if dt.tzinfo is None else dt


def _survived(spell, days: int) -> bool:
    """Whether this member was still here `days` after joining.

    Somebody still in the server has survived every window they are old enough for. Somebody
    who left survived only the windows that closed before they went.
    """
    joined = _aware(spell.get("joined_at"))
    left = _aware(spell.get("left_at"))
    if joined is None:
        return False
    if left is None:
        return True
    return (left - joined).total_seconds() >= days * 86400


def _measurable(spell, days: int, now) -> bool:
    """Only somebody who joined at least `days` ago can say anything about that window.

    This is why the denominators differ per window. Counting a member who joined yesterday as
    having survived 30 days would flatter every figure on the page.
    """
    joined = _aware(spell.get("joined_at"))
    return joined is not None and (now - joined).total_seconds() >= days * 86400


def _period_start(when, unit: str):
    """The start of the bucket a timestamp falls in."""
    when = _aware(when)
    if unit == "day":
        return when.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == "week":
        midnight = when.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight - datetime.timedelta(days=midnight.weekday())
    return when.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _step_back(when, unit: str, count: int):
    if unit == "day":
        return when - datetime.timedelta(days=count)
    if unit == "week":
        return when - datetime.timedelta(weeks=count)
    # Months vary in length, so walk them rather than subtracting a fixed number of days.
    year, month = when.year, when.month - count
    while month <= 0:
        month += 12
        year -= 1
    return when.replace(year=year, month=month)


def _spells(guild_id: int) -> list:
    """Every spell the window still holds for this server, newest first."""
    return list(_memberships()
                .find({"guild_id": guild_id})
                .sort("joined_at", -1)
                .limit(MAX_SPELLS_READ))


def retention_by_invite(guild_id: int, spells: list = None) -> dict:
    """How many of each invite's joins were still here a week later.

    The point of the page: which promotion brings people who stay, rather than which brings
    the most people. An invite pulling in two hundred members who all leave is worth less than
    one bringing twenty who don't, and a join count alone cannot tell you that.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    spells = _spells(guild_id) if spells is None else spells

    rows = {}
    for spell in spells:
        code = spell.get("invite_code")
        row = rows.setdefault(code, {"code": code, "inviter": None, "joins": 0,
                                     "still_here": 0, "measurable": 0, "survived": 0})
        # Taken from whichever spell has one: an invite whose author has since left still has
        # a name on the older joins.
        row["inviter"] = row["inviter"] or spell.get("inviter_name")
        row["joins"] += 1
        if spell.get("left_at") is None:
            row["still_here"] += 1
        if _measurable(spell, 7, now):
            row["measurable"] += 1
            if _survived(spell, 7):
                row["survived"] += 1

    for row in rows.values():
        # None rather than zero where nothing can be said yet, so the page shows a dash
        # instead of a 0% that reads as a terrible invite.
        row["rate"] = (round(row["survived"] / row["measurable"] * 100)
                       if row["measurable"] else None)

    # Best keep rate first, then biggest. An invite with nothing measurable yet sorts last
    # rather than at either extreme, since it is neither good nor bad news.
    ordered = sorted(rows.values(),
                     key=lambda r: (r["rate"] is not None, r["rate"] or 0, r["joins"]),
                     reverse=True)
    known = [r for r in ordered if r["code"] is not None]
    unknown = next((r for r in ordered if r["code"] is None), None)
    return {"invites": known, "unknown": unknown,
            "total": sum(r["joins"] for r in ordered)}


def retention_trend(guild_id: int, period: str = DEFAULT_TREND, spells: list = None) -> dict:
    """The seven day figure bucket by bucket, so a direction is visible rather than a point.

    One number tells you where you are. This tells you whether what you changed worked, which
    is the question anybody looking at retention is actually asking.
    """
    period = period if period in TREND_PERIODS else DEFAULT_TREND
    unit, count, label_fmt, heading = TREND_PERIODS[period]
    now = datetime.datetime.now(datetime.timezone.utc)
    spells = _spells(guild_id) if spells is None else spells

    # Oldest first, so the chart reads left to right.
    current = _period_start(now, unit)
    starts = [_step_back(current, unit, n) for n in range(count - 1, -1, -1)]
    buckets = {start: {"start": start, "label": start.strftime(label_fmt).lstrip("0"),
                       "joins": 0, "measurable": 0, "survived": 0} for start in starts}

    for spell in spells:
        joined = _aware(spell.get("joined_at"))
        if joined is None:
            continue
        bucket = buckets.get(_period_start(joined, unit))
        if bucket is None:
            continue                      # older than the chart reaches
        bucket["joins"] += 1
        if _measurable(spell, 7, now):
            bucket["measurable"] += 1
            if _survived(spell, 7):
                bucket["survived"] += 1

    points = []
    for start in starts:
        bucket = buckets[start]
        # A bucket whose members are all younger than seven days has no rate yet. Drawn as a
        # gap rather than as zero, which would look like a collapse.
        bucket["rate"] = (round(bucket["survived"] / bucket["measurable"] * 100)
                          if bucket["measurable"] else None)
        points.append(bucket)

    rated = [p["rate"] for p in points if p["rate"] is not None]
    return {
        "period": period,
        "heading": heading,
        "points": points,
        "joins": sum(p["joins"] for p in points),
        # The direction, which is the one thing anybody wants off this chart. Measured across
        # the rated buckets only, and only when there are two to compare.
        "change": (rated[-1] - rated[0]) if len(rated) >= 2 else None,
        "latest": rated[-1] if rated else None,
    }


# The two series the activity chart can draw, and which key each reads.
SERIES = {
    "joins": "Joined",
    "leaves": "Left",
    "both": "Both",
}
DEFAULT_SERIES = "joins"


def activity_trend(guild_id: int, period: str = DEFAULT_TREND, spells: list = None) -> dict:
    """How many joined and how many left, bucket by bucket.

    A join is counted in the bucket it happened in, and so is a leave, which means the two
    series are independent: somebody who joined in March and left in July is one point on each
    of two different bars. Counting a leave against the bucket they joined in would answer a
    different question, and it is the one the survival figures already answer.

    Zero is a real answer here, unlike the retention chart where a bucket can have no answer
    at all, so these lines never break.
    """
    period = period if period in TREND_PERIODS else DEFAULT_TREND
    unit, count, label_fmt, heading = TREND_PERIODS[period]
    now = datetime.datetime.now(datetime.timezone.utc)
    spells = _spells(guild_id) if spells is None else spells

    current = _period_start(now, unit)
    starts = [_step_back(current, unit, n) for n in range(count - 1, -1, -1)]
    buckets = {start: {"start": start, "label": start.strftime(label_fmt).lstrip("0"),
                       "joins": 0, "leaves": 0} for start in starts}

    for spell in spells:
        joined = _aware(spell.get("joined_at"))
        if joined is not None:
            bucket = buckets.get(_period_start(joined, unit))
            if bucket is not None:
                bucket["joins"] += 1
        left = _aware(spell.get("left_at"))
        if left is not None:
            bucket = buckets.get(_period_start(left, unit))
            if bucket is not None:
                bucket["leaves"] += 1

    points = [buckets[start] for start in starts]
    return {
        "period": period,
        "heading": heading,
        "points": points,
        "joins": sum(p["joins"] for p in points),
        "leaves": sum(p["leaves"] for p in points),
        # Both series share one axis, so it has to reach the taller of them or the shorter
        # one would be drawn against a scale it doesn't fit.
        "peak": max([max(p["joins"], p["leaves"]) for p in points] or [0]),
    }


def insights(guild_id: int, period: str = DEFAULT_TREND) -> dict:
    """Everything the insights page needs, off one read of the spells."""
    spells = _spells(guild_id)
    now = datetime.datetime.now(datetime.timezone.utc)

    survival = {}
    for days in INSIGHT_WINDOWS:
        measurable = [s for s in spells if _measurable(s, days, now)]
        survived = sum(1 for s in measurable if _survived(s, days))
        survival[days] = {
            "days": days,
            "measurable": len(measurable),
            "survived": survived,
            "rate": round(survived / len(measurable) * 100) if measurable else None,
        }

    return {
        "joins": len(spells),
        "still_here": sum(1 for s in spells if s.get("left_at") is None),
        "survival": [survival[d] for d in INSIGHT_WINDOWS],
        # Every period, not just the one asked for. The chart switches between them in the
        # browser without going back to the server, so they all have to be on the page, and
        # they all come off the one read of the spells above.
        "activity": {name: activity_trend(guild_id, name, spells) for name in TREND_PERIODS},
        # Retention still feeds the headline figure and its direction. It stopped being the
        # chart because joins and leaves are what somebody opens this page to see.
        #
        # Fixed to weekly rather than following the chart's period, so switching the chart
        # cannot leave a stale figure sitting above it.
        "trend": retention_trend(guild_id, DEFAULT_TREND, spells),
        "invites": retention_by_invite(guild_id, spells),
        "window_days": SPELL_WINDOW_DAYS,
        "capped": len(spells) >= MAX_SPELLS_READ,
        # Joins recorded before invite tracking existed carry no code at all. Telling those
        # apart from "it couldn't be worked out" matters: one is history, the other is a
        # permission the server still has to grant.
        "any_attributed": any(s.get("invite_code") for s in spells),
    }
