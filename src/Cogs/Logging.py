"""The server log: twelve kinds of event, each of which can go wherever you want.

The routing is the whole design. Most servers want one #server-log channel and nothing more,
and a bot that forces twelve separate pickers on those people is worse than one that offers a
single box. So there is one destination for everything, and any individual event can override
it. An event with no channel of its own falls back to the shared one, which means the common
case is a single setting and the flexible case costs nothing to ignore.

Two things stop this logging itself into a loop. The bot's own messages are never logged, so
deleting a log entry does not create another one, and any channel that is a log destination is
skipped outright, which also keeps a busy log channel from filling up with notes about itself.

Deleted media stays with the MediaLog cog, which holds the actual file bytes. When both are
switched on, a deleted message carrying an image is left to MediaLog so it is reported once,
with the picture, rather than twice.
"""

import asyncio
import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import Database
import GuildConfig

REMINDER_DAYS = 30          # how far back /logging status looks for survey reminders

# (key, icon, label). The keys are stored in the database and are mirrored in the dashboard's
# store.LOG_EVENTS; a test asserts the two lists agree, because a typo here would silently
# switch an event off for everybody rather than fail.
EVENTS = [
    ("message_delete",   "🗑️", "Deleted messages"),
    ("message_edit",     "✏️", "Edited messages"),
    ("message_purge",    "🧹", "Bulk deletions"),
    ("member_join",      "📥", "People joining"),
    ("member_leave",     "📤", "People leaving"),
    ("member_ban",       "🔨", "Bans"),
    ("member_unban",     "🕊️", "Unbans"),
    ("member_nickname",  "🏷️", "Nickname changes"),
    ("member_roles",     "🎭", "Role changes"),
    ("voice_activity",   "🔊", "Voice channels"),
    ("channel_changes",  "📁", "Channels added, removed or renamed"),
    ("role_changes",     "🎟️", "Roles added, removed or renamed"),
    ("server_changes",   "⚙️", "Server settings"),
]
EVENT_KEYS = [key for key, _, _ in EVENTS]

RED = 0xED4245
AMBER = 0xE67E22
GREEN = 0x2ECC71
GREY = 0x99AAB5
BLURPLE = 0x5865F2

MAX_FIELD = 1000            # an embed field caps at 1024; leave room for the code fence
AUDIT_WINDOW = 15           # seconds an audit entry can lag the event and still be the cause

DEFAULT_CATEGORY = "Server Logs"

# How /logging setup lays the channels out. (channel name, the events that go there.)
# One channel per event is offered too, but this is the default: thirteen channels is a lot of
# sidebar for something most people skim.
GROUPED = [
    ("message-log", ["message_delete", "message_edit", "message_purge"]),
    ("member-log", ["member_join", "member_leave", "member_nickname", "member_roles"]),
    ("moderation-log", ["member_ban", "member_unban"]),
    ("voice-log", ["voice_activity"]),
    ("server-log", ["channel_changes", "role_changes", "server_changes"]),
]
# The numbered cases from /ban and friends belong beside the ban entries.
MODLOG_CHANNEL = "moderation-log"


def _trim(text: str, limit: int = MAX_FIELD) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _block(text: str) -> str:
    """Message content inside a fence, so mentions and markdown in it stay inert."""
    return f"```\n{_trim(text).replace('`', 'ˋ')}\n```"


class Logging(commands.Cog, name="Logging"):
    """A record of what happens in the server."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    logging = app_commands.Group(
        name="logging", description="Keep a record of what happens in the server",
        guild_only=True, default_permissions=discord.Permissions(manage_guild=True))

    # ── routing ──────────────────────────────────────────────────────
    @staticmethod
    def _destination(cfg: dict, key: str) -> Optional[int]:
        """Where this event goes, or None when it is switched off or has nowhere to go."""
        if not cfg.get("logging_enabled"):
            return None
        setting = (cfg.get("log_events") or {}).get(key)
        if not setting or not setting.get("on"):
            return None
        # The event's own channel wins; otherwise everything shares one.
        return setting.get("channel") or cfg.get("log_channel")

    @staticmethod
    def _all_destinations(cfg: dict) -> set:
        out = set()
        if cfg.get("log_channel"):
            out.add(int(cfg["log_channel"]))
        for setting in (cfg.get("log_events") or {}).values():
            if isinstance(setting, dict) and setting.get("channel"):
                out.add(int(setting["channel"]))
        return out

    async def _wanted(self, guild: discord.Guild, key: str):
        """The config, but only when this event is actually going somewhere.

        Checked before any audit log lookup so a server with logging off never spends a
        request working out who did something nobody is recording.
        """
        if guild is None:
            return None
        cfg = await GuildConfig.get(self.bot, guild.id)
        return cfg if self._destination(cfg, key) else None

    async def _send(self, guild: discord.Guild, key: str, embed: discord.Embed):
        cfg = await GuildConfig.get(self.bot, guild.id)
        channel_id = self._destination(cfg, key)
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            return

        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[Logging] couldn't post {key} in {guild.id}: {e}")

    def _skip_channel(self, cfg: dict, channel) -> bool:
        """A log channel is never itself logged, or the log talks about itself forever."""
        return channel is not None and channel.id in self._all_destinations(cfg)

    async def _actor(self, guild: discord.Guild, action, target_id=None):
        """Who did it, from the audit log. None when we can't see it or can't be sure."""
        if not guild.me.guild_permissions.view_audit_log:
            return None
        try:
            async for entry in guild.audit_logs(limit=6, action=action):
                age = (discord.utils.utcnow() - entry.created_at).total_seconds()
                if age > AUDIT_WINDOW:
                    continue
                if target_id is not None and getattr(entry.target, "id", None) != target_id:
                    continue
                return entry
        except (discord.Forbidden, discord.HTTPException):
            return None
        return None

    def _own_action(self, cfg: dict, entry) -> bool:
        """Whether the moderation log has already recorded this.

        A ban placed with /ban goes through the bot, so Discord fires on_member_ban as well and
        the same ban would be written twice: once as a numbered case, once here. When the mod
        log is set up it owns those, and the server log stays out of the way. When it isn't,
        this is the only record there would be, so it gets logged after all.
        """
        if not cfg.get("modlog_channel"):
            return False
        return (entry is not None and entry.user is not None
                and entry.user.id == self.bot.user.id)

    @staticmethod
    def _stamp(embed: discord.Embed, who) -> discord.Embed:
        if who is not None:
            embed.set_author(name=str(who), icon_url=who.display_avatar.url)
            embed.set_footer(text=f"ID {who.id}")
        embed.timestamp = discord.utils.utcnow()
        return embed

    # ── messages ─────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.guild is None or message.author.id == self.bot.user.id:
            return
        cfg = await self._wanted(message.guild, "message_delete")
        if cfg is None or self._skip_channel(cfg, message.channel):
            return

        # A deleted image belongs to MediaLog, which has the file itself. Reporting it here as
        # well would show the same deletion twice, once without the picture.
        if message.attachments and cfg.get("medialog_enabled") and cfg.get("medialog_channel"):
            return

        embed = discord.Embed(
            title="Message deleted", color=RED,
            description=f"In {message.channel.mention}")
        if message.content:
            embed.add_field(name="Content", value=_block(message.content), inline=False)
        else:
            embed.add_field(name="Content", value="*no text*", inline=False)
        if message.attachments:
            embed.add_field(
                name="Attachments",
                value=_trim(", ".join(a.filename for a in message.attachments), 300),
                inline=False)
        await self._send(message.guild, "message_delete", self._stamp(embed, message.author))

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.guild is None or before.author.id == self.bot.user.id:
            return
        # Discord fires an edit when it unfurls a link into a preview. Nobody typed anything,
        # so without this the log fills with edits that never happened.
        if before.content == after.content:
            return
        cfg = await self._wanted(before.guild, "message_edit")
        if cfg is None or self._skip_channel(cfg, before.channel):
            return

        embed = discord.Embed(
            title="Message edited", color=AMBER,
            description=f"In {after.channel.mention} · [jump]({after.jump_url})")
        embed.add_field(name="Before", value=_block(before.content or "*empty*"), inline=False)
        embed.add_field(name="After", value=_block(after.content or "*empty*"), inline=False)
        await self._send(before.guild, "message_edit", self._stamp(embed, before.author))

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list):
        if not messages:
            return
        first = messages[0]
        if first.guild is None:
            return
        cfg = await self._wanted(first.guild, "message_purge")
        if cfg is None or self._skip_channel(cfg, first.channel):
            return

        authors = {}
        for m in messages:
            authors[m.author] = authors.get(m.author, 0) + 1
        lines = [f"{who.mention} · {count}" for who, count in
                 sorted(authors.items(), key=lambda kv: -kv[1])[:10]]

        embed = discord.Embed(
            title="Messages purged", color=RED,
            description=f"**{len(messages)}** messages removed from {first.channel.mention}",
            timestamp=discord.utils.utcnow())
        embed.add_field(name="Who they were from", value="\n".join(lines) or "unknown",
                        inline=False)
        await self._send(first.guild, "message_purge", embed)

    # ── members ──────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if await self._wanted(member.guild, "member_join") is None:
            return
        embed = discord.Embed(title="Member joined", color=GREEN,
                              description=f"{member.mention} is member number "
                                          f"{member.guild.member_count or 0:,}")
        embed.add_field(name="Account created",
                        value=discord.utils.format_dt(member.created_at, "R"), inline=True)
        await self._send(member.guild, "member_join", self._stamp(embed, member))

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if await self._wanted(member.guild, "member_leave") is None:
            return
        held = [r.mention for r in member.roles if not r.is_default()]
        embed = discord.Embed(title="Member left", color=GREY,
                              description=f"{member.mention} left the server")
        if member.joined_at:
            embed.add_field(name="They joined",
                            value=discord.utils.format_dt(member.joined_at, "R"), inline=True)
        if held:
            embed.add_field(name="Roles they had",
                            value=_trim(", ".join(held), 500), inline=False)
        await self._send(member.guild, "member_leave", self._stamp(embed, member))

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user):
        cfg = await self._wanted(guild, "member_ban")
        if cfg is None:
            return
        entry = await self._actor(guild, discord.AuditLogAction.ban, user.id)
        if self._own_action(cfg, entry):
            return
        embed = discord.Embed(title="Member banned", color=RED,
                              description=f"{user.mention} was banned")
        if entry is not None:
            embed.add_field(name="By", value=entry.user.mention if entry.user else "unknown",
                            inline=True)
            embed.add_field(name="Reason", value=_trim(entry.reason or "none given", 300),
                            inline=True)
        await self._send(guild, "member_ban", self._stamp(embed, user))

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user):
        cfg = await self._wanted(guild, "member_unban")
        if cfg is None:
            return
        entry = await self._actor(guild, discord.AuditLogAction.unban, user.id)
        if self._own_action(cfg, entry):
            return
        embed = discord.Embed(title="Member unbanned", color=GREEN,
                              description=f"{user.mention} was unbanned")
        if entry is not None and entry.user:
            embed.add_field(name="By", value=entry.user.mention, inline=True)
        await self._send(guild, "member_unban", self._stamp(embed, user))

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        # The noisiest event on the gateway, so both branches leave as early as they can.
        if before.nick != after.nick:
            if await self._wanted(after.guild, "member_nickname") is not None:
                embed = discord.Embed(title="Nickname changed", color=BLURPLE)
                embed.add_field(name="Before", value=before.nick or "*none*", inline=True)
                embed.add_field(name="After", value=after.nick or "*none*", inline=True)
                await self._send(after.guild, "member_nickname", self._stamp(embed, after))

        if before.roles != after.roles:
            if await self._wanted(after.guild, "member_roles") is None:
                return
            gained = [r for r in after.roles if r not in before.roles]
            lost = [r for r in before.roles if r not in after.roles]
            if not (gained or lost):
                return
            embed = discord.Embed(title="Roles changed", color=BLURPLE)
            if gained:
                embed.add_field(name="Given",
                                value=_trim(", ".join(r.mention for r in gained), 500),
                                inline=False)
            if lost:
                embed.add_field(name="Taken away",
                                value=_trim(", ".join(r.mention for r in lost), 500),
                                inline=False)
            await self._send(after.guild, "member_roles", self._stamp(embed, after))

    # ── voice ────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after):
        # This fires for muting, deafening, starting a stream and turning on a camera as well
        # as for actually going somewhere. Only movement is worth a log line; without this the
        # channel fills with entries for somebody toggling their own mic.
        if before.channel == after.channel:
            return
        # Music bots rejoin on every track. Logging them would drown out the people.
        if member.bot:
            return
        if await self._wanted(member.guild, "voice_activity") is None:
            return

        if before.channel is None:
            embed = discord.Embed(title="Joined voice", color=GREEN,
                                  description=f"{member.mention} joined "
                                              f"**{after.channel.name}**")
        elif after.channel is None:
            embed = discord.Embed(title="Left voice", color=GREY,
                                  description=f"{member.mention} left "
                                              f"**{before.channel.name}**")
        else:
            embed = discord.Embed(title="Moved voice channel", color=BLURPLE,
                                  description=f"{member.mention} moved from "
                                              f"**{before.channel.name}** to "
                                              f"**{after.channel.name}**")
        await self._send(member.guild, "voice_activity", self._stamp(embed, member))

    # ── the server itself ────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        await self._channel_event(channel, "created", GREEN)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        await self._channel_event(channel, "deleted", RED)

    async def _channel_event(self, channel, what: str, colour: int):
        cfg = await self._wanted(channel.guild, "channel_changes")
        if cfg is None or self._skip_channel(cfg, channel):
            return
        embed = discord.Embed(
            title=f"Channel {what}", color=colour,
            description=f"**#{channel.name}**", timestamp=discord.utils.utcnow())
        if getattr(channel, "category", None):
            embed.add_field(name="Category", value=channel.category.name, inline=True)
        await self._send(channel.guild, "channel_changes", embed)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        # Only the name, deliberately. Permission overwrites fire this constantly and produce
        # a log nobody reads.
        if before.name == after.name:
            return
        cfg = await self._wanted(after.guild, "channel_changes")
        if cfg is None or self._skip_channel(cfg, after):
            return
        embed = discord.Embed(title="Channel renamed", color=BLURPLE,
                              description=f"**#{before.name}** is now **#{after.name}**",
                              timestamp=discord.utils.utcnow())
        await self._send(after.guild, "channel_changes", embed)

    @commands.Cog.listener()
    async def on_guild_role_create(self, role):
        await self._role_event(role.guild, f"**{role.name}** was created", "Role created", GREEN)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role):
        await self._role_event(role.guild, f"**{role.name}** was deleted", "Role deleted", RED)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before, after):
        if before.name == after.name:
            return
        await self._role_event(after.guild, f"**{before.name}** is now **{after.name}**",
                               "Role renamed", BLURPLE)

    async def _role_event(self, guild, description: str, title: str, colour: int):
        if await self._wanted(guild, "role_changes") is None:
            return
        embed = discord.Embed(title=title, color=colour, description=description,
                              timestamp=discord.utils.utcnow())
        await self._send(guild, "role_changes", embed)

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        if await self._wanted(after, "server_changes") is None:
            return
        changes = []
        if before.name != after.name:
            changes.append(f"Name: **{before.name}** to **{after.name}**")
        if before.icon != after.icon:
            changes.append("The server icon changed")
        if before.owner_id != after.owner_id:
            changes.append(f"Ownership moved to <@{after.owner_id}>")
        if not changes:
            return
        embed = discord.Embed(title="Server updated", color=BLURPLE,
                              description="\n".join(changes),
                              timestamp=discord.utils.utcnow())
        await self._send(after, "server_changes", embed)

    # ── /logging setup ───────────────────────────────────────────────
    @staticmethod
    def _layout(style: str) -> list:
        """The channels to build, as (name, events)."""
        if style == "single":
            return [("server-log", list(EVENT_KEYS))]
        if style == "each":
            return [(key.replace("_", "-"), [key]) for key in EVENT_KEYS]
        return [(name, list(keys)) for name, keys in GROUPED]

    @logging.command(
        name="setup",
        description="Build the log channels and switch everything on, all in one go")
    @app_commands.describe(
        style="How many channels to make.",
        category="What to call the category they go in.")
    @app_commands.choices(style=[
        app_commands.Choice(name="A few channels, grouped by what they record", value="grouped"),
        app_commands.Choice(name="One channel for everything", value="single"),
        app_commands.Choice(name="A separate channel for every kind of event", value="each"),
    ])
    @app_commands.checks.cooldown(1, 60.0)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_logs(self, interaction: discord.Interaction,
                         style: Optional[app_commands.Choice[str]] = None,
                         category: Optional[app_commands.Range[str, 1, 90]] = None):
        guild = interaction.guild
        chosen = style.value if style else "grouped"
        category_name = (category or DEFAULT_CATEGORY).strip()

        if not guild.me.guild_permissions.manage_channels:
            await interaction.response.send_message(
                "I need the **Manage Channels** permission to make these for you. Grant it and "
                "run this again, or set the channels up yourself with `/logging channel`.",
                ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        # Without Manage Roles the bot can't deny @everyone, and a log full of deleted messages
        # would be readable by the whole server. Say so rather than quietly building it.
        can_hide = guild.me.guild_permissions.manage_roles
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, embed_links=True),
        } if can_hide else None

        reason = f"Log channels, set up by {interaction.user}"

        # Reuse anything already there, so running this twice doesn't leave two of everything.
        parent = discord.utils.find(
            lambda c: c.name.lower() == category_name.lower(), guild.categories)
        reused_category = parent is not None
        if parent is None:
            try:
                parent = await guild.create_category(
                    category_name, overwrites=overwrites, reason=reason)
            except discord.Forbidden:
                await interaction.followup.send(
                    "Discord wouldn't let me make the category. Check that I have Manage "
                    "Channels and that my role is high enough.", ephemeral=True)
                return
            except discord.HTTPException as e:
                await interaction.followup.send(f"That didn't work: {e}", ephemeral=True)
                return

        routes, made, reused, failed = {}, [], [], None
        for name, keys in self._layout(chosen):
            existing = discord.utils.find(
                lambda c: c.name == name, parent.text_channels)
            if existing is not None:
                channel = existing
                reused.append(channel)
            else:
                try:
                    channel = await guild.create_text_channel(
                        name, category=parent, reason=reason)
                    made.append(channel)
                except (discord.Forbidden, discord.HTTPException) as e:
                    # Keep whatever was built rather than abandoning half a category.
                    failed = e
                    break
            for key in keys:
                routes[key] = channel

        if not routes:
            await interaction.followup.send(
                f"I couldn't create any channels: {failed}", ephemeral=True)
            return

        events = {key: {"on": key in routes,
                        "channel": routes[key].id if key in routes else None}
                  for key in EVENT_KEYS}
        values = {"logging_enabled": True, "log_events": events}
        # Each channel is named on its own events, so there is nothing left for a shared one
        # to catch. Clearing it stops an old setting quietly collecting anything added later.
        values["log_channel"] = routes[EVENT_KEYS[0]].id if chosen == "single" else None

        modlog = next((c for c in list(routes.values())
                       if c.name == MODLOG_CHANNEL), None)
        if modlog is not None:
            values["modlog_channel"] = modlog.id

        await GuildConfig.update(self.bot, guild.id, values)

        embed = discord.Embed(
            title="Logging is set up", color=GREEN,
            description=f"Everything is switched on and going to **{parent.name}**.",
            timestamp=discord.utils.utcnow())

        seen, lines = set(), []
        for key in EVENT_KEYS:
            channel = routes.get(key)
            if channel is None or channel.id in seen:
                continue
            seen.add(channel.id)
            covered = [label for k, icon, label in EVENTS if routes.get(k) is channel]
            lines.append(f"{channel.mention}\n{'  ·  '.join(covered)}")
        embed.add_field(name="Where things go", value="\n\n".join(lines)[:1024], inline=False)

        notes = []
        if made:
            notes.append(f"Made {len(made)} channel{'s' if len(made) != 1 else ''}.")
        if reused or reused_category:
            notes.append("Reused what was already there.")
        if modlog is not None:
            notes.append(f"Moderation cases now go to {modlog.mention} too.")
        if not can_hide:
            embed.color = AMBER
            notes.append("⚠️ I don't have Manage Roles, so these channels are visible to "
                         "everyone. Hide the category yourself, or the whole server will be "
                         "able to read your deleted messages.")
        if failed is not None:
            embed.color = AMBER
            notes.append(f"⚠️ I had to stop early: {failed}")
        if notes:
            embed.add_field(name="Notes", value="\n".join(notes)[:1024], inline=False)
        embed.set_footer(text="Change any of it with /logging event or on the dashboard.")

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── commands ─────────────────────────────────────────────────────
    @logging.command(name="channel", description="Send every kind of log to one channel")
    @app_commands.describe(channel="Where the logs go. Leave blank to switch logging off.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_channel(self, interaction: discord.Interaction,
                          channel: Optional[discord.TextChannel] = None):
        if channel is None:
            await GuildConfig.update(self.bot, interaction.guild.id,
                                     {"logging_enabled": False})
            await interaction.response.send_message(
                "Logging is off. Your choices are kept, so setting a channel again brings them "
                "back.", ephemeral=True)
            return

        perms = channel.permissions_for(interaction.guild.me)
        missing = [n for n, ok in (("View Channel", perms.view_channel),
                                   ("Send Messages", perms.send_messages),
                                   ("Embed Links", perms.embed_links)) if not ok]
        if missing:
            await interaction.response.send_message(
                f"I'm missing **{', '.join(missing)}** in {channel.mention}. Grant those and "
                f"try again.", ephemeral=True)
            return

        cfg = await GuildConfig.get(self.bot, interaction.guild.id)
        events = cfg.get("log_events") or {}
        # First time through, switch everything on. Somebody who picks a log channel wants a
        # log, not an empty one they have to fill in twelve times.
        if not events:
            events = {key: {"on": True, "channel": None} for key in EVENT_KEYS}

        await GuildConfig.update(self.bot, interaction.guild.id, {
            "logging_enabled": True,
            "log_channel": channel.id,
            "log_events": events,
        })
        await interaction.response.send_message(
            f"Logging everything to {channel.mention}. Use `/logging status` to see the list, "
            f"or the dashboard to send some kinds somewhere else.", ephemeral=True)

    @logging.command(name="event", description="Switch one kind of log on or off, or move it")
    @app_commands.describe(event="Which kind of log.", on="On or off.",
                           channel="Send this one somewhere else. Blank uses the main channel.")
    @app_commands.choices(event=[
        app_commands.Choice(name=f"{icon} {label}", value=key) for key, icon, label in EVENTS])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_event(self, interaction: discord.Interaction,
                        event: app_commands.Choice[str], on: bool,
                        channel: Optional[discord.TextChannel] = None):
        cfg = await GuildConfig.get(self.bot, interaction.guild.id)
        events = dict(cfg.get("log_events") or {})
        events[event.value] = {"on": on, "channel": channel.id if channel else None}

        values = {"log_events": events}
        if on and not (cfg.get("log_channel") or channel):
            await interaction.response.send_message(
                "There's nowhere to put it yet. Run `/logging channel` first, or give this one "
                "a channel of its own.", ephemeral=True)
            return
        if on:
            values["logging_enabled"] = True

        await GuildConfig.update(self.bot, interaction.guild.id, values)
        where = channel.mention if channel else "the main log channel"
        await interaction.response.send_message(
            f"**{event.name}** is {'on, going to ' + where if on else 'off'}.", ephemeral=True)

    # ── the other three logs ─────────────────────────────────────────
    # These used to be /logchannel, /modlogchannel and /trackping, spread across three cogs
    # with three different naming conventions. Somebody typing /log got a menu of unrelated
    # things and no way to tell which was which, so all four logs answer to /logging now.
    # The settings still belong to the cogs that act on them; only the switches moved.
    @staticmethod
    def _missing(guild: discord.Guild, channel: discord.TextChannel,
                 files: bool = False) -> list:
        perms = channel.permissions_for(guild.me)
        checks = [("View Channel", perms.view_channel),
                  ("Send Messages", perms.send_messages),
                  ("Embed Links", perms.embed_links)]
        if files:
            checks.append(("Attach Files", perms.attach_files))
        return [name for name, ok in checks if not ok]

    @logging.command(name="media",
                     description="Where deleted images, videos and voice memos are kept")
    @app_commands.describe(channel="Where to post them. Leave blank to switch this off.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def media(self, interaction: discord.Interaction,
                    channel: Optional[discord.TextChannel] = None):
        if channel is None:
            await GuildConfig.update(self.bot, interaction.guild.id,
                                     {"medialog_enabled": False})
            await interaction.response.send_message(
                "Deleted media logging is off. Give this command a channel to switch it back "
                "on.", ephemeral=True)
            return

        missing = self._missing(interaction.guild, channel, files=True)
        if missing:
            await interaction.response.send_message(
                f"I'm missing **{', '.join(missing)}** in {channel.mention}. Grant those and "
                f"try again.", ephemeral=True)
            return

        await GuildConfig.update(self.bot, interaction.guild.id,
                                 {"medialog_enabled": True, "medialog_channel": channel.id})
        embed = discord.Embed(
            title="Deleted media log is on", color=GREEN,
            description=f"Deleted images, video and audio go to {channel.mention} with the "
                        f"file itself attached, from members and bots alike.")
        if not interaction.guild.me.guild_permissions.view_audit_log:
            embed.add_field(
                name="Worth knowing",
                value="I don't have **View Audit Log**, so \"deleted by\" will always say "
                      "unknown. Grant it if you want to see who removed something.",
                inline=False)
        embed.set_footer(text="Coverage starts now. Files posted before this aren't kept.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @logging.command(name="moderation",
                     description="Where numbered moderation cases are posted")
    @app_commands.describe(channel="Where to post them. Leave blank to switch this off.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def moderation(self, interaction: discord.Interaction,
                         channel: Optional[discord.TextChannel] = None):
        if channel is None:
            await GuildConfig.update(self.bot, interaction.guild.id,
                                     unset={"modlog_channel": ""})
            await interaction.response.send_message(
                "The moderation log is off. Actions still work and are still recorded against "
                "each member, they just won't be posted anywhere.", ephemeral=True)
            return

        missing = self._missing(interaction.guild, channel)
        if missing:
            await interaction.response.send_message(
                f"I'm missing **{', '.join(missing)}** in {channel.mention}. Grant those and "
                f"try again.", ephemeral=True)
            return

        await GuildConfig.update(self.bot, interaction.guild.id,
                                 {"modlog_channel": channel.id})
        await interaction.response.send_message(
            f"Moderation cases will be posted to {channel.mention}, each with its own number "
            f"so you can look it up with `/modlogs`.", ephemeral=True)

    @logging.command(name="reminders",
                     description="Where the survey reminders sent to new members are recorded")
    @app_commands.describe(channel="Where to post them. Leave blank to switch this off.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reminders(self, interaction: discord.Interaction,
                        channel: Optional[discord.TextChannel] = None):
        if channel is None:
            await GuildConfig.update(self.bot, interaction.guild.id,
                                     {"pinglog_enabled": False})
            await interaction.response.send_message(
                "Survey reminder logging is off. The ones already recorded are still there.",
                ephemeral=True)
            return

        missing = self._missing(interaction.guild, channel)
        if missing:
            await interaction.response.send_message(
                f"I'm missing **{', '.join(missing)}** in {channel.mention}. Grant those and "
                f"try again.", ephemeral=True)
            return

        await GuildConfig.update(self.bot, interaction.guild.id,
                                 {"pinglog_enabled": True, "pinglog_channel": channel.id})
        cfg = await GuildConfig.get(self.bot, interaction.guild.id)
        note = ("" if cfg.get("discovery_channel") else
                "\n\nYou haven't run `/setchannel` yet, so there are no reminders to record. "
                "Set that up first.")
        await interaction.response.send_message(
            f"Survey reminders will be recorded in {channel.mention}, with the group they went "
            f"to, how many members they reached and when.\nThe reminder itself deletes after "
            f"two seconds, so this is the only lasting record of it.{note}", ephemeral=True)

    # ── /logging status ──────────────────────────────────────────────
    async def _reminder_summary(self, guild_id: int) -> Optional[str]:
        """A line or two about recent survey reminders, or None if there's nothing to say."""
        try:
            since = (datetime.datetime.now(datetime.timezone.utc)
                     - datetime.timedelta(days=REMINDER_DAYS))
            events = await asyncio.to_thread(lambda: list(
                Database.get_bot_database(self.bot.MongoClient)["ping_events"].find(
                    {"guild_id": guild_id, "created_at": {"$gte": since}})))
        except Exception as e:
            print(f"[Logging] couldn't read reminder history: {e}")
            return None
        if not events:
            return None

        reach = sum(e.get("reach", 0) for e in events)
        lines = [f"**{len(events)}** sent in the last {REMINDER_DAYS} days, reaching "
                 f"**{reach}** members"]
        recent = sorted((e for e in events
                         if isinstance(e.get("created_at"), datetime.datetime)),
                        key=lambda e: e["created_at"], reverse=True)[:3]
        for e in recent:
            when = int(e["created_at"].replace(
                tzinfo=datetime.timezone.utc).timestamp())
            lines.append(f"`{e.get('cohort', '?')}` reached {e.get('reach', 0)} · <t:{when}:R>")
        return "\n".join(lines)

    @logging.command(name="status",
                     description="Every log, what it records and where it goes")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        cfg = await GuildConfig.get(self.bot, guild.id)

        def channel_of(key):
            cid = cfg.get(key)
            return guild.get_channel(int(cid)) if cid else None

        embed = discord.Embed(title=f"Logs in {guild.name}", color=BLURPLE,
                              timestamp=discord.utils.utcnow())

        # ── the server log, event by event ──
        events = cfg.get("log_events") or {}
        main = channel_of("log_channel")
        if not cfg.get("logging_enabled"):
            embed.add_field(
                name="📋 Server log",
                value="**Off.** `/logging setup` builds the channels and switches it all on, "
                      "or `/logging channel` if you'd rather pick one yourself.",
                inline=False)
        else:
            on_lines, off_lines = [], []
            for key, icon, label in EVENTS:
                setting = events.get(key) or {}
                if not setting.get("on"):
                    off_lines.append(label)
                    continue
                own = setting.get("channel")
                where = guild.get_channel(int(own)) if own else None
                if own and where is None:
                    on_lines.append(f"{icon} {label} · ⚠️ its channel is gone")
                elif where is not None:
                    on_lines.append(f"{icon} {label} · {where.mention}")
                else:
                    on_lines.append(f"{icon} {label}")

            heading = (f"Everything goes to {main.mention} unless it says otherwise."
                       if main else
                       "No shared channel, so only events with one of their own are recorded.")
            embed.add_field(
                name=f"📋 Server log · {len(on_lines)} of {len(EVENTS)} on",
                value=(heading + "\n" + "\n".join(on_lines))[:1024], inline=False)
            if off_lines:
                embed.add_field(name="Not recording",
                                value=_trim(", ".join(off_lines), 1000), inline=False)

        # ── the three that live elsewhere ──
        modlog = channel_of("modlog_channel")
        embed.add_field(
            name="🔨 Moderation log",
            value=(f"Numbered cases go to {modlog.mention}." if modlog else
                   "**Off.** `/logging moderation` switches it on."),
            inline=False)

        media = channel_of("medialog_channel")
        media_on = bool(cfg.get("medialog_enabled") and media)
        embed.add_field(
            name="🗄️ Deleted media",
            value=(f"Files go to {media.mention} with the picture attached." if media_on else
                   "**Off.** `/logging media` switches it on."),
            inline=False)

        pings = channel_of("pinglog_channel")
        pings_on = bool(cfg.get("pinglog_enabled") and pings)
        value = (f"Recorded in {pings.mention}." if pings_on else
                 "**Off.** `/logging reminders` switches it on.")
        summary = await self._reminder_summary(guild.id) if pings_on else None
        if summary:
            value += "\n" + summary
        elif pings_on:
            value += ("\nNothing yet. They go out around midday to whoever joined 8 days "
                      "earlier. `/forcesurvey days:0` sends one now.")
        embed.add_field(name="⭐ Survey reminders", value=value[:1024], inline=False)

        embed.set_footer(text="Change any of it here or on the dashboard.")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Logging(bot))
    print("Logging cog loaded ✓")
