"""Tracks pings that reach a group of people and logs them to one channel.

"Reach" is the number of distinct members a ping actually woke up, which is the interesting
number and not one Discord shows you anywhere. A role ping is counted by walking that role's
members, overlapping roles are de-duplicated, and @everyone / @here fall back to the member
count rather than enumerating everybody.

Only pings at or above the configured minimum get logged, so ordinary conversation where
somebody mentions one friend stays out of the channel.
"""

import asyncio
import datetime
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import Database

DEFAULT_MINIMUM = 5
EVENT_TTL_DAYS = 30
CFG_TTL = 300
SUMMARY_DAYS = 7

COLOR_PING = 0xF39C12
COLOR_BIG = 0xE74C3C
COLOR_INFO = 0x5865F2
COLOR_WARN = 0xE67E22

BIG_PING = 50          # coloured red above this, purely cosmetic


class PingTracker(commands.Cog, name="Ping Tracking"):
    """Logs who pinged how many people, and when."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cfg: dict[int, tuple[Optional[dict], float]] = {}

    async def _run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(lambda: fn(*args, **kwargs))

    @property
    def _db(self):
        return Database.get_bot_database(self.bot.MongoClient)

    async def cog_load(self):
        try:
            await self._run(self._ensure_indexes)
        except Exception as e:
            print(f"[PingLog] index setup failed: {e}")

    def _ensure_indexes(self):
        events = self._db["ping_events"]
        events.create_index([("guild_id", 1), ("created_at", -1)], name="guild_recent")
        events.create_index([("guild_id", 1), ("user_id", 1)], name="guild_user")
        # Events are only kept for summaries, so let Mongo expire them rather than growing
        # forever on a public bot.
        events.create_index("created_at", expireAfterSeconds=EVENT_TTL_DAYS * 86400,
                            name="ttl_created")

    # ── config ───────────────────────────────────────────────────────
    async def _get_config(self, guild_id: int) -> Optional[dict]:
        now = time.monotonic()
        hit = self._cfg.get(guild_id)
        if hit is not None and now - hit[1] < CFG_TTL:
            return hit[0]
        try:
            doc = await self._run(self._db["servers"].find_one, {"guild_id": guild_id})
        except Exception as e:
            print(f"[PingLog] config lookup failed for {guild_id}: {e}")
            return hit[0] if hit else None
        cfg = doc if (doc and doc.get("pinglog_enabled") and doc.get("pinglog_channel")) else None
        self._cfg[guild_id] = (cfg, now)
        return cfg

    # ── working out the reach ────────────────────────────────────────
    @staticmethod
    def _measure(message: discord.Message) -> tuple[int, str, list]:
        """Returns (people reached, what kind of ping, role names)."""
        guild = message.guild

        if message.mention_everyone:
            # mention_everyone covers both, and is only True when the author actually had
            # permission, so this can't be faked by typing the text.
            kind = "@here" if "@here" in (message.content or "") else "@everyone"
            return (guild.member_count or 0), kind, []

        reached: set[int] = set()
        for member in message.mentions:
            reached.add(member.id)
        role_names = []
        for role in message.role_mentions:
            role_names.append(role.name)
            # Overlapping roles are handled for free by the set.
            for member in role.members:
                reached.add(member.id)

        reached.discard(message.author.id)      # pinging yourself isn't reach
        if role_names and message.mentions:
            kind = "roles and members"
        elif role_names:
            kind = "role" if len(role_names) == 1 else "roles"
        else:
            kind = "members"
        return len(reached), kind, role_names

    # ── the listener ─────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None:
            return
        # Nothing to measure, and this is the cheap early exit for almost every message.
        if not (message.mention_everyone or message.role_mentions or message.mentions):
            return

        cfg = await self._get_config(message.guild.id)
        if cfg is None:
            return
        log_channel_id = cfg["pinglog_channel"]
        if message.channel.id == log_channel_id:
            return                       # don't track our own log channel

        minimum = cfg.get("pinglog_minimum") or DEFAULT_MINIMUM
        reach, kind, role_names = self._measure(message)
        if reach < minimum:
            return

        channel = message.guild.get_channel(log_channel_id)
        if channel is None:
            return

        sent_at = int(message.created_at.timestamp())
        embed = discord.Embed(
            title="Ping tracked",
            description=f"**{reach}** {'person' if reach == 1 else 'people'} pinged via {kind}",
            color=COLOR_BIG if reach >= BIG_PING else COLOR_PING,
            timestamp=message.created_at,
        )
        embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        embed.add_field(
            name="Sent by",
            value=f"<@{message.author.id}>\n`{message.author.id}`"
                  + ("\n*(bot)*" if message.author.bot else ""),
            inline=True)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        embed.add_field(name="Reached", value=f"**{reach}** members", inline=True)
        embed.add_field(name="When", value=f"<t:{sent_at}:f>\n<t:{sent_at}:R>", inline=True)
        if role_names:
            embed.add_field(
                name="Roles pinged",
                value=", ".join(f"`@{r}`" for r in role_names)[:1024], inline=True)
        embed.add_field(
            name="Message",
            value=(message.content or "*(no text)*")[:500], inline=False)
        embed.add_field(name="Jump", value=f"[Go to message]({message.jump_url})", inline=False)

        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            print(f"[PingLog] can't post in the log channel for guild {message.guild.id}")
            return
        except discord.HTTPException as e:
            print(f"[PingLog] send failed: {e}")
            return

        try:
            await self._run(self._db["ping_events"].insert_one, {
                "guild_id": message.guild.id,
                "user_id": message.author.id,
                "user_tag": str(message.author),
                "channel_id": message.channel.id,
                "message_id": message.id,
                "reach": reach,
                "kind": kind,
                "roles": role_names,
                "created_at": datetime.datetime.now(datetime.timezone.utc),
            })
        except Exception as e:
            print(f"[PingLog] couldn't record event: {e}")

    # ── /trackping ───────────────────────────────────────────────────
    @app_commands.command(
        name="trackping",
        description="Log pings that reach a group of people, and see recent ping activity")
    @app_commands.describe(
        channel="Where to post the logs. Leave blank to just see the current setup",
        minimum="Only log pings reaching at least this many people (default 5)")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def trackping(self, interaction: discord.Interaction,
                        channel: Optional[discord.TextChannel] = None,
                        minimum: Optional[app_commands.Range[int, 1, 1000]] = None):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        if channel is not None:
            perms = channel.permissions_for(guild.me)
            missing = [n for n, ok in (("View Channel", perms.view_channel),
                                       ("Send Messages", perms.send_messages),
                                       ("Embed Links", perms.embed_links)) if not ok]
            if missing:
                await interaction.followup.send(
                    f"I'm missing **{', '.join(missing)}** in {channel.mention}. "
                    f"Grant those and run this again.", ephemeral=True)
                return

            updates = {"pinglog_enabled": True, "pinglog_channel": channel.id}
            if minimum is not None:
                updates["pinglog_minimum"] = minimum
            await self._run(self._db["servers"].update_one, {"guild_id": guild.id},
                            {"$set": updates}, upsert=True)
            self._cfg.pop(guild.id, None)

            threshold = minimum or DEFAULT_MINIMUM
            await interaction.followup.send(
                f"Ping tracking is on. Anything reaching **{threshold}** or more people gets "
                f"logged to {channel.mention}, with who sent it, how many it woke up and when.\n"
                f"Run `/trackping` on its own any time to see a summary, or `/trackpingoff` "
                f"to stop.",
                ephemeral=True)
            return

        # No channel given, so show the current setup and a recent summary.
        try:
            doc = await self._run(self._db["servers"].find_one, {"guild_id": guild.id}) or {}
            since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
                days=SUMMARY_DAYS)
            events = await self._run(lambda: list(self._db["ping_events"].find(
                {"guild_id": guild.id, "created_at": {"$gte": since}})))
        except Exception as e:
            await interaction.followup.send(f"Couldn't read the data: {e}", ephemeral=True)
            return

        enabled = bool(doc.get("pinglog_enabled") and doc.get("pinglog_channel"))
        log_channel = guild.get_channel(doc.get("pinglog_channel") or 0)

        embed = discord.Embed(
            title="Ping tracking",
            description=("**On**" if enabled else
                         "**Off.** Run `/trackping channel:#somewhere` to switch it on."),
            color=COLOR_INFO if enabled else COLOR_WARN,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Logging to",
                        value=log_channel.mention if log_channel else "*not set*", inline=True)
        embed.add_field(name="Minimum reach",
                        value=f"{doc.get('pinglog_minimum') or DEFAULT_MINIMUM} people",
                        inline=True)

        if events:
            total = len(events)
            people = sum(e.get("reach", 0) for e in events)
            biggest = max(events, key=lambda e: e.get("reach", 0))
            by_user: dict[int, int] = {}
            for e in events:
                by_user[e["user_id"]] = by_user.get(e["user_id"], 0) + 1
            top = sorted(by_user.items(), key=lambda kv: kv[1], reverse=True)[:5]

            embed.add_field(
                name=f"Last {SUMMARY_DAYS} days",
                value=(f"**{total}** ping{'' if total == 1 else 's'} logged, waking up "
                       f"**{people}** members in total"),
                inline=False)
            big_when = biggest.get("created_at")
            when = (f"<t:{int(big_when.replace(tzinfo=datetime.timezone.utc).timestamp())}:R>"
                    if isinstance(big_when, datetime.datetime) else "recently")
            embed.add_field(
                name="Biggest",
                value=f"<@{biggest['user_id']}> reached **{biggest.get('reach', 0)}** "
                      f"via {biggest.get('kind', 'a ping')}, {when}",
                inline=False)
            embed.add_field(
                name="Most frequent",
                value="\n".join(f"<@{uid}> — {n} ping{'' if n == 1 else 's'}"
                                for uid, n in top),
                inline=False)
        elif enabled:
            embed.add_field(
                name=f"Last {SUMMARY_DAYS} days",
                value="Nothing logged yet. Either it's been quiet, or no ping has reached the "
                      "minimum.",
                inline=False)

        embed.set_footer(text=f"Logged pings are kept for {EVENT_TTL_DAYS} days")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /trackpingoff ────────────────────────────────────────────────
    @app_commands.command(name="trackpingoff", description="Stop logging pings")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def trackpingoff(self, interaction: discord.Interaction):
        await self._run(self._db["servers"].update_one, {"guild_id": interaction.guild.id},
                        {"$set": {"pinglog_enabled": False}}, upsert=True)
        self._cfg.pop(interaction.guild.id, None)
        await interaction.response.send_message(
            "Ping tracking is off. The pings already logged are still there, and `/trackping` "
            "will still show the summary.", ephemeral=True)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        cfg = await self._get_config(channel.guild.id)
        if cfg and cfg.get("pinglog_channel") == channel.id:
            await self._run(self._db["servers"].update_one, {"guild_id": channel.guild.id},
                            {"$set": {"pinglog_enabled": False}})
            self._cfg.pop(channel.guild.id, None)


async def setup(bot: commands.Bot):
    await bot.add_cog(PingTracker(bot))
    print("PingTracker cog loaded ✓")
