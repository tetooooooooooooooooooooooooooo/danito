"""The dashboard's view of the same MongoDB the bot uses.

Deliberately its own small module rather than importing the bot's GuildConfig: that one caches
per process and expects a bot object. Two processes sharing one cache would be a lie, so the
dashboard reads through every time and tells the bot when something changed.
"""

import datetime
import os

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
