"""Logs deleted media into a single channel.

Discord destroys an attachment's CDN url the moment its message is deleted, so the bytes are
downloaded while the message is still alive and held in a bounded in-memory cache. On delete
the cached copy is re-uploaded straight into the log channel, attached to the embed — one
channel, no separate archive.

The tradeoff that buys the simplicity: coverage is whatever is in the cache. A message deleted
after a bot restart, or after the cache has rotated, still logs who/what/when if discord.py's
own message cache remembers it, but says plainly that the file wasn't retained.

Scope is images, video and audio, from members and bots alike.
"""

import asyncio
import datetime
import io
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import Database
import GuildConfig
from Brand import MINT

# Tuning. These bound memory: a public bot can't hold every upload from every server.
MAX_FILE_BYTES = 8 * 1024 * 1024        # skip caching anything larger
MAX_CACHE_BYTES = 96 * 1024 * 1024      # total across all guilds
MAX_CACHE_ENTRIES = 400
CACHE_TTL = 12 * 3600                   # drop entries this old
MAX_FILES_PER_LOG = 10                  # Discord's per-message attachment limit
MAX_BULK_LOGS = 8                       # individual logs before collapsing to a summary
# An embed holds one image. Several embeds in the same message that carry the same `url` are
# drawn by the client as one embed with a grid of pictures, which is the only way to show more
# than one. Four is where the grid stops.
GALLERY_MAX = 4
AUDIT_DELAY = 1.5
AUDIT_WINDOW = 20

MEDIA_TYPES = ("image/", "video/", "audio/")
MEDIA_EXT = re.compile(
    r"\.(png|jpe?g|gif|webp|bmp|tiff?|heic|heif|avif"
    r"|mp4|mov|webm|mkv|avi|m4v|wmv|flv|mpe?g|3gp"
    r"|mp3|ogg|oga|opus|wav|m4a|aac|flac|weba)$",
    re.IGNORECASE,
)
UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def _is_media(att: discord.Attachment) -> bool:
    ctype = (att.content_type or "").lower()
    if ctype.startswith(MEDIA_TYPES):
        return True
    return bool(MEDIA_EXT.search(att.filename))


def _media_of(message: discord.Message) -> list:
    return [a for a in message.attachments if _is_media(a)]


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    unit, scaled = ("KB", n / 1024) if n < 1024 * 1024 else ("MB", n / (1024 * 1024))
    # 3 MB rather than 3.0 MB. The decimal is only worth printing when it says something.
    return f"{scaled:.1f}".rstrip("0").rstrip(".") + f" {unit}"


def _kind(content_type: Optional[str], filename: str) -> str:
    ctype = (content_type or "").lower()
    if ctype.startswith("image/"):
        return "image"
    if ctype.startswith("video/"):
        return "video"
    if ctype.startswith("audio/"):
        return "audio file"
    return "file"


def _summarise(items: list) -> str:
    counts: dict[str, int] = {}
    for i in items:
        k = _kind(i.content_type, i.filename)
        counts[k] = counts.get(k, 0) + 1
    return ", ".join(f"{n} {k}" if n == 1 else f"{n} {k}s" for k, n in counts.items()) or "media"


# One glyph at the head of the line, so a channel full of these can be read down the left edge
# instead of one title at a time. Mixed messages take the first kind they contain.
KIND_ICONS = {"image": "🖼️", "video": "🎞️", "audio file": "🎧", "file": "📎"}


def _not_kept(f) -> str:
    """Why a file has no bytes behind it, worked out from what we know rather than stored.

    Two very different causes used to share one word. Somebody deleting a phone photo wants to
    be told it was over the size limit, not left wondering whether the bot is broken.
    """
    if f.size > MAX_FILE_BYTES:
        return f"over {_fmt_size(MAX_FILE_BYTES)}, not kept"
    return "not kept, posted before a restart"


def _lead_icon(items: list) -> str:
    for i in items:
        icon = KIND_ICONS.get(_kind(i.content_type, i.filename))
        if icon:
            return icon
    return KIND_ICONS["file"]


@dataclass
class CachedFile:
    filename: str
    data: Optional[bytes]          # None when the file was too large to retain
    content_type: Optional[str]
    size: int
    spoiler: bool


@dataclass
class CachedMessage:
    guild_id: int
    channel_id: int
    author_id: int
    author_tag: str
    author_avatar: Optional[str]
    author_bot: bool
    content: str
    created_at: datetime.datetime
    files: list = field(default_factory=list)
    nbytes: int = 0
    cached_at: float = 0.0


class MediaLog(commands.Cog):
    """Logs deleted images, videos and audio to one channel."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cache: "OrderedDict[int, CachedMessage]" = OrderedDict()
        self._bytes = 0
        self._log_channels: set[int] = set()
        self._pending: dict[int, asyncio.Task] = {}
        self.stats = {"cached": 0, "logged": 0, "too_big": 0, "failed": 0}

    @property
    def servers(self):
        return Database.get_bot_database(self.bot.MongoClient)["servers"]

    async def _db(self, fn, *args, **kwargs):
        return await asyncio.to_thread(lambda: fn(*args, **kwargs))

    async def cog_load(self):
        try:
            self._log_channels = await self._db(self._warm)
        except Exception as e:
            print(f"[MediaLog] startup DB error: {e}")
        self.prune.start()

    async def cog_unload(self):
        self.prune.cancel()
        self._cache.clear()
        self._bytes = 0

    def _warm(self) -> set:
        return {
            d["medialog_channel"]
            for d in self.servers.find({"medialog_channel": {"$ne": None}}, {"medialog_channel": 1})
            if d.get("medialog_channel")
        }

    # ── config ───────────────────────────────────────────────────────
    async def _get_config(self, guild_id: int) -> Optional[dict]:
        """None when logging is off here. Backed by the shared GuildConfig cache, so this
        costs nothing when another cog has already read this guild's settings."""
        doc = await GuildConfig.get(self.bot, guild_id)
        if not (doc.get("medialog_enabled") and doc.get("medialog_channel")):
            return None
        self._log_channels.add(doc["medialog_channel"])
        return doc

    # ── capture ──────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or not message.attachments:
            return
        if self.bot.user is not None and message.author.id == self.bot.user.id:
            return                                    # our own log uploads
        if message.channel.id in self._log_channels:
            return                                    # never mirror the log channel itself
        media = _media_of(message)
        if not media:
            return

        cfg = await self._get_config(message.guild.id)
        if cfg is None:
            return

        task = asyncio.create_task(self._capture(message, media))
        self._pending[message.id] = task
        task.add_done_callback(lambda _t, mid=message.id: self._pending.pop(mid, None))

    async def _capture(self, message: discord.Message, media: list):
        entry = CachedMessage(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            author_id=message.author.id,
            author_tag=str(message.author),
            author_avatar=message.author.display_avatar.url,
            author_bot=message.author.bot,
            content=(message.content or "")[:900],
            created_at=message.created_at,
            cached_at=time.monotonic(),
        )

        for att in media:
            if att.size > MAX_FILE_BYTES:
                # Record that it existed, but don't spend memory on it.
                entry.files.append(CachedFile(att.filename, None, att.content_type,
                                              att.size, att.is_spoiler()))
                self.stats["too_big"] += 1
                continue
            try:
                data = await att.read()
            except discord.NotFound:
                entry.files.append(CachedFile(att.filename, None, att.content_type,
                                              att.size, att.is_spoiler()))
                continue
            except Exception as e:
                print(f"[MediaLog] download failed for {att.filename}: {e}")
                self.stats["failed"] += 1
                entry.files.append(CachedFile(att.filename, None, att.content_type,
                                              att.size, att.is_spoiler()))
                continue
            entry.files.append(CachedFile(att.filename, data, att.content_type,
                                          att.size, att.is_spoiler()))
            entry.nbytes += len(data)

        self._cache[message.id] = entry
        self._bytes += entry.nbytes
        self.stats["cached"] += 1
        self._evict()

    async def capture_now(self, message: discord.Message):
        """For cogs that are about to delete a message themselves — grabs the bytes before the
        CDN url dies. Cog listeners run concurrently with no ordering guarantee, so an
        auto-moderation delete can't rely on our on_message having run first."""
        if message.guild is None or message.id in self._cache:
            return
        media = _media_of(message)
        if not media:
            return
        if await self._get_config(message.guild.id) is None:
            return
        existing = self._pending.get(message.id)
        if existing is not None:
            await existing
            return
        await self._capture(message, media)

    def _evict(self):
        """Oldest-first eviction until back under the entry and byte ceilings."""
        while self._cache and (
            len(self._cache) > MAX_CACHE_ENTRIES or self._bytes > MAX_CACHE_BYTES
        ):
            _, old = self._cache.popitem(last=False)
            self._bytes -= old.nbytes

    def _drop(self, message_id: int) -> Optional[CachedMessage]:
        entry = self._cache.pop(message_id, None)
        if entry is not None:
            self._bytes -= entry.nbytes
        return entry

    @tasks.loop(minutes=10)
    async def prune(self):
        cutoff = time.monotonic() - CACHE_TTL
        for mid in [k for k, v in self._cache.items() if v.cached_at < cutoff]:
            self._drop(mid)
        GuildConfig.prune()

    @prune.before_loop
    async def before_prune(self):
        await self.bot.wait_until_ready()

    # ── attribution ──────────────────────────────────────────────────
    async def _who_deleted(self, guild, channel_id, author_id) -> Optional[str]:
        """Best effort. A member deleting their own message produces no audit entry at all,
        and Discord coalesces entries, so this is never presented as authoritative."""
        if guild is None or guild.me is None or not guild.me.guild_permissions.view_audit_log:
            return None
        cutoff = discord.utils.utcnow() - datetime.timedelta(seconds=AUDIT_WINDOW)
        try:
            async for e in guild.audit_logs(limit=5,
                                            action=discord.AuditLogAction.message_delete):
                if e.created_at < cutoff:
                    break
                ch = getattr(e.extra, "channel", None)
                if ch is not None and ch.id != channel_id:
                    continue
                if author_id is not None and e.target is not None and e.target.id != author_id:
                    continue
                return f"{e.user} (`{e.user.id}`)" if e.user else None
        except (discord.Forbidden, discord.HTTPException):
            return None
        return None

    # ── delete ───────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if payload.guild_id is None or payload.channel_id in self._log_channels:
            return
        cfg = await self._get_config(payload.guild_id)
        if cfg is None:
            return

        # A file deleted seconds after posting may still be downloading.
        pending = self._pending.get(payload.message_id)
        if pending is not None:
            try:
                await asyncio.wait_for(asyncio.shield(pending), timeout=20)
            except Exception:
                pass

        entry = self._drop(payload.message_id)
        if entry is None:
            entry = self._from_cached_message(payload.cached_message)
        if entry is None:
            return

        guild = self.bot.get_guild(payload.guild_id)
        await asyncio.sleep(AUDIT_DELAY)
        who = await self._who_deleted(guild, payload.channel_id, entry.author_id)
        await self._send(guild, cfg, entry, who, discord.utils.utcnow())

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        if payload.guild_id is None or payload.channel_id in self._log_channels:
            return
        cfg = await self._get_config(payload.guild_id)
        if cfg is None:
            return

        entries, handled = [], set()
        for mid in payload.message_ids:
            e = self._drop(mid)
            if e is not None:
                entries.append(e)
                handled.add(mid)

        # Fall back to discord.py's cache for anything we had no bytes for. Deduped on the
        # message id and nothing else: _drop has already emptied our own cache of everything
        # above, so testing self._cache here finds nothing, and the author test that used to
        # stand in for it threw away every other message the same person had in the batch. Ten
        # images purged from one spammer logged the one we happened to hold and dropped the
        # rest without a word.
        for m in payload.cached_messages:
            if m.id in handled or not _media_of(m):
                continue
            e = self._from_cached_message(m)
            if e:
                entries.append(e)
                handled.add(m.id)
        if not entries:
            return

        guild = self.bot.get_guild(payload.guild_id)
        now = discord.utils.utcnow()
        channel = self._log_channel(guild, cfg)
        if channel is None:
            return

        total_files = sum(len(e.files) for e in entries)
        summary = discord.Embed(
            title="Bulk delete",
            description=f"**{len(entries)}** message{'' if len(entries) == 1 else 's'} carrying "
                        f"**{total_files}** file{'' if total_files == 1 else 's'} went at once "
                        f"in <#{payload.channel_id}>. Each one follows.",
            color=MINT,
            timestamp=now,
        )
        try:
            await channel.send(embed=summary, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            return

        for entry in entries[:MAX_BULK_LOGS]:
            await self._send(guild, cfg, entry, "Bulk delete / purge", now)
        if len(entries) > MAX_BULK_LOGS:
            try:
                await channel.send(
                    f"-# …and {len(entries) - MAX_BULK_LOGS} more not shown individually.")
            except discord.HTTPException:
                pass

    def _from_cached_message(self, message: Optional[discord.Message]) -> Optional[CachedMessage]:
        """Fall back to discord.py's own message cache: we lose the bytes but keep who/what/when."""
        if message is None or message.guild is None:
            return None
        media = _media_of(message)
        if not media:
            return None
        return CachedMessage(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            author_id=message.author.id,
            author_tag=str(message.author),
            author_avatar=message.author.display_avatar.url,
            author_bot=message.author.bot,
            content=(message.content or "")[:900],
            created_at=message.created_at,
            files=[CachedFile(a.filename, None, a.content_type, a.size, a.is_spoiler())
                   for a in media],
            cached_at=time.monotonic(),
        )

    def _log_channel(self, guild, cfg):
        if guild is None:
            return None
        return guild.get_channel(cfg["medialog_channel"])

    async def _send(self, guild, cfg, entry: CachedMessage, who: Optional[str],
                    when: datetime.datetime):
        channel = self._log_channel(guild, cfg)
        if channel is None:
            return

        retained = [f for f in entry.files if f.data is not None]
        files, shown, used = [], [], set()

        for i, f in enumerate(retained[:MAX_FILES_PER_LOG]):
            safe = UNSAFE_NAME.sub("_", f.filename) or "file"
            # Two files can sanitise to the same name, and then attachment:// picks whichever
            # Discord decides. "IMG 1.png" and "IMG_1.png" is all it takes.
            if safe in used:
                safe = f"{i}_{safe}"
            used.add(safe)
            files.append(discord.File(io.BytesIO(f.data), filename=safe, spoiler=f.spoiler))
            # Referencing an attachment draws it inside the embed rather than as a separate
            # block below it. Spoilers are left out so they stay blurred.
            if (not f.spoiler and (f.content_type or "").lower().startswith("image/")
                    and len(shown) < GALLERY_MAX):
                shown.append(f"attachment://{safe}")

        # The whole entry in two lines and a picture. It used to be a grid of five fields, three
        # of which repeated the author header or said "no text, just the file" on every single
        # image anybody ever deleted. What is left is what somebody scrolling the log needs.
        embed = discord.Embed(title="Media deleted", color=MINT, timestamp=when)
        embed.set_author(
            name=entry.author_tag + (" · bot" if entry.author_bot else ""),
            icon_url=entry.author_avatar or None)

        posted = int(entry.created_at.timestamp())
        # Discord writes no audit entry when somebody deletes their own message, so unknown is
        # the ordinary case rather than a failure. Say which it is without a field spent on it.
        by = f"deleted by {who}" if who else "nobody named, so probably the author"
        embed.description = (
            f"{_lead_icon(entry.files)} **{_summarise(entry.files)}** in <#{entry.channel_id}>\n"
            f"-# posted <t:{posted}:R> · {by}")

        if entry.content:
            embed.add_field(name="Caption", value=entry.content[:1000], inline=False)

        # Every picture, not just the first. Embeds sharing a url are drawn as one embed with
        # a grid, so the extras carry nothing but the url and their image. The url is a link
        # to the channel it happened in, which is always valid and worth clicking, and it is
        # only set when there is a grid to make so single entries look exactly as before.
        gallery = []
        if shown:
            embed.set_image(url=shown[0])
        if len(shown) > 1:
            link = f"https://discord.com/channels/{entry.guild_id}/{entry.channel_id}"
            embed.url = link
            for ref in shown[1:]:
                extra = discord.Embed(url=link)
                extra.set_image(url=ref)
                gallery.append(extra)

        missing = len(entry.files) - len(retained)
        # The list earns its place only when something is not already on show: a file we could
        # not keep, a video, a spoiler, or more pictures than the grid holds. Naming files that
        # are visible directly underneath tells nobody anything.
        if len(shown) != len(entry.files):
            lines = [f"`{f.filename}` · {_fmt_size(f.size)}"
                     + ("" if f.data is not None else f" · {_not_kept(f)}")
                     for f in entry.files]
            embed.add_field(name="Files", value="\n".join(lines)[:1024], inline=False)

        # The id lives here rather than in a field of its own. It is wanted rarely, usually
        # once the person has left and their mention is a dead link, and it is still copyable.
        note = [f"ID {entry.author_id}"]
        if missing:
            shown = len(entry.files) - missing
            note.append(f"{shown} of {len(entry.files)} files kept")
        embed.set_footer(text=" · ".join(note))

        try:
            await channel.send(embeds=[embed, *gallery], files=files,
                               allowed_mentions=discord.AllowedMentions.none())
            self.stats["logged"] += 1
        except discord.Forbidden:
            print(f"[MediaLog] missing permissions in log channel for guild {entry.guild_id}")
        except discord.HTTPException as e:
            print(f"[MediaLog] send failed: {e}")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        cfg = await self._get_config(channel.guild.id)
        if cfg and cfg.get("medialog_channel") == channel.id:
            await GuildConfig.update(self.bot, channel.guild.id,
                                     {"medialog_enabled": False})
            self._log_channels.discard(channel.id)

    # Switching this on and off lives in the Logging cog, under /logging media, so all four
    # logs are configured from one place instead of four differently-named commands.


async def setup(bot: commands.Bot):
    await bot.add_cog(MediaLog(bot))
    print("MediaLog cog loaded ✓")
