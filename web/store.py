"""The dashboard's view of the same MongoDB the bot uses.

Deliberately its own small module rather than importing the bot's GuildConfig: that one caches
per process and expects a bot object. Two processes sharing one cache would be a lie, so the
dashboard reads through every time and tells the bot when something changed.
"""

import datetime
import os

import certifi
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
}
MAX_TEXT = 1500


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


def clean(field: str, raw, valid_channels: set):
    """Coerce one submitted value, rejecting anything that isn't allowed.

    Channel ids are checked against the guild's real channels, so a crafted post can't point
    the bot at a channel in a different server.
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
    raise ValueError(field)


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
