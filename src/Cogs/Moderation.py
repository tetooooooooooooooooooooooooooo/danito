"""Moderation commands with a persistent case history.

Every action that touches a member creates a numbered case in Mongo, so moderators can see
whether someone is a repeat offender rather than reacting to each incident in isolation.
Cases are also posted to a mod-log channel, set with /logging moderation.

Two guards run before any action, because skipping either produces confusing Discord API
errors instead of useful messages:

- Role hierarchy — a moderator can't action someone at or above their own top role, and the
  bot can't action anyone above its own. Server owners are never actionable.
- Bot permissions — checked up front so the failure is "I'm missing Ban Members" rather than
  a 403 halfway through.
"""

import asyncio
import datetime
import re
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import Database
import GuildConfig

COLOR_BAN = 0xE74C3C
COLOR_KICK = 0xE67E22
COLOR_TIMEOUT = 0xF1C40F
COLOR_WARN = 0xF39C12
COLOR_GOOD = 0x2ECC71
COLOR_INFO = 0x5865F2

MAX_TIMEOUT_DAYS = 28          # Discord's hard ceiling
MAX_PURGE = 200

DURATION_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

ACTION_STYLE = {
    "ban": ("🔨", "Ban", COLOR_BAN),
    "unban": ("♻️", "Unban", COLOR_GOOD),
    "kick": ("👢", "Kick", COLOR_KICK),
    "timeout": ("⏳", "Timeout", COLOR_TIMEOUT),
    "untimeout": ("✅", "Timeout removed", COLOR_GOOD),
    "warn": ("⚠️", "Warning", COLOR_WARN),
    "purge": ("🧹", "Purge", COLOR_INFO),
}


def parse_duration(text: str) -> Optional[int]:
    """'1h30m' / '2d' / '45s' -> seconds. None if nothing parseable."""
    if not text:
        return None
    total = 0
    found = False
    for amount, unit in DURATION_RE.findall(text):
        total += int(amount) * UNIT_SECONDS[unit.lower()]
        found = True
    return total if found and total > 0 else None


def fmt_duration(seconds: int) -> str:
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs and not parts:
        parts.append(f"{secs}s")
    return " ".join(parts) or "0s"


class HierarchyError(Exception):
    """Raised when the actor or the bot isn't allowed to touch the target."""


class Moderation(commands.Cog):
    """Ban, kick, timeout, purge, warn — with a case history."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── storage ──────────────────────────────────────────────────────
    @property
    def _db_(self):
        return Database.get_bot_database(self.bot.MongoClient)

    @property
    def cases(self):
        return self._db_["mod_cases"]

    @property
    def servers(self):
        return self._db_["servers"]

    async def _run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(lambda: fn(*args, **kwargs))

    async def cog_load(self):
        try:
            await self._run(self._ensure_indexes)
        except Exception as e:
            print(f"[Moderation] index setup failed: {e}")

    def _ensure_indexes(self):
        self.cases.create_index([("guild_id", 1), ("case_id", -1)], name="guild_case")
        self.cases.create_index([("guild_id", 1), ("user_id", 1), ("created_at", -1)],
                                name="guild_user")
        self.cases.create_index([("guild_id", 1), ("action", 1), ("user_id", 1)],
                                name="guild_action_user")

    def _next_case_id(self, guild_id: int) -> int:
        """Sequential per guild. find_one_and_update is atomic, so two simultaneous actions
        can't be handed the same number."""
        doc = self._db_["counters"].find_one_and_update(
            {"_id": f"case:{guild_id}"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=True,
        )
        return int(doc["seq"]) if doc else 1

    async def _record(self, guild_id, action, user_id, user_tag, mod_id, mod_tag,
                      reason, duration=None) -> Optional[int]:
        def write():
            case_id = self._next_case_id(guild_id)
            self.cases.insert_one({
                "guild_id": guild_id,
                "case_id": case_id,
                "action": action,
                "user_id": user_id,
                "user_tag": user_tag,
                "mod_id": mod_id,
                "mod_tag": mod_tag,
                "reason": reason,
                "duration": duration,
                "created_at": datetime.datetime.now(datetime.timezone.utc),
                "active": True,
            })
            return case_id
        try:
            return await self._run(write)
        except Exception as e:
            print(f"[Moderation] failed to record case: {e}")
            return None

    # ── mod-log channel ──────────────────────────────────────────────
    async def _log_channel_id(self, guild_id: int) -> Optional[int]:
        doc = await GuildConfig.get(self.bot, guild_id)
        return doc.get("modlog_channel")

    async def _post_case(self, guild: discord.Guild, case_id, action, target, moderator,
                         reason, duration=None, extra=None):
        cid = await self._log_channel_id(guild.id)
        if not cid:
            return
        channel = guild.get_channel(cid)
        if channel is None:
            return

        emoji, label, color = ACTION_STYLE.get(action, ("📌", action.title(), COLOR_INFO))
        embed = discord.Embed(
            title=f"{emoji} {label}" + (f" · Case #{case_id}" if case_id else ""),
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        if target is not None:
            embed.add_field(name="User", value=f"{target}\n`{target.id}`", inline=True)
            embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Moderator", value=f"{moderator}\n`{moderator.id}`", inline=True)
        if duration:
            embed.add_field(name="Duration", value=fmt_duration(duration), inline=True)
        embed.add_field(name="Reason", value=(reason or "*No reason given*")[:1024], inline=False)
        if extra:
            embed.add_field(name="Details", value=extra[:1024], inline=False)

        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            print(f"[Moderation] mod-log send failed: {e}")

    # ── guards ───────────────────────────────────────────────────────
    def _check_hierarchy(self, interaction: discord.Interaction, target: discord.Member):
        guild = interaction.guild
        actor = interaction.user

        if target.id == actor.id:
            raise HierarchyError("You can't use this on yourself.")
        if target.id == self.bot.user.id:
            raise HierarchyError("I'm not doing that to myself.")
        if target.id == guild.owner_id:
            raise HierarchyError("You can't action the server owner.")
        # The owner outranks everyone, so exempt them from the role comparison.
        if actor.id != guild.owner_id and target.top_role >= actor.top_role:
            raise HierarchyError(
                f"**{target}** has a role at or above yours, so you can't action them.")
        if target.top_role >= guild.me.top_role:
            raise HierarchyError(
                f"**{target}**'s highest role is above mine. Move my role higher in "
                f"Server Settings → Roles.")

    def _need(self, guild: discord.Guild, **perms):
        mine = guild.me.guild_permissions
        missing = [n for n in perms if not getattr(mine, n, False)]
        if missing:
            pretty = ", ".join(n.replace("_", " ").title() for n in missing)
            raise HierarchyError(f"I'm missing **{pretty}**. Grant it and try again.")

    async def _notify(self, member: discord.Member, guild_name: str, action: str,
                      reason: str, duration=None) -> bool:
        """Best effort — plenty of people have DMs closed."""
        try:
            embed = discord.Embed(
                title=f"You were {action} in {guild_name}",
                description=(reason or "*No reason given*")[:2000],
                color=ACTION_STYLE.get(action, ("", "", COLOR_INFO))[2],
                timestamp=discord.utils.utcnow(),
            )
            if duration:
                embed.add_field(name="Duration", value=fmt_duration(duration))
            await member.send(embed=embed)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def _fail(self, interaction: discord.Interaction, message: str):
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ {message}", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ {message}", ephemeral=True)

    # ── /ban ─────────────────────────────────────────────────────────
    @app_commands.command(name="ban", description="Ban a member from the server")
    @app_commands.describe(
        member="Who to ban",
        reason="Why (shown in the audit log and the mod log)",
        delete_days="Delete their messages from the last N days (0-7, default 0)",
    )
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.guild_only()
    async def ban(self, interaction: discord.Interaction, member: discord.Member,
                  reason: str = None, delete_days: app_commands.Range[int, 0, 7] = 0):
        try:
            self._need(interaction.guild, ban_members=True)
            self._check_hierarchy(interaction, member)
        except HierarchyError as e:
            return await self._fail(interaction, str(e))

        await interaction.response.defer()
        dmed = await self._notify(member, interaction.guild.name, "banned", reason)
        try:
            await member.ban(reason=f"{interaction.user}: {reason or 'no reason'}",
                             delete_message_seconds=delete_days * 86400)
        except discord.HTTPException as e:
            return await self._fail(interaction, f"Ban failed: {e}")

        case_id = await self._record(interaction.guild.id, "ban", member.id, str(member),
                                     interaction.user.id, str(interaction.user), reason)
        await self._post_case(interaction.guild, case_id, "ban", member, interaction.user, reason,
                              extra=None if dmed else "Couldn't DM them (DMs closed).")
        await interaction.followup.send(
            f"🔨 Banned **{member}**" + (f" · Case #{case_id}" if case_id else ""))

    # ── /unban ───────────────────────────────────────────────────────
    @app_commands.command(name="unban", description="Unban a user by their ID")
    @app_commands.describe(user_id="The banned user's ID", reason="Why")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.guild_only()
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str = None):
        try:
            self._need(interaction.guild, ban_members=True)
        except HierarchyError as e:
            return await self._fail(interaction, str(e))
        try:
            uid = int(user_id.strip())
        except ValueError:
            return await self._fail(interaction, "That isn't a valid user ID.")

        await interaction.response.defer()
        try:
            user = await self.bot.fetch_user(uid)
            await interaction.guild.unban(user, reason=f"{interaction.user}: {reason or '-'}")
        except discord.NotFound:
            return await self._fail(interaction, "That user isn't banned here.")
        except discord.HTTPException as e:
            return await self._fail(interaction, f"Unban failed: {e}")

        case_id = await self._record(interaction.guild.id, "unban", uid, str(user),
                                     interaction.user.id, str(interaction.user), reason)
        await self._post_case(interaction.guild, case_id, "unban", user, interaction.user, reason)
        await interaction.followup.send(
            f"♻️ Unbanned **{user}**" + (f" · Case #{case_id}" if case_id else ""))

    # ── /kick ────────────────────────────────────────────────────────
    @app_commands.command(name="kick", description="Kick a member from the server")
    @app_commands.describe(member="Who to kick", reason="Why")
    @app_commands.default_permissions(kick_members=True)
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.guild_only()
    async def kick(self, interaction: discord.Interaction, member: discord.Member,
                   reason: str = None):
        try:
            self._need(interaction.guild, kick_members=True)
            self._check_hierarchy(interaction, member)
        except HierarchyError as e:
            return await self._fail(interaction, str(e))

        await interaction.response.defer()
        dmed = await self._notify(member, interaction.guild.name, "kicked", reason)
        try:
            await member.kick(reason=f"{interaction.user}: {reason or 'no reason'}")
        except discord.HTTPException as e:
            return await self._fail(interaction, f"Kick failed: {e}")

        case_id = await self._record(interaction.guild.id, "kick", member.id, str(member),
                                     interaction.user.id, str(interaction.user), reason)
        await self._post_case(interaction.guild, case_id, "kick", member, interaction.user, reason,
                              extra=None if dmed else "Couldn't DM them (DMs closed).")
        await interaction.followup.send(
            f"👢 Kicked **{member}**" + (f" · Case #{case_id}" if case_id else ""))

    # ── /timeout ─────────────────────────────────────────────────────
    @app_commands.command(name="timeout", description="Temporarily mute a member")
    @app_commands.describe(
        member="Who to time out",
        duration="How long, e.g. 10m, 2h, 1d, 1h30m (max 28d)",
        reason="Why",
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def timeout(self, interaction: discord.Interaction, member: discord.Member,
                      duration: str, reason: str = None):
        seconds = parse_duration(duration)
        if seconds is None:
            return await self._fail(
                interaction,
                "I couldn't read that duration. Use something like `10m`, `2h`, `1d` or `1h30m`.")
        if seconds > MAX_TIMEOUT_DAYS * 86400:
            return await self._fail(
                interaction, f"Discord caps timeouts at {MAX_TIMEOUT_DAYS} days.")
        try:
            self._need(interaction.guild, moderate_members=True)
            self._check_hierarchy(interaction, member)
        except HierarchyError as e:
            return await self._fail(interaction, str(e))

        await interaction.response.defer()
        until = discord.utils.utcnow() + datetime.timedelta(seconds=seconds)
        dmed = await self._notify(member, interaction.guild.name, "timed out", reason, seconds)
        try:
            await member.timeout(until, reason=f"{interaction.user}: {reason or 'no reason'}")
        except discord.HTTPException as e:
            return await self._fail(interaction, f"Timeout failed: {e}")

        case_id = await self._record(interaction.guild.id, "timeout", member.id, str(member),
                                     interaction.user.id, str(interaction.user), reason, seconds)
        await self._post_case(interaction.guild, case_id, "timeout", member, interaction.user,
                              reason, seconds,
                              extra=None if dmed else "Couldn't DM them (DMs closed).")
        await interaction.followup.send(
            f"⏳ Timed out **{member}** for {fmt_duration(seconds)} "
            f"(until <t:{int(until.timestamp())}:f>)"
            + (f" · Case #{case_id}" if case_id else ""))

    # ── /untimeout ───────────────────────────────────────────────────
    @app_commands.command(name="untimeout", description="Remove a member's timeout")
    @app_commands.describe(member="Who to un-mute", reason="Why")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member,
                        reason: str = None):
        try:
            self._need(interaction.guild, moderate_members=True)
            self._check_hierarchy(interaction, member)
        except HierarchyError as e:
            return await self._fail(interaction, str(e))
        if not member.is_timed_out():
            return await self._fail(interaction, f"**{member}** isn't timed out.")

        await interaction.response.defer()
        try:
            await member.timeout(None, reason=f"{interaction.user}: {reason or '-'}")
        except discord.HTTPException as e:
            return await self._fail(interaction, f"Failed: {e}")

        case_id = await self._record(interaction.guild.id, "untimeout", member.id, str(member),
                                     interaction.user.id, str(interaction.user), reason)
        await self._post_case(interaction.guild, case_id, "untimeout", member,
                              interaction.user, reason)
        await interaction.followup.send(f"✅ Removed the timeout on **{member}**.")

    # ── /purge ───────────────────────────────────────────────────────
    @app_commands.command(name="purge", description="Bulk delete recent messages")
    @app_commands.describe(
        amount=f"How many messages to scan (1-{MAX_PURGE})",
        member="Only delete messages from this member",
        contains="Only delete messages containing this text",
    )
    @app_commands.checks.cooldown(1, 5.0)
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def purge(self, interaction: discord.Interaction,
                    amount: app_commands.Range[int, 1, MAX_PURGE],
                    member: discord.Member = None, contains: str = None):
        try:
            self._need(interaction.guild, manage_messages=True)
        except HierarchyError as e:
            return await self._fail(interaction, str(e))

        await interaction.response.defer(ephemeral=True)

        def predicate(m: discord.Message) -> bool:
            if member is not None and m.author.id != member.id:
                return False
            if contains and contains.lower() not in (m.content or "").lower():
                return False
            return True

        try:
            deleted = await interaction.channel.purge(limit=amount, check=predicate)
        except discord.Forbidden:
            return await self._fail(interaction, "I can't delete messages in this channel.")
        except discord.HTTPException as e:
            # Discord's bulk endpoint refuses anything older than 14 days.
            return await self._fail(
                interaction,
                f"Purge failed: {e}. Discord won't bulk-delete messages older than 14 days.")

        filters = []
        if member:
            filters.append(f"from {member}")
        if contains:
            filters.append(f"containing “{contains}”")
        detail = (" " + " and ".join(filters)) if filters else ""

        case_id = await self._record(
            interaction.guild.id, "purge", member.id if member else 0,
            str(member) if member else "n/a", interaction.user.id, str(interaction.user),
            f"{len(deleted)} message(s) in #{interaction.channel.name}{detail}")
        await self._post_case(
            interaction.guild, case_id, "purge", member, interaction.user,
            f"Deleted **{len(deleted)}** message(s) in {interaction.channel.mention}{detail}")
        await interaction.followup.send(
            f"🧹 Deleted **{len(deleted)}** message(s){detail}.", ephemeral=True)

    # ── /slowmode ────────────────────────────────────────────────────
    @app_commands.command(name="slowmode", description="Set this channel's slowmode")
    @app_commands.describe(duration="e.g. 10s, 2m, 1h, or 0 to turn it off (max 6h)")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def slowmode(self, interaction: discord.Interaction, duration: str):
        seconds = 0 if duration.strip() in ("0", "off", "none") else parse_duration(duration)
        if seconds is None:
            return await self._fail(
                interaction, "Couldn't read that. Try `10s`, `2m`, `1h`, or `0` to disable.")
        if seconds > 21600:
            return await self._fail(interaction, "Discord caps slowmode at 6 hours.")
        try:
            await interaction.channel.edit(
                slowmode_delay=seconds, reason=f"{interaction.user} via /slowmode")
        except discord.Forbidden:
            return await self._fail(interaction, "I can't edit this channel.")
        except discord.HTTPException as e:
            return await self._fail(interaction, f"Failed: {e}")

        await interaction.response.send_message(
            f"🐌 Slowmode set to **{fmt_duration(seconds)}**." if seconds
            else "🐌 Slowmode is off.")

    # ── /lock and /unlock ────────────────────────────────────────────
    async def _set_lock(self, interaction: discord.Interaction, locked: bool, reason):
        try:
            self._need(interaction.guild, manage_channels=True)
        except HierarchyError as e:
            return await self._fail(interaction, str(e))

        channel = interaction.channel
        everyone = interaction.guild.default_role
        overwrite = channel.overwrites_for(everyone)
        if overwrite.send_messages is (False if locked else None) and locked:
            return await self._fail(interaction, "This channel is already locked.")

        overwrite.send_messages = False if locked else None
        try:
            await channel.set_permissions(
                everyone, overwrite=overwrite,
                reason=f"{interaction.user}: {reason or ('lock' if locked else 'unlock')}")
        except discord.Forbidden:
            return await self._fail(interaction, "I can't edit permissions in this channel.")
        except discord.HTTPException as e:
            return await self._fail(interaction, f"Failed: {e}")

        embed = discord.Embed(
            title="🔒 Channel locked" if locked else "🔓 Channel unlocked",
            description=(reason or None),
            color=COLOR_BAN if locked else COLOR_GOOD,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"by {interaction.user}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="lock", description="Stop members sending messages here")
    @app_commands.describe(reason="Shown in the channel")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def lock(self, interaction: discord.Interaction, reason: str = None):
        await self._set_lock(interaction, True, reason)

    @app_commands.command(name="unlock", description="Let members send messages here again")
    @app_commands.describe(reason="Shown in the channel")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def unlock(self, interaction: discord.Interaction, reason: str = None):
        await self._set_lock(interaction, False, reason)

    # ── /warn ────────────────────────────────────────────────────────
    @app_commands.command(name="warn", description="Warn a member (recorded in their history)")
    @app_commands.describe(member="Who to warn", reason="What they did")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        try:
            self._check_hierarchy(interaction, member)
        except HierarchyError as e:
            return await self._fail(interaction, str(e))

        await interaction.response.defer()
        case_id = await self._record(interaction.guild.id, "warn", member.id, str(member),
                                     interaction.user.id, str(interaction.user), reason)
        if case_id is None:
            return await self._fail(
                interaction, "Couldn't save the warning, the database didn't respond.")

        try:
            count = await self._run(self.cases.count_documents, {
                "guild_id": interaction.guild.id, "user_id": member.id,
                "action": "warn", "active": True})
        except Exception:
            count = 0

        dmed = await self._notify(member, interaction.guild.name, "warned", reason)
        await self._post_case(interaction.guild, case_id, "warn", member, interaction.user, reason,
                              extra=f"That's warning **#{count}** for this member."
                              + ("" if dmed else "\nCouldn't DM them (DMs closed)."))
        await interaction.followup.send(
            f"⚠️ Warned **{member}**. That's warning **#{count}** · Case #{case_id}")

    # ── /warnings ────────────────────────────────────────────────────
    @app_commands.command(name="warnings", description="Show a member's warnings")
    @app_commands.describe(member="Whose warnings to show")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        try:
            rows = await self._run(lambda: list(self.cases.find({
                "guild_id": interaction.guild.id, "user_id": member.id,
                "action": "warn", "active": True}).sort("case_id", -1).limit(25)))
        except Exception as e:
            return await self._fail(interaction, f"Couldn't read the database: {e}")

        embed = discord.Embed(
            title=f"⚠️ Warnings for {member}",
            description=f"**{len(rows)}** active warning(s)" if rows
            else "No active warnings. 🎉",
            color=COLOR_WARN if rows else COLOR_GOOD,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        for row in rows[:15]:
            ts = row.get("created_at")
            when = f"<t:{int(ts.replace(tzinfo=datetime.timezone.utc).timestamp())}:R>" \
                if isinstance(ts, datetime.datetime) else "?"
            embed.add_field(
                name=f"Case #{row['case_id']} · {when}",
                value=f"{(row.get('reason') or '*No reason*')[:200]}\n"
                      f"-# by {row.get('mod_tag', 'unknown')}",
                inline=False)
        if len(rows) > 15:
            embed.set_footer(text=f"Showing 15 of {len(rows)}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /delwarn ─────────────────────────────────────────────────────
    @app_commands.command(name="delwarn", description="Remove a warning by its case number")
    @app_commands.describe(case_id="The case number from /warnings")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def delwarn(self, interaction: discord.Interaction, case_id: int):
        await interaction.response.defer(ephemeral=True)
        try:
            # Marked inactive rather than deleted, so the audit trail survives.
            result = await self._run(self.cases.update_one, {
                "guild_id": interaction.guild.id, "case_id": case_id, "action": "warn"},
                {"$set": {"active": False, "removed_by": str(interaction.user),
                          "removed_at": datetime.datetime.now(datetime.timezone.utc)}})
        except Exception as e:
            return await self._fail(interaction, f"Database error: {e}")

        if result.matched_count == 0:
            return await self._fail(interaction, f"No warning found with case #{case_id}.")
        await interaction.followup.send(
            f"✅ Warning **#{case_id}** removed. The case is kept in `/modlogs` for the record.",
            ephemeral=True)

    # ── /modlogs ─────────────────────────────────────────────────────
    @app_commands.command(name="modlogs", description="Show a member's full moderation history")
    @app_commands.describe(member="Whose history to show")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def modlogs(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        try:
            rows = await self._run(lambda: list(self.cases.find({
                "guild_id": interaction.guild.id,
                "user_id": member.id}).sort("case_id", -1).limit(25)))
        except Exception as e:
            return await self._fail(interaction, f"Couldn't read the database: {e}")

        counts: dict[str, int] = {}
        for r in rows:
            counts[r["action"]] = counts.get(r["action"], 0) + 1
        summary = " · ".join(f"{v} {k}" for k, v in counts.items()) or "clean record"

        embed = discord.Embed(
            title=f"📋 Mod history for {member}",
            description=f"**{len(rows)}** case(s) · {summary}",
            color=COLOR_INFO,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        for row in rows[:15]:
            emoji, label, _ = ACTION_STYLE.get(row["action"], ("📌", row["action"].title(), 0))
            ts = row.get("created_at")
            when = f"<t:{int(ts.replace(tzinfo=datetime.timezone.utc).timestamp())}:R>" \
                if isinstance(ts, datetime.datetime) else "?"
            struck = "" if row.get("active", True) else " *(removed)*"
            embed.add_field(
                name=f"{emoji} Case #{row['case_id']} · {label}{struck} · {when}",
                value=f"{(row.get('reason') or '*No reason*')[:200]}\n"
                      f"-# by {row.get('mod_tag', 'unknown')}",
                inline=False)
        if len(rows) > 15:
            embed.set_footer(text=f"Showing 15 of {len(rows)}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # Choosing the channel lives in the Logging cog, under /logging moderation, alongside the
    # other three logs. The cases themselves are still this cog's job.


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
    print("Moderation cog loaded ✓")
