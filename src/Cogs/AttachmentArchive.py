"""Mirrors uploaded media into a private archive channel and logs deletions.

Scope is images, videos and voice memos only — documents, archives and other file types are
ignored entirely and never recorded.

Discord invalidates an attachment's CDN URL the moment its message is deleted, so the bytes
have to be captured at upload time. And `on_message_delete` only fires for messages still in
discord.py's in-memory cache (wiped on every restart), so deletions are detected through the
*raw* events plus our own Mongo index keyed by message id. That combination is what makes a
two-year-old deletion log correctly.
"""

import asyncio
import datetime
import re
import time
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands, tasks

import Database

MAX_WORKERS = 2
QUEUE_MAXSIZE = 200
CFG_TTL = 300           # seconds to cache a guild's config (None is cached too)
AUDIT_DELAY = 1.5       # let the audit log settle before querying it
AUDIT_WINDOW = 20       # ignore audit entries older than this
FILES_PER_MSG = 10      # Discord hard limit
SIZE_HEADROOM = 512 * 1024
CONTENT_LIMIT = 500
WARN_INTERVAL = 3600

COLOR_DELETE = 0xE74C3C
COLOR_INFO = 0x2B2D31
COLOR_WARN = 0xE67E22

# Scope: images, videos and voice memos. Discord reports voice memos as an audio attachment
# with a duration, so audio/* is included; everything else (documents, archives, code) is not
# tracked at all. content_type is authoritative when present, extension is the fallback for
# the uploads Discord doesn't type.
MEDIA_TYPES = ("image/", "video/", "audio/")
MEDIA_EXT = re.compile(
    r"\.(png|jpe?g|gif|webp|bmp|tiff?|heic|heif|avif"
    r"|mp4|mov|webm|mkv|avi|m4v|wmv|flv|mpe?g|3gp"
    r"|ogg|oga|opus|mp3|wav|m4a|aac|flac|weba)$",
    re.IGNORECASE,
)


def _is_media(att: discord.Attachment) -> bool:
    ctype = (att.content_type or "").lower()
    if ctype.startswith(MEDIA_TYPES):
        return True
    return bool(MEDIA_EXT.search(att.filename))


def _media_attachments(message: discord.Message) -> list:
    return [a for a in message.attachments if _is_media(a)]


def _kind(entry: dict) -> str:
    if entry.get("duration"):
        return "voice memo"
    ctype = (entry.get("content_type") or "").lower()
    if ctype.startswith("image/"):
        return "image"
    if ctype.startswith("video/"):
        return "video"
    if ctype.startswith("audio/"):
        return "audio file"
    return "file"


def _summarise(entries: list) -> str:
    """'2 images, 1 video' — what was actually deleted, at a glance."""
    counts: dict[str, int] = {}
    for e in entries:
        k = _kind(e)
        counts[k] = counts.get(k, 0) + 1
    return ", ".join(
        f"{n} {k}" if n == 1 else f"{n} {k}s" for k, n in counts.items()
    ) or "nothing"


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _fmt_duration(secs: float) -> str:
    secs = int(secs)
    return f"{secs // 60}:{secs % 60:02d}"


def _aware(dt: datetime.datetime) -> datetime.datetime:
    """pymongo hands back naive UTC datetimes, and treating a naive value as local time would
    shift every rendered timestamp by the host's UTC offset."""
    return dt.replace(tzinfo=datetime.timezone.utc) if dt.tzinfo is None else dt


def _unix(dt: datetime.datetime) -> int:
    return int(_aware(dt).timestamp())


class AttachmentArchive(commands.Cog):
    """Archives attachments on upload and logs them when they are deleted."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._workers: list[asyncio.Task] = []
        self._cfg_cache: dict[int, tuple[Optional[dict], float]] = {}
        # Warmed at startup so the on_message hot path never touches the DB to self-exclude.
        self._archive_channel_ids: set[int] = set()
        self._captures: dict[int, asyncio.Task] = {}
        self._delete_reasons: dict[int, tuple[str, float]] = {}
        self._warned: dict[tuple[int, str], float] = {}
        self._backfilling: set[int] = set()
        self.stats = {"queued": 0, "mirrored": 0, "dropped": 0, "failed": 0, "oversized": 0}

    # ------------------------------------------------------------------ infra

    @property
    def coll(self):
        return Database.get_bot_database(self.bot.MongoClient)["attachment_archive"]

    @property
    def servers(self):
        return Database.get_bot_database(self.bot.MongoClient)["servers"]

    def _ensure_indexes(self):
        coll = self.coll
        coll.create_index([("guild_id", 1), ("created_at", -1)], name="guild_created")
        coll.create_index(
            [("guild_id", 1), ("author_id", 1), ("created_at", -1)], name="guild_author_created"
        )
        coll.create_index(
            [("guild_id", 1), ("created_at", 1)],
            name="undeleted_scan",
            partialFilterExpression={"deleted": False},
        )
        self.servers.create_index([("guild_id", 1)], name="guild_id_idx")

    def _warm_archive_channel_ids(self) -> set[int]:
        return {
            d["archive_channel"]
            for d in self.servers.find({"archive_channel": {"$ne": None}}, {"archive_channel": 1})
            if d.get("archive_channel")
        }

    async def cog_load(self):
        try:
            await asyncio.to_thread(self._ensure_indexes)
            self._archive_channel_ids = await asyncio.to_thread(self._warm_archive_channel_ids)
        except Exception as e:
            print(f"[Archive] startup DB error: {e}")
        self._workers = [asyncio.create_task(self._worker(i)) for i in range(MAX_WORKERS)]
        self.prune_state.start()

    async def cog_unload(self):
        self.prune_state.cancel()
        for t in self._workers:
            t.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)

    # pymongo is synchronous. This cog puts the DB on a hot path (every media message, plus
    # bulk deletes carrying up to 100 ids), and blocking the loop for >10s makes discord.py
    # log "heartbeat blocked" and forces a gateway reconnect. MongoClient is thread-safe with
    # its own pool, so to_thread is the supported way to keep those calls off the loop.
    async def _db(self, fn, *args, **kwargs):
        return await asyncio.to_thread(lambda: fn(*args, **kwargs))

    async def _get_config(self, guild_id: int) -> Optional[dict]:
        """Per-guild config with a TTL cache. Caches ``None`` too, so an unconfigured guild
        costs one DB hit per TTL rather than one per media message."""
        now = time.monotonic()
        hit = self._cfg_cache.get(guild_id)
        if hit is not None and now - hit[1] < CFG_TTL:
            return hit[0]

        try:
            doc = await self._db(self.servers.find_one, {"guild_id": guild_id})
        except Exception as e:
            print(f"[Archive] config lookup failed for {guild_id}: {e}")
            return hit[0] if hit else None

        cfg = doc if (doc and doc.get("archive_enabled")) else None
        self._cfg_cache[guild_id] = (cfg, now)
        if cfg and cfg.get("archive_channel"):
            self._archive_channel_ids.add(cfg["archive_channel"])
        return cfg

    def _invalidate(self, guild_id: int):
        self._cfg_cache.pop(guild_id, None)

    async def _set_config(self, guild_id: int, updates: dict, unset: Optional[dict] = None):
        ops = {"$set": updates}
        if unset:
            ops["$unset"] = unset
        await self._db(self.servers.update_one, {"guild_id": guild_id}, ops, upsert=True)
        self._invalidate(guild_id)

    # --------------------------------------------------------------- capture

    def note_automod_delete(self, message_id: int, reason: str):
        """Synchronous marker so an auto-moderation delete is attributed correctly and can
        opt out of mirroring. Call this *before* awaiting anything."""
        self._delete_reasons[message_id] = (reason, time.monotonic())

    def _pop_delete_reason(self, message_id: int) -> Optional[str]:
        entry = self._delete_reasons.pop(message_id, None)
        return entry[0] if entry else None

    def _peek_delete_reason(self, message_id: int) -> Optional[str]:
        entry = self._delete_reasons.get(message_id)
        return entry[0] if entry else None

    def _start_capture(self, message: discord.Message, reason: Optional[str] = None) -> asyncio.Task:
        """Deduplicates capture across the queue worker and any inline caller, so a message
        is never mirrored twice and an inline caller can await work already in progress."""
        task = self._captures.get(message.id)
        if task is None:
            task = asyncio.create_task(self._capture(message, reason=reason))
            self._captures[message.id] = task
            task.add_done_callback(lambda _t, mid=message.id: self._captures.pop(mid, None))
        return task

    async def capture_now(self, message: discord.Message, reason: Optional[str] = None):
        """Public entry point for other cogs that are about to delete a message themselves."""
        if reason:
            self.note_automod_delete(message.id, reason)
        await self._start_capture(message, reason=reason)

    async def _worker(self, n: int):
        while True:
            message = await self.queue.get()
            try:
                await self._start_capture(message)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.stats["failed"] += 1
                print(f"[Archive] worker {n} error on {message.id}: {e}")
            finally:
                self.queue.task_done()

    def _build_doc(self, message: discord.Message, reason: Optional[str]) -> dict:
        return {
            "guild_id": message.guild.id,
            "channel_id": message.channel.id,
            "parent_channel_id": getattr(message.channel, "parent_id", None),
            "author_id": message.author.id,
            "author_tag": str(message.author),
            "author_avatar": message.author.display_avatar.url,
            "content": (message.content or "")[:CONTENT_LIMIT],
            "created_at": message.created_at,
            "jump_url": message.jump_url,
            "archive_channel_id": None,
            "archive_message_ids": [],
            "attachments": [
                {
                    "id": a.id,
                    "filename": a.filename,
                    "size": a.size,
                    "content_type": a.content_type,
                    "is_spoiler": a.is_spoiler(),
                    "description": a.description,
                    "width": a.width,
                    "height": a.height,
                    "duration": getattr(a, "duration", None),
                    "original_url": a.url,
                    "mirror_url": None,
                    "mirror_message_id": None,
                    "mirror_index": None,
                    "status": "pending",
                }
                for a in _media_attachments(message)
            ],
            "status": "pending",
            "deleted": False,
            "deleted_at": None,
            "delete_reason": reason,
            "deleted_by": None,
            "detected_by": None,
            "indexed_at": discord.utils.utcnow(),
        }

    async def _capture(self, message: discord.Message, reason: Optional[str] = None):
        guild = message.guild
        cfg = await self._get_config(guild.id)
        if cfg is None:
            return

        reason = reason or self._peek_delete_reason(message.id)
        # Auto-moderation deletes are metadata-only by default: that filter catches exactly the
        # raid content you least want the bot re-uploading under its own account.
        mirror = reason is None or bool(cfg.get("archive_mirror_automod", False))

        # Phase 1 — index the metadata before downloading anything, so a delete still logs
        # even if the mirror fails. The upsert is also the cross-task dedupe guard.
        doc = self._build_doc(message, reason)
        try:
            res = await self._db(
                self.coll.update_one, {"_id": message.id}, {"$setOnInsert": doc}, upsert=True
            )
            already_indexed = res.upserted_id is None
        except Exception as e:
            print(f"[Archive] index write failed for {message.id}: {e}")
            return

        if not mirror:
            return
        if already_indexed:
            # Already captured. Only re-attempt the mirror if it never actually ran (e.g. the
            # archive channel was missing at the time) — a finalised "failed"/"expired" is not
            # worth retrying, since the source CDN url is gone by then anyway.
            existing = await self._db(self.coll.find_one, {"_id": message.id}, {"status": 1})
            if not existing or existing.get("status") != "pending":
                return

        # Phase 2 — download and re-upload.
        cap = min(cfg.get("archive_max_bytes") or guild.filesize_limit, guild.filesize_limit)
        archive = guild.get_channel(cfg["archive_channel"]) if cfg.get("archive_channel") else None
        if archive is None:
            await self._warn(guild, cfg, "archive_missing",
                             "The configured archive channel is gone — files are no longer being "
                             "mirrored. Run `/archivesetup` to reconnect it.")
            return

        per_att_status: dict[int, str] = {}
        batches: list[list] = []
        batch: list = []
        batch_bytes = 0

        for att in _media_attachments(message):
            if att.size > cap:
                # Never download something we already know we cannot re-upload.
                per_att_status[att.id] = "oversized"
                self.stats["oversized"] += 1
                continue
            if len(batch) >= FILES_PER_MSG or batch_bytes + att.size > cap - SIZE_HEADROOM:
                if batch:
                    batches.append(batch)
                batch, batch_bytes = [], 0
            try:
                f = await att.to_file()
            except discord.NotFound:
                try:
                    f = await att.to_file(use_cached=True)
                except Exception:
                    per_att_status[att.id] = "expired"
                    continue
            except (discord.HTTPException, OSError) as e:
                print(f"[Archive] download failed for {att.filename}: {e}")
                per_att_status[att.id] = "failed"
                continue
            batch.append((att, f))
            batch_bytes += att.size
        if batch:
            batches.append(batch)

        header = (
            f"`{message.id}` • <@{message.author.id}> in <#{message.channel.id}> "
            f"• <t:{int(message.created_at.timestamp())}:f>"
        )
        mirror_msg_ids: list[int] = []
        url_by_att: dict[int, tuple[str, int, int]] = {}

        for i, b in enumerate(batches):
            try:
                sent = await archive.send(
                    content=header if i == 0 else f"`{message.id}` (cont. {i + 1})",
                    files=[f for _, f in b],
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.Forbidden:
                await self._warn(guild, cfg, "archive_perms",
                                 f"I can't upload to {archive.mention} — files are being logged "
                                 f"but not archived. Check my View Channel / Send Messages / "
                                 f"Attach Files permissions there.")
                if not mirror_msg_ids:
                    # Nothing landed — leave the record at "pending" rather than finalising it,
                    # so /archivebackfill can re-mirror this once the permission is restored.
                    return
                break
            except discord.HTTPException as e:
                print(f"[Archive] upload failed for {message.id}: {e}")
                for att, _ in b:
                    per_att_status.setdefault(att.id, "failed")
                continue
            mirror_msg_ids.append(sent.id)
            for idx, ((att, _), up) in enumerate(zip(b, sent.attachments)):
                url_by_att[att.id] = (up.url, sent.id, idx)
                per_att_status[att.id] = "mirrored"
                self.stats["mirrored"] += 1

        try:
            await self._db(
                self._finalise, message.id, cfg.get("archive_channel"),
                mirror_msg_ids, url_by_att, per_att_status,
            )
        except Exception as e:
            print(f"[Archive] finalise failed for {message.id}: {e}")

    def _finalise(self, message_id, archive_channel_id, mirror_msg_ids, url_by_att, per_att_status):
        doc = self.coll.find_one({"_id": message_id})
        if doc is None:
            return
        atts = doc.get("attachments", [])
        for entry in atts:
            status = per_att_status.get(entry["id"])
            if status:
                entry["status"] = status
            info = url_by_att.get(entry["id"])
            if info:
                entry["mirror_url"], entry["mirror_message_id"], entry["mirror_index"] = info

        statuses = {e.get("status") for e in atts}
        if statuses == {"mirrored"}:
            overall = "mirrored"
        elif "mirrored" in statuses:
            overall = "partial"
        elif statuses and statuses <= {"oversized"}:
            overall = "oversized"
        else:
            overall = "failed"

        self.coll.update_one(
            {"_id": message_id},
            {"$set": {
                "attachments": atts,
                "archive_channel_id": archive_channel_id,
                "archive_message_ids": mirror_msg_ids,
                "status": overall,
            }},
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Every sync check first — an unconfigured or irrelevant message must cost nothing.
        if message.guild is None or not message.attachments:
            return
        if self.bot.user is not None and message.author.id == self.bot.user.id:
            return  # never re-mirror our own mirrors
        if message.channel.id in self._archive_channel_ids:
            return  # skip the archive channel wholesale
        if not _media_attachments(message):
            return  # images, videos and voice memos only

        cfg = await self._get_config(message.guild.id)
        if cfg is None or not cfg.get("archive_channel"):
            return

        ignored = cfg.get("archive_ignored_channels") or []
        if message.channel.id in ignored:
            return
        parent = getattr(message.channel, "parent_id", None)
        if parent and parent in ignored:
            return

        try:
            self.queue.put_nowait(message)
            self.stats["queued"] += 1
        except asyncio.QueueFull:
            self.stats["dropped"] += 1
            await self._warn(message.guild, cfg, "queue_full",
                             "The archive queue is full — some attachments are not being mirrored.")

    # ---------------------------------------------------------------- deletes

    async def _resolve_deleter(self, guild, channel_id, author_id, action) -> Optional[str]:
        """Best effort. Self-deletes produce no audit entry at all, and Discord coalesces
        message_delete entries by (moderator, target, channel), so this can be both a false
        positive and a false negative. Never presented as authoritative."""
        if guild is None or guild.me is None or not guild.me.guild_permissions.view_audit_log:
            return None
        cutoff = discord.utils.utcnow() - datetime.timedelta(seconds=AUDIT_WINDOW)
        try:
            async for entry in guild.audit_logs(limit=5, action=action):
                if entry.created_at < cutoff:
                    break
                if action is discord.AuditLogAction.message_bulk_delete:
                    if entry.target is not None and entry.target.id != channel_id:
                        continue
                else:
                    ch = getattr(entry.extra, "channel", None)
                    if ch is not None and ch.id != channel_id:
                        continue
                    if author_id is not None and entry.target is not None \
                            and entry.target.id != author_id:
                        continue
                if entry.user is None:
                    return None
                return f"{entry.user} ({entry.user.id})"
        except (discord.Forbidden, discord.HTTPException):
            return None
        return None

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if payload.guild_id is None:
            return
        if payload.channel_id in self._archive_channel_ids:
            return  # pruning the archive must not generate "file deleted" logs
        cfg = await self._get_config(payload.guild_id)
        if cfg is None or not cfg.get("archive_log_channel"):
            return

        # A message deleted seconds after posting may still be mid-upload. Wait for its capture
        # to land, or the embed would report "not archived yet" for a file that arrives a moment
        # later. shield() so the timeout doesn't cancel the upload itself.
        capture = self._captures.get(payload.message_id)
        if capture is not None:
            try:
                await asyncio.wait_for(asyncio.shield(capture), timeout=30)
            except Exception:
                pass

        try:
            record = await self._db(self.coll.find_one, {"_id": payload.message_id})
        except Exception as e:
            print(f"[Archive] delete lookup failed for {payload.message_id}: {e}")
            return

        cached = payload.cached_message
        if record is None:
            # Covers the race where a message is deleted before it was ever indexed: the
            # in-memory cache still has the filenames, author and content.
            if cached is None or cached.guild is None or not _media_attachments(cached):
                return
            record = self._build_doc(cached, None)
            record["_id"] = cached.id

        guild = self.bot.get_guild(payload.guild_id)
        reason = self._pop_delete_reason(payload.message_id)
        deleted_by = None
        if reason is None:
            await asyncio.sleep(AUDIT_DELAY)
            deleted_by = await self._resolve_deleter(
                guild, payload.channel_id, record.get("author_id"),
                discord.AuditLogAction.message_delete,
            )

        now = discord.utils.utcnow()
        updates = {
            "deleted": True,
            "deleted_at": now,
            "deleted_by": deleted_by,
            "detected_by": "gateway",
        }
        if reason is not None:
            # Only overwrite when we actually have one — the capture may have stored it already
            # and the in-memory marker expires after a few minutes.
            updates["delete_reason"] = reason
        try:
            await self._db(
                self.coll.update_one, {"_id": payload.message_id}, {"$set": updates}
            )
        except Exception as e:
            print(f"[Archive] delete flag failed for {payload.message_id}: {e}")

        embed = await self._build_delete_embed(guild, record, reason, deleted_by, deleted_at=now)
        await self._send_log(guild, cfg, [embed])

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        if payload.guild_id is None:
            return
        if payload.channel_id in self._archive_channel_ids:
            return
        cfg = await self._get_config(payload.guild_id)
        if cfg is None or not cfg.get("archive_log_channel"):
            return

        ids = list(payload.message_ids)
        try:
            # One query for the whole batch — never a loop of find_one.
            records = await self._db(lambda: list(self.coll.find({"_id": {"$in": ids}})))
        except Exception as e:
            print(f"[Archive] bulk lookup failed: {e}")
            return

        known = {r["_id"]: r for r in records}
        for m in payload.cached_messages:
            if m.id not in known and m.guild is not None and _media_attachments(m):
                doc = self._build_doc(m, None)
                doc["_id"] = m.id
                known[m.id] = doc
        if not known:
            return

        guild = self.bot.get_guild(payload.guild_id)
        # One audit lookup for the whole bulk, not one per message.
        deleted_by = await self._resolve_deleter(
            guild, payload.channel_id, None, discord.AuditLogAction.message_bulk_delete
        )

        now = discord.utils.utcnow()
        try:
            await self._db(
                self.coll.update_many, {"_id": {"$in": ids}},
                {"$set": {
                    "deleted": True,
                    "deleted_at": now,
                    "deleted_by": deleted_by,
                    "detected_by": "gateway",
                }},
            )
        except Exception as e:
            print(f"[Archive] bulk delete flag failed: {e}")

        total_files = sum(len(r.get("attachments") or []) for r in known.values())
        summary = discord.Embed(
            title="🗑️ Bulk Delete",
            description=(f"**{len(known)}** archived message(s) carrying **{total_files}** media "
                         f"file(s) were bulk-deleted in <#{payload.channel_id}>."),
            color=COLOR_DELETE,
            timestamp=discord.utils.utcnow(),
        )
        summary.add_field(name="Deleted by", value=deleted_by or "Unknown", inline=False)

        ordered = sorted(known.values(), key=lambda r: r["_id"])
        details = []
        for record in ordered[:9]:
            details.append(
                await self._build_delete_embed(guild, record, None, deleted_by, deleted_at=now))
        if len(ordered) > 9:
            summary.set_footer(text=f"Showing 9 of {len(ordered)} — use /archivelookup for the rest")

        await self._send_log(guild, cfg, [summary] + details)

    async def _build_delete_embed(self, guild, record, reason, deleted_by,
                                  deleted_at=None) -> discord.Embed:
        atts = record.get("attachments") or []
        mirror_missing = False
        mirror_msgs: dict[int, discord.Message] = {}

        # Stored CDN urls carry ~24h signed params, so they are refetched rather than reused.
        archive_id = record.get("archive_channel_id")
        msg_ids = record.get("archive_message_ids") or []
        if guild is not None and archive_id and msg_ids:
            channel = guild.get_channel(archive_id)
            if channel is None:
                mirror_missing = True
            else:
                for mid in msg_ids:
                    try:
                        mirror_msgs[mid] = await channel.fetch_message(mid)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        mirror_missing = True

        def fresh(entry):
            m = mirror_msgs.get(entry.get("mirror_message_id"))
            idx = entry.get("mirror_index")
            if m is None or idx is None or idx >= len(m.attachments):
                return None, None
            return m.attachments[idx].url, m.jump_url

        lines = []
        previews = []
        bullet = "• " if len(atts) > 1 else ""
        for entry in atts:
            name = entry.get("filename", "unknown")
            # No mime type here — the description already says what kind of media it was.
            meta = [_fmt_size(entry.get("size") or 0)]
            if entry.get("duration"):
                meta.append(_fmt_duration(entry["duration"]))
            tail = " · ".join(meta)

            status = entry.get("status")
            if status == "mirrored":
                url, jump = fresh(entry)
                if jump:
                    # The filename is the link — a trailing "[archived]" would just be a
                    # second copy of the same url.
                    lines.append(f"{bullet}[`{name}`]({jump}) — {tail}")
                    ctype = (entry.get("content_type") or "").lower()
                    if url and ctype.startswith("image/") and not entry.get("is_spoiler"):
                        previews.append(url)
                else:
                    lines.append(f"{bullet}`{name}` — {tail} · ⚠️ mirror no longer available")
            elif status == "oversized":
                lines.append(f"{bullet}`{name}` — {tail} · ⚠️ too large to archive")
            elif status == "expired":
                lines.append(f"{bullet}`{name}` — {tail} · ⚠️ deleted before it was archived")
            elif status == "pending":
                lines.append(f"{bullet}`{name}` — {tail} · ⚠️ not archived yet")
            else:
                lines.append(f"{bullet}`{name}` — {tail} · ⚠️ archive failed")

        when = deleted_at or record.get("deleted_at")
        embed = discord.Embed(
            title="🗑️ Media Deleted",
            description=f"**{_summarise(atts)}**",
            color=COLOR_DELETE,
            timestamp=_aware(when) if isinstance(when, datetime.datetime)
            else discord.utils.utcnow(),
        )
        embed.add_field(
            name="Uploaded by",
            value=f"{record.get('author_tag', 'Unknown')}\n`{record.get('author_id')}`",
            inline=True,
        )
        embed.add_field(name="Channel", value=f"<#{record.get('channel_id')}>", inline=True)

        # Skipped entirely for a message that is still live, so /archivelookup doesn't claim
        # an unknown deleter for something nobody deleted.
        if when is not None or record.get("deleted"):
            if reason or record.get("delete_reason"):
                attribution = reason or record.get("delete_reason")
            elif deleted_by:
                attribution = deleted_by
            else:
                # A user deleting their own message leaves no audit entry at all — saying plain
                # "Unknown" here reliably gets misread as "something suspicious happened".
                attribution = "Unknown — likely the author"
            embed.add_field(name="Deleted by", value=attribution, inline=True)

        created = record.get("created_at")
        if isinstance(created, datetime.datetime):
            ts = _unix(created)
            embed.add_field(name="Posted", value=f"<t:{ts}:F>\n<t:{ts}:R>", inline=True)
        if isinstance(when, datetime.datetime):
            ts = _unix(when)
            embed.add_field(name="Deleted", value=f"<t:{ts}:F>\n<t:{ts}:R>", inline=True)

        content = record.get("content")
        embed.add_field(
            name="Message sent with it",
            value=(content[:1000] if content else "*(no text — file only)*"),
            inline=False,
        )

        # A 10-attachment message overflows a single 1024-char field.
        chunks, current = [], ""
        for line in lines:
            line = line[:1000]
            if len(current) + len(line) + 1 > 1024:
                chunks.append(current)
                current = ""
            current += line + "\n"
        if current:
            chunks.append(current)
        base = "File" if len(atts) == 1 else "Files"
        for i, chunk in enumerate(chunks):
            label = base if len(chunks) == 1 else f"{base} ({i + 1}/{len(chunks)})"
            embed.add_field(name=label, value=chunk, inline=False)

        # No separate "Archive" link — each file line already links to its own mirror, so a
        # second copy of the same jump url is pure noise. Only surface it when something broke.
        if mirror_missing:
            embed.add_field(name="Archive",
                            value="⚠️ part of the mirror could not be reached", inline=False)

        # An embed can only render one image, so show the first and let the rest be links —
        # better than showing nothing at all when several images go at once.
        if previews:
            embed.set_image(url=previews[0])
            if len(previews) > 1:
                embed.add_field(
                    name="​",
                    value=f"*Showing 1 of {len(previews)} images — the rest are linked above.*",
                    inline=False)
        embed.set_footer(text=f"message {record.get('_id')} • {self.bot.user.name}")
        return embed

    async def _send_log(self, guild, cfg, embeds):
        channel = guild.get_channel(cfg["archive_log_channel"]) if guild else None
        if channel is None:
            return

        async def flush(batch):
            try:
                await channel.send(embeds=batch,
                                   allowed_mentions=discord.AllowedMentions.none())
            except discord.Forbidden:
                print(f"[Archive] cannot post to log channel in {getattr(guild, 'id', '?')}")
            except discord.HTTPException as e:
                print(f"[Archive] log send failed: {e}")

        # Discord caps a message at 10 embeds *and* 6000 characters across all of them, so a
        # bulk delete has to be split across several messages rather than one big send.
        batch, size = [], 0
        for embed in embeds:
            length = len(embed)
            if batch and (len(batch) >= 10 or size + length > 5500):
                await flush(batch)
                batch, size = [], 0
            batch.append(embed)
            size += length
        if batch:
            await flush(batch)

    async def _warn(self, guild, cfg, key: str, text: str):
        """Throttled to once per hour per guild per issue, and mirrored to the owner log."""
        now = time.monotonic()
        k = (guild.id, key)
        if now - self._warned.get(k, 0.0) < WARN_INTERVAL:
            return
        self._warned[k] = now

        log_id = cfg.get("archive_log_channel") if cfg else None
        channel = guild.get_channel(log_id) if log_id else None
        if channel is not None:
            embed = discord.Embed(title="⚠️ Attachment Archive", description=text,
                                  color=COLOR_WARN, timestamp=discord.utils.utcnow())
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                pass
        try:
            await self.bot.send_log(
                title="Attachment Archive degraded",
                description=text,
                fields={"Guild": f"{guild.name} ({guild.id})", "Issue": key},
                color=COLOR_WARN,
            )
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        guild = channel.guild
        cfg = await self._get_config(guild.id)
        if cfg is None:
            return
        unset = {}
        if cfg.get("archive_channel") == channel.id:
            unset["archive_channel"] = ""
            self._archive_channel_ids.discard(channel.id)
        if cfg.get("archive_log_channel") == channel.id:
            unset["archive_log_channel"] = ""
        if not unset:
            return
        await self._db(self.servers.update_one, {"guild_id": guild.id}, {"$unset": unset})
        self._invalidate(guild.id)
        self._warned.pop((guild.id, "archive_missing"), None)
        await self._warn(guild, cfg, "channel_deleted",
                         f"`#{channel.name}` was deleted and it was part of the archive setup. "
                         f"Archiving is degraded — run `/archivesetup` to reconnect it.")

    @tasks.loop(minutes=5)
    async def prune_state(self):
        now = time.monotonic()
        self._delete_reasons = {k: v for k, v in self._delete_reasons.items()
                                if now - v[1] < 300}
        self._warned = {k: v for k, v in self._warned.items() if now - v < WARN_INTERVAL}
        self._cfg_cache = {k: v for k, v in self._cfg_cache.items()
                           if now - v[1] < CFG_TTL * 4}

    @prune_state.before_loop
    async def before_prune_state(self):
        await self.bot.wait_until_ready()

    # --------------------------------------------------------------- commands

    def _preflight(self, guild, archive_channel, log_channel) -> str:
        def mark(ok):
            return "✅" if ok else "❌"

        lines = []
        if archive_channel is None:
            lines.append("Archive channel — ❌ **not configured**")
        else:
            p = archive_channel.permissions_for(guild.me)
            lines.append(
                f"Archive {archive_channel.mention} — {mark(p.view_channel)} View "
                f"{mark(p.send_messages)} Send {mark(p.attach_files)} Attach "
                f"{mark(p.read_message_history)} History"
            )
        if log_channel is None:
            lines.append("Log channel — ❌ **not configured**")
        else:
            p = log_channel.permissions_for(guild.me)
            lines.append(
                f"Log {log_channel.mention} — {mark(p.view_channel)} View "
                f"{mark(p.send_messages)} Send {mark(p.embed_links)} Embed"
            )
        if archive_channel is not None and log_channel is not None \
                and archive_channel.id == log_channel.id:
            lines.append("Channels — ❌ archive and log are the **same channel**, so every file "
                         "shows twice. Re-run `/archivesetup` to split them.")
        if guild.me.guild_permissions.view_audit_log:
            lines.append("Audit log — ✅ deletions can be attributed")
        else:
            lines.append("Audit log — ⚠️ missing **View Audit Log**, deleters show as Unknown")
        lines.append(f"Upload limit — {_fmt_size(guild.filesize_limit)} (boost tier "
                     f"{guild.premium_tier})")
        return "\n".join(lines)

    @app_commands.command(name="archivesetup",
                          description="Set up logging of deleted images, videos and voice memos")
    @app_commands.describe(
        log_channel="Where deletion logs are posted",
        archive_channel="Private channel holding the mirrored files (created for you if omitted)",
        max_file_mb="Skip mirroring files larger than this (default: the server upload limit)",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def archivesetup(
        self,
        interaction: discord.Interaction,
        log_channel: discord.TextChannel,
        archive_channel: Optional[discord.TextChannel] = None,
        max_file_mb: Optional[app_commands.Range[int, 1, 100]] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        created = False

        # The mirror upload is a real message carrying the real file. If it lands in the log
        # channel you see the file once as the upload and again in the embed, so the two must
        # be different channels — the archive is storage, the log is the notification.
        collided = archive_channel is not None and archive_channel.id == log_channel.id
        if collided:
            archive_channel = None

        if archive_channel is None:
            existing = await self._db(self.servers.find_one, {"guild_id": guild.id})
            prior = (existing or {}).get("archive_channel")
            if prior and prior != log_channel.id:
                archive_channel = guild.get_channel(prior)
        if archive_channel is None:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, attach_files=True,
                    read_message_history=True, embed_links=True,
                ),
            }
            try:
                archive_channel = await guild.create_text_channel(
                    "file-archive",
                    overwrites=overwrites,
                    topic="Automatic attachment mirror — do not delete messages here.",
                    reason=f"Attachment archive setup by {interaction.user}",
                )
                created = True
            except discord.Forbidden:
                await interaction.followup.send(
                    "❌ I need **Manage Channels** to create the archive channel, which has to "
                    "be separate from the log channel — otherwise every file shows up twice, "
                    "once as the stored copy and once in the embed.\n\nEither grant me Manage "
                    "Channels and re-run this, or make a private channel yourself and pass it "
                    "as `archive_channel:`. Nobody needs to read it; it just holds the files "
                    "so the links in the log keep working.", ephemeral=True)
                return

        updates = {
            "archive_enabled": True,
            "archive_channel": archive_channel.id,
            "archive_log_channel": log_channel.id,
        }
        if max_file_mb is not None:
            updates["archive_max_bytes"] = max_file_mb * 1024 * 1024
        await self._set_config(guild.id, updates)
        self._archive_channel_ids.add(archive_channel.id)

        embed = discord.Embed(
            title="🗄️ Media archiving enabled",
            description="Tracking **images, videos and voice memos**. Other file types "
                        "(documents, archives, etc.) are ignored entirely.\n\n"
                        + self._preflight(guild, archive_channel, log_channel),
            color=COLOR_INFO,
        )
        if collided:
            embed.add_field(
                name="Archive moved out of your log channel",
                value=f"The archive can't be the same channel as the log — the stored copy is a "
                      f"real upload, so you'd see every file twice. Files now go to "
                      f"{archive_channel.mention} and {log_channel.mention} shows only the "
                      f"embed.",
                inline=False)
        if created:
            embed.add_field(
                name="Archive channel created",
                value=f"I created {archive_channel.mention} — private, hidden from @everyone, so "
                      f"it won't clutter anything. It only exists to hold the files so the links "
                      f"in your log keep working. You never need to open it.",
                inline=False)
        embed.add_field(
            name="Please note",
            value="Deleted files are kept **forever**. Members should be told their deleted "
                  "uploads are retained. Use `/archivepurge` to remove specific files or "
                  "everything from one user.",
            inline=False)
        embed.set_footer(text="Coverage starts now — files posted before this are not archived.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="archivestatus",
                          description="Show the media archive configuration and health")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def archivestatus(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        doc = await self._db(self.servers.find_one, {"guild_id": guild.id})
        doc = doc or {}

        archive_channel = guild.get_channel(doc.get("archive_channel")) \
            if doc.get("archive_channel") else None
        log_channel = guild.get_channel(doc.get("archive_log_channel")) \
            if doc.get("archive_log_channel") else None
        enabled = bool(doc.get("archive_enabled"))

        embed = discord.Embed(
            title="🗄️ Attachment Archive",
            description="**Status:** " + ("🟢 enabled" if enabled else "🔴 disabled"),
            color=COLOR_INFO if enabled else COLOR_WARN,
        )
        embed.add_field(name="Checks", value=self._preflight(guild, archive_channel, log_channel),
                        inline=False)

        cap = doc.get("archive_max_bytes")
        ignored = doc.get("archive_ignored_channels") or []
        embed.add_field(
            name="Settings",
            value=(f"Tracking: images, videos, voice memos\n"
                   f"Max mirrored file: {_fmt_size(cap) if cap else 'server limit'}\n"
                   f"Mirror auto-moderated spam: "
                   f"{'yes' if doc.get('archive_mirror_automod') else 'no (metadata only)'}\n"
                   f"Retention: forever\n"
                   f"Ignored: " + (", ".join(f"<#{c}>" for c in ignored[:10]) if ignored else "none")),
            inline=False)

        try:
            total = await self._db(self.coll.count_documents, {"guild_id": guild.id})
            oldest = await self._db(
                lambda: self.coll.find_one({"guild_id": guild.id}, sort=[("created_at", 1)]))
        except Exception:
            total, oldest = "?", None
        since = "—"
        if oldest and isinstance(oldest.get("created_at"), datetime.datetime):
            since = f"<t:{_unix(oldest['created_at'])}:D>"
        embed.add_field(name="Coverage",
                        value=f"{total} message(s) indexed • archiving since {since}", inline=False)

        s = self.stats
        embed.add_field(
            name="This session",
            value=(f"queued {s['queued']} • mirrored {s['mirrored']} • oversized {s['oversized']} "
                   f"• failed {s['failed']} • **dropped {s['dropped']}**\n"
                   f"queue depth: {self.queue.qsize()}/{QUEUE_MAXSIZE}"),
            inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="archivedisable",
                          description="Stop archiving and logging deleted media")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def archivedisable(self, interaction: discord.Interaction):
        await self._set_config(interaction.guild.id, {"archive_enabled": False})
        await interaction.response.send_message(
            "🔴 Archiving is off. **Nothing was deleted** — existing mirrors and records are "
            "untouched, and `/archivesetup` resumes where you left off.", ephemeral=True)

    @app_commands.command(name="archiveignore",
                          description="Stop archiving media posted in a channel")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def archiveignore(
        self, interaction: discord.Interaction,
        channel: Union[discord.TextChannel, discord.ForumChannel, discord.VoiceChannel],
    ):
        await self._db(self.servers.update_one, {"guild_id": interaction.guild.id},
                       {"$addToSet": {"archive_ignored_channels": channel.id}}, upsert=True)
        self._invalidate(interaction.guild.id)
        await interaction.response.send_message(
            f"✅ Files posted in {channel.mention} (and its threads) will no longer be archived.",
            ephemeral=True)

    @app_commands.command(name="archiveunignore",
                          description="Resume archiving media posted in a channel")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def archiveunignore(
        self, interaction: discord.Interaction,
        channel: Union[discord.TextChannel, discord.ForumChannel, discord.VoiceChannel],
    ):
        await self._db(self.servers.update_one, {"guild_id": interaction.guild.id},
                       {"$pull": {"archive_ignored_channels": channel.id}})
        self._invalidate(interaction.guild.id)
        await interaction.response.send_message(
            f"✅ Archiving resumed for {channel.mention}.", ephemeral=True)

    @app_commands.command(name="archivelookup",
                          description="Look up the archived media of a message by its ID")
    @app_commands.describe(message_id="Right-click the message → Copy Message ID")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def archivelookup(self, interaction: discord.Interaction, message_id: str):
        await interaction.response.defer(ephemeral=True)
        try:
            mid = int(message_id.strip())
        except ValueError:
            await interaction.followup.send("❌ That isn't a valid message ID.", ephemeral=True)
            return

        record = await self._db(self.coll.find_one,
                                {"_id": mid, "guild_id": interaction.guild.id})
        if record is None:
            await interaction.followup.send(
                "❌ No archived record for that message ID in this server.", ephemeral=True)
            return

        embed = await self._build_delete_embed(
            interaction.guild, record, record.get("delete_reason"), record.get("deleted_by"))
        embed.title = "🗄️ Archived Message"
        embed.color = COLOR_INFO
        state = "deleted" if record.get("deleted") else "still live"
        embed.description = f"{embed.description}\nStatus: **{state}**"
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="archivepurge",
        description="Permanently delete archived media for one message or one user")
    @app_commands.describe(
        confirm="Must be True — this permanently deletes mirrored files",
        message_id="Purge a single message's archive",
        user="Purge every archived file from this user",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def archivepurge(
        self, interaction: discord.Interaction, confirm: bool,
        message_id: Optional[str] = None, user: Optional[discord.User] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        if not confirm:
            await interaction.followup.send(
                "Nothing purged — re-run with `confirm: True`.", ephemeral=True)
            return
        if (message_id is None) == (user is None):
            await interaction.followup.send(
                "❌ Give exactly one of `message_id` or `user`.", ephemeral=True)
            return

        query = {"guild_id": interaction.guild.id}
        if message_id is not None:
            try:
                query["_id"] = int(message_id.strip())
            except ValueError:
                await interaction.followup.send("❌ That isn't a valid message ID.", ephemeral=True)
                return
        else:
            query["author_id"] = user.id

        records = await self._db(lambda: list(self.coll.find(query)))
        if not records:
            await interaction.followup.send("Nothing to purge.", ephemeral=True)
            return

        removed = 0
        for record in records:
            channel = interaction.guild.get_channel(record.get("archive_channel_id") or 0)
            if channel is None:
                continue
            for mid in record.get("archive_message_ids") or []:
                try:
                    # Partial message: a plain delete, so it works at any message age
                    # (bulk delete refuses anything older than 14 days).
                    await channel.get_partial_message(mid).delete()
                    removed += 1
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    continue

        result = await self._db(self.coll.delete_many, query)
        await interaction.followup.send(
            f"🧹 Purged **{result.deleted_count}** record(s) and deleted **{removed}** mirror "
            f"message(s). This cannot be undone.", ephemeral=True)

    @app_commands.command(
        name="archivebackfill",
        description="Archive media already posted in a channel (slow)")
    @app_commands.describe(
        channel="Channel to scan",
        limit="How many past messages to scan (newest first)",
        confirm="Must be True — this can take hours on a busy channel",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def archivebackfill(
        self, interaction: discord.Interaction, channel: discord.TextChannel,
        confirm: bool, limit: Optional[app_commands.Range[int, 1, 20000]] = 1000,
    ):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if not confirm:
            await interaction.followup.send(
                "Nothing scanned — re-run with `confirm: True`. Every media message found costs "
                "a download plus an upload, so a channel with a few thousand files can take "
                "**hours**. Old attachments may also already be unreachable.", ephemeral=True)
            return
        cfg = await self._get_config(guild.id)
        if cfg is None or not cfg.get("archive_channel"):
            await interaction.followup.send("❌ Run `/archivesetup` first.", ephemeral=True)
            return
        if guild.id in self._backfilling:
            await interaction.followup.send(
                "❌ A backfill is already running in this server.", ephemeral=True)
            return

        self._backfilling.add(guild.id)
        scanned = queued = 0
        try:
            async for msg in channel.history(limit=limit, oldest_first=False):
                scanned += 1
                if msg.author.id == self.bot.user.id or not _media_attachments(msg):
                    continue
                # Same queue as live capture — never a parallel fast path.
                await self.queue.put(msg)
                queued += 1
                if queued % 100 == 0:
                    try:
                        await interaction.edit_original_response(
                            content=f"Scanning {channel.mention}… {scanned} messages, "
                                    f"{queued} queued.")
                    except discord.HTTPException:
                        pass
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ I can't read history in {channel.mention}.", ephemeral=True)
            return
        finally:
            self._backfilling.discard(guild.id)

        await interaction.followup.send(
            f"✅ Scanned {scanned} message(s) in {channel.mention} and queued {queued} for "
            f"archiving. Already-archived messages are skipped automatically. Watch "
            f"`/archivestatus` for progress.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AttachmentArchive(bot))
    print("AttachmentArchive cog loaded ✓")
