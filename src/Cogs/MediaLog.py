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

# Tuning. These bound memory: a public bot can't hold every upload from every server.
MAX_FILE_BYTES = 8 * 1024 * 1024        # skip caching anything larger
MAX_CACHE_BYTES = 96 * 1024 * 1024      # total across all guilds
MAX_CACHE_ENTRIES = 400
CACHE_TTL = 12 * 3600                   # drop entries this old
MAX_FILES_PER_LOG = 10                  # Discord's per-message attachment limit
MAX_BULK_LOGS = 8                       # individual logs before collapsing to a summary
AUDIT_DELAY = 1.5
AUDIT_WINDOW = 20

COLOR_DELETE = 0xE74C3C
COLOR_INFO = 0x5865F2
COLOR_WARN = 0xE67E22

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
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


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

        entries = []
        for mid in payload.message_ids:
            e = self._drop(mid)
            if e is not None:
                entries.append(e)
        known = {e.author_id for e in entries}
        for m in payload.cached_messages:
            if m.id not in self._cache and _media_of(m) and m.author.id not in known:
                e = self._from_cached_message(m)
                if e:
                    entries.append(e)
        if not entries:
            return

        guild = self.bot.get_guild(payload.guild_id)
        now = discord.utils.utcnow()
        channel = self._log_channel(guild, cfg)
        if channel is None:
            return

        total_files = sum(len(e.files) for e in entries)
        summary = discord.Embed(
            title="🗑️ Bulk Delete",
            description=f"**{len(entries)}** message(s) with **{total_files}** media file(s) "
                        f"were bulk-deleted in <#{payload.channel_id}>.",
            color=COLOR_DELETE,
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
        files, inline_image = [], None

        for f in retained[:MAX_FILES_PER_LOG]:
            safe = UNSAFE_NAME.sub("_", f.filename) or "file"
            files.append(discord.File(io.BytesIO(f.data), filename=safe, spoiler=f.spoiler))
            # Referencing an attachment renders it inside the embed rather than as a
            # separate block. Spoilers are skipped so they stay blurred.
            if (inline_image is None and not f.spoiler
                    and (f.content_type or "").lower().startswith("image/")):
                inline_image = f"attachment://{safe}"

        embed = discord.Embed(
            title="🗑️ Deleted Media",
            description=f"**{_summarise(entry.files)}** in <#{entry.channel_id}>",
            color=COLOR_DELETE,
            timestamp=when,
        )
        if entry.author_avatar:
            embed.set_author(name=entry.author_tag, icon_url=entry.author_avatar)

        uploader = f"<@{entry.author_id}>\n`{entry.author_id}`"
        if entry.author_bot:
            uploader += "\n*(bot)*"
        embed.add_field(name="Uploaded by", value=uploader, inline=True)
        embed.add_field(name="Deleted by", value=who or "Unknown, probably the author", inline=True)

        posted = int(entry.created_at.timestamp())
        embed.add_field(name="Posted", value=f"<t:{posted}:f>\n<t:{posted}:R>", inline=True)

        embed.add_field(
            name="Message text",
            value=entry.content[:1000] if entry.content else "*(no text, just the file)*",
            inline=False,
        )

        lines = []
        for f in entry.files:
            mark = "" if f.data is not None else "  ⚠️ *not retained*"
            lines.append(f"`{f.filename}` · {_fmt_size(f.size)}{mark}")
        embed.add_field(name="Files", value="\n".join(lines)[:1024], inline=False)

        if inline_image:
            embed.set_image(url=inline_image)

        missing = len(entry.files) - len(retained)
        if missing:
            embed.set_footer(text=f"{missing} file(s) couldn't be retained "
                                  f"(too large, or posted before the bot restarted)")

        try:
            await channel.send(embed=embed, files=files,
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

    # ── commands ─────────────────────────────────────────────────────
    @app_commands.command(name="logchannel",
                          description="Set the channel where deleted media gets logged")
    @app_commands.describe(channel="Where to post the logs")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def logchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        guild = interaction.guild
        perms = channel.permissions_for(guild.me)
        missing = [n for n, ok in (
            ("View Channel", perms.view_channel),
            ("Send Messages", perms.send_messages),
            ("Embed Links", perms.embed_links),
            ("Attach Files", perms.attach_files),
        ) if not ok]
        if missing:
            await interaction.response.send_message(
                f"❌ I'm missing **{', '.join(missing)}** in {channel.mention}. "
                f"Grant those and run this again.", ephemeral=True)
            return

        await GuildConfig.update(self.bot, guild.id,
                                 {"medialog_enabled": True, "medialog_channel": channel.id})
        self._log_channels.add(channel.id)

        embed = discord.Embed(
            title="✅ Media logging enabled",
            description=f"Deleted images, videos and audio will be posted to "
                        f"{channel.mention}, from members and bots alike.",
            color=COLOR_INFO,
        )
        if not guild.me.guild_permissions.view_audit_log:
            embed.add_field(
                name="⚠️ Heads up",
                value="I don't have **View Audit Log**, so \"Deleted by\" will always say "
                      "unknown. Grant it if you want to see who removed something.",
                inline=False)
        embed.set_footer(text="Coverage starts now. Files posted before this aren't kept.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="logoff", description="Stop logging deleted media")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def logoff(self, interaction: discord.Interaction):
        await GuildConfig.update(self.bot, interaction.guild.id,
                                 {"medialog_enabled": False})
        await interaction.response.send_message(
            "🔴 Media logging is off. Run `/logchannel` to turn it back on.", ephemeral=True)

    @app_commands.command(name="logstatus", description="Show the media logging setup")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def logstatus(self, interaction: discord.Interaction):
        guild = interaction.guild
        doc = await GuildConfig.get(self.bot, guild.id)
        enabled = bool(doc.get("medialog_enabled") and doc.get("medialog_channel"))
        channel = guild.get_channel(doc.get("medialog_channel") or 0)

        embed = discord.Embed(
            title="🗄️ Media Logging",
            description="🟢 **Enabled**" if enabled else "🔴 **Disabled**. Run `/logchannel` to switch it on.",
            color=COLOR_INFO if enabled else COLOR_WARN,
        )
        embed.add_field(name="Log channel",
                        value=channel.mention if channel else "*not set*", inline=True)
        embed.add_field(name="Audit log",
                        value="✅ available" if guild.me.guild_permissions.view_audit_log
                        else "⚠️ missing, so deleters show as unknown", inline=True)
        embed.add_field(
            name="What gets logged",
            value=f"Images, video and audio up to {_fmt_size(MAX_FILE_BYTES)}, "
                  f"from members and bots.",
            inline=False)
        mine = sum(1 for e in self._cache.values() if e.guild_id == guild.id)
        embed.add_field(
            name="Currently held",
            value=f"{mine} message(s) from this server\n"
                  f"-# {len(self._cache)} total • {_fmt_size(self._bytes)} in memory",
            inline=False)
        embed.set_footer(text="Files are held in memory and rotate out after ~12h or a restart.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(MediaLog(bot))
    print("MediaLog cog loaded ✓")
