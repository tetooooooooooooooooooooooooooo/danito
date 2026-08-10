"""Logs the survey reminders this bot sends, and how many people each one reached.

The reminder is a bare role mention posted into the ratings channel by main.mention_players, then
deleted two seconds later. That makes it invisible after the fact: you can't tell whether it
fired, or how many people it woke up. This records each one before it vanishes.

"Reach" is the number of distinct members in the cohort role, which is the number that actually
matters and one Discord shows you nowhere. The role is named after the date its members joined,
so the log doubles as a record of which cohort was reminded when.

Deliberately narrow: only this bot's own reminders, only in the configured ratings channel, and
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
from Brand import MINT

EVENT_TTL_DAYS = 30
SUMMARY_DAYS = 7

COLOR_PING = 0xF39C12
COLOR_BIG = 0xE74C3C
COLOR_WARN = 0xE67E22

BIG_PING = 50          # coloured red above this, purely cosmetic

# The reminder is exactly this and nothing else, which is what makes it safely identifiable.
SURVEY_PING = re.compile(r"^<@&(\d+)>$")


class PingTracker(commands.Cog, name="Ping Tracking"):
    """Records each survey reminder and the number of members it reached."""

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

    def _is_survey_reminder(self, message: discord.Message, cfg: dict) -> bool:
        """A reminder is: sent by this bot, in the configured ratings channel, and consisting of
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
        if cfg is None or not self._is_survey_reminder(message, cfg):
            return

        role = message.role_mentions[0]
        reach = len(role.members)

        channel = message.guild.get_channel(cfg["pinglog_channel"])
        if channel is None:
            return

        sent_at = int(message.created_at.timestamp())
        embed = discord.Embed(
            title="Survey reminder sent",
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
                value="Nobody is in that cohort role, so this reminder reached no one.",
                inline=False)
        embed.set_footer(text="The reminder itself deletes after 2 seconds")

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

    # Switching this on and off lives in the Logging cog, under /logging reminders,
    # with the other three logs. Recording the reminders is still this cog's job.

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        cfg = await self._get_config(channel.guild.id)
        if cfg and cfg.get("pinglog_channel") == channel.id:
            await GuildConfig.update(self.bot, channel.guild.id, {"pinglog_enabled": False})


async def setup(bot: commands.Bot):
    await bot.add_cog(PingTracker(bot))
    print("PingTracker cog loaded ✓")
