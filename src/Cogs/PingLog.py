"""Logs the survey nudges this bot sends, and how many people each one reached.

The nudge is a bare role mention posted into the ratings channel by main.mention_players, then
deleted two seconds later. That makes it invisible after the fact: you can't tell whether it
fired, or how many people it woke up. This records each one before it vanishes.

"Reach" is the number of distinct members in the cohort role, which is the number that actually
matters and one Discord shows you nowhere. The role is named after the date its members joined,
so the log doubles as a record of which cohort was nudged when.

Deliberately narrow: only this bot's own nudges, only in the configured ratings channel, and
only when the message is a lone role mention. Ordinary pings from members are not tracked.
"""

import asyncio
import datetime
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import Database
import GuildConfig

EVENT_TTL_DAYS = 30
SUMMARY_DAYS = 7

COLOR_PING = 0xF39C12
COLOR_BIG = 0xE74C3C
COLOR_INFO = 0x5865F2
COLOR_WARN = 0xE67E22

BIG_PING = 50          # coloured red above this, purely cosmetic

# The nudge is exactly this and nothing else, which is what makes it safely identifiable.
SURVEY_PING = re.compile(r"^<@&(\d+)>$")


class PingTracker(commands.Cog, name="Ping Tracking"):
    """Records each survey nudge and the number of members it reached."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

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
        # Events are only kept for summaries, so let Mongo expire them rather than growing
        # forever on a public bot.
        events.create_index("created_at", expireAfterSeconds=EVENT_TTL_DAYS * 86400,
                            name="ttl_created")

    async def _get_config(self, guild_id: int) -> Optional[dict]:
        """None when tracking is off here. Shares one cached read with the other cogs."""
        doc = await GuildConfig.get(self.bot, guild_id)
        if not (doc.get("pinglog_enabled") and doc.get("pinglog_channel")):
            return None
        return doc

    def _is_survey_nudge(self, message: discord.Message, cfg: dict) -> bool:
        """A nudge is: sent by this bot, in the configured ratings channel, and consisting of
        one role mention and nothing else."""
        if self.bot.user is None or message.author.id != self.bot.user.id:
            return False
        if message.channel.id != cfg.get("discovery_channel"):
            return False
        if len(message.role_mentions) != 1:
            return False
        return bool(SURVEY_PING.match((message.content or "").strip()))

    # ── the listener ─────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Cheapest possible rejection first: almost every message fails one of these.
        if message.guild is None or not message.role_mentions:
            return
        if self.bot.user is None or message.author.id != self.bot.user.id:
            return

        cfg = await self._get_config(message.guild.id)
        if cfg is None or not self._is_survey_nudge(message, cfg):
            return

        role = message.role_mentions[0]
        reach = len(role.members)

        channel = message.guild.get_channel(cfg["pinglog_channel"])
        if channel is None:
            return

        sent_at = int(message.created_at.timestamp())
        embed = discord.Embed(
            title="Survey nudge sent",
            description=f"Reached **{reach}** {'member' if reach == 1 else 'members'}",
            color=COLOR_BIG if reach >= BIG_PING else COLOR_PING,
            timestamp=message.created_at,
        )
        # The cohort role is named after the date its members joined.
        embed.add_field(name="Cohort", value=f"`{role.name}`", inline=True)
        embed.add_field(name="Reached", value=f"**{reach}**", inline=True)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        embed.add_field(name="When", value=f"<t:{sent_at}:f>\n<t:{sent_at}:R>", inline=False)
        if reach == 0:
            embed.add_field(
                name="Note",
                value="Nobody is in that cohort role, so this nudge reached no one.",
                inline=False)
        embed.set_footer(text="The nudge itself deletes after 2 seconds")

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
                "channel_id": message.channel.id,
                "message_id": message.id,
                "role_id": role.id,
                "cohort": role.name,
                "reach": reach,
                "created_at": datetime.datetime.now(datetime.timezone.utc),
            })
        except Exception as e:
            print(f"[PingLog] couldn't record event: {e}")

    # ── /trackping ───────────────────────────────────────────────────
    @app_commands.command(
        name="trackping",
        description="Log every survey nudge and how many members it reached")
    @app_commands.describe(
        channel="Where to post the logs. Leave blank to just see the current setup")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def trackping(self, interaction: discord.Interaction,
                        channel: Optional[discord.TextChannel] = None):
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

            await GuildConfig.update(self.bot, guild.id,
                                     {"pinglog_enabled": True, "pinglog_channel": channel.id})
            cfg = await GuildConfig.get(self.bot, guild.id)
            note = ("" if cfg.get("discovery_channel") else
                    "\n\nHeads up: you haven't run `/setchannel` yet, so there are no nudges "
                    "to log. Set that up first.")
            await interaction.followup.send(
                f"Nudge tracking is on. Every survey nudge gets logged to {channel.mention} "
                f"with the cohort it targeted, how many members it reached and when.\n"
                f"The nudge itself deletes after 2 seconds, so this is the only lasting record "
                f"of it.{note}",
                ephemeral=True)
            return

        # No channel given, so show the current setup and a recent summary.
        try:
            doc = await GuildConfig.get(self.bot, guild.id)
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
            title="Nudge tracking",
            description=("**On**" if enabled else
                         "**Off.** Run `/trackping channel:#somewhere` to switch it on."),
            color=COLOR_INFO if enabled else COLOR_WARN,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Logging to",
                        value=log_channel.mention if log_channel else "*not set*", inline=True)
        embed.add_field(name="Tracking", value="Survey nudges only", inline=True)

        if events:
            total = len(events)
            people = sum(e.get("reach", 0) for e in events)
            biggest = max(events, key=lambda e: e.get("reach", 0))
            embed.add_field(
                name=f"Last {SUMMARY_DAYS} days",
                value=(f"**{total}** nudge{'' if total == 1 else 's'} sent, reaching "
                       f"**{people}** members in total"),
                inline=False)
            when = biggest.get("created_at")
            stamp = (f"<t:{int(when.replace(tzinfo=datetime.timezone.utc).timestamp())}:R>"
                     if isinstance(when, datetime.datetime) else "recently")
            embed.add_field(
                name="Biggest",
                value=f"cohort `{biggest.get('cohort', '?')}` reached "
                      f"**{biggest.get('reach', 0)}** members, {stamp}",
                inline=False)
            recent = sorted(
                (e for e in events if isinstance(e.get("created_at"), datetime.datetime)),
                key=lambda e: e["created_at"], reverse=True)[:5]
            if recent:
                embed.add_field(
                    name="Recent",
                    value="\n".join(
                        f"`{e.get('cohort', '?')}` reached {e.get('reach', 0)}  "
                        f"<t:{int(e['created_at'].replace(tzinfo=datetime.timezone.utc).timestamp())}:R>"
                        for e in recent),
                    inline=False)
        elif enabled:
            embed.add_field(
                name=f"Last {SUMMARY_DAYS} days",
                value="No nudges yet. They go out around midday, to whoever joined 8 days "
                      "earlier. Use `/forcesurvey days:0` to send one now and see this work.",
                inline=False)

        embed.set_footer(text=f"Logged nudges are kept for {EVENT_TTL_DAYS} days")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /trackpingoff ────────────────────────────────────────────────
    @app_commands.command(name="trackpingoff", description="Stop logging survey nudges")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def trackpingoff(self, interaction: discord.Interaction):
        await GuildConfig.update(self.bot, interaction.guild.id, {"pinglog_enabled": False})
        await interaction.response.send_message(
            "Nudge tracking is off. The nudges already logged are still there, and "
            "`/trackping` will still show the summary.", ephemeral=True)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        cfg = await self._get_config(channel.guild.id)
        if cfg and cfg.get("pinglog_channel") == channel.id:
            await GuildConfig.update(self.bot, channel.guild.id, {"pinglog_enabled": False})


async def setup(bot: commands.Bot):
    await bot.add_cog(PingTracker(bot))
    print("PingTracker cog loaded ✓")
