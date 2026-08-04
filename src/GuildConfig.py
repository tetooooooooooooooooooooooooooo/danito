"""One shared cache for the per-guild settings document, and the indexes it relies on.

Four cogs read the same document out of `servers`. Each used to keep its own TTL cache of it,
so a single message carrying both an attachment and a role mention cost two identical database
reads, and every cog had its own invalidation to remember. They all go through here now, so a
guild's settings are fetched once per TTL no matter how many features are switched on, and a
write from any command invalidates the copy everybody else sees.

Reads are cached even when a guild has no document at all, which is the common case on a public
bot: an unconfigured server costs one read per TTL rather than one per message.
"""

import asyncio
import time
from typing import Optional

import Database

TTL = 300
_cache: dict[int, tuple[dict, float]] = {}


async def _run(fn, *args, **kwargs):
    # pymongo is synchronous, so keep it off the event loop.
    return await asyncio.to_thread(lambda: fn(*args, **kwargs))


def _servers(bot):
    return Database.get_bot_database(bot.MongoClient)["servers"]


async def get(bot, guild_id: int) -> dict:
    """The guild's settings, or an empty dict when it has none. Never returns None, so callers
    can go straight to .get() for the flag they care about."""
    now = time.monotonic()
    hit = _cache.get(guild_id)
    if hit is not None and now - hit[1] < TTL:
        return hit[0]

    try:
        doc = await _run(_servers(bot).find_one, {"guild_id": guild_id})
    except Exception as e:
        print(f"[GuildConfig] read failed for {guild_id}: {e}")
        # Serve the stale copy rather than pretending the guild is unconfigured, which would
        # silently switch features off during a database blip.
        return hit[0] if hit is not None else {}

    doc = doc or {}
    _cache[guild_id] = (doc, now)
    return doc


async def update(bot, guild_id: int, values: Optional[dict] = None,
                 unset: Optional[dict] = None, add_to_set: Optional[dict] = None,
                 pull: Optional[dict] = None):
    """Apply a change and drop the cached copy so every cog sees it immediately."""
    ops = {}
    if values:
        ops["$set"] = values
    if unset:
        ops["$unset"] = unset
    if add_to_set:
        ops["$addToSet"] = add_to_set
    if pull:
        ops["$pull"] = pull
    if ops:
        await _run(_servers(bot).update_one, {"guild_id": guild_id}, ops, upsert=True)
    invalidate(guild_id)


def invalidate(guild_id: int):
    _cache.pop(guild_id, None)


def prune():
    """Drop entries nobody has looked at in a while, so the dict can't grow without bound."""
    now = time.monotonic()
    for key in [k for k, v in _cache.items() if now - v[1] > TTL * 4]:
        _cache.pop(key, None)


def stats() -> dict:
    return {"cached_guilds": len(_cache)}


async def ensure_indexes(bot):
    """The three original collections never had any index, despite being the most-read ones:
    `servers` by four cogs, `roles` on every member join, `departures` on every join and leave.
    Every lookup was a collection scan."""
    db = Database.get_bot_database(bot.MongoClient)

    def build():
        db["servers"].create_index([("guild_id", 1)], name="guild_id")
        # mention_players queries by date alone (the scheduled pass) and by date plus guild
        # (/forcesurvey); a compound index starting with date serves both.
        db["roles"].create_index([("date", 1), ("guild_id", 1)], name="date_guild")
        db["roles"].create_index([("guild_id", 1), ("mentioned", 1)], name="guild_mentioned")
        db["departures"].create_index([("guild_id", 1), ("user_id", 1)], name="guild_user")
        db["departures"].create_index([("departure_time", 1)], name="departure_time")

    try:
        await _run(build)
    except Exception as e:
        print(f"[GuildConfig] index setup failed: {e}")
