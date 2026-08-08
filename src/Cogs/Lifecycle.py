"""What happens when the bot joins a server, and what happens to the data when it leaves.

Joining used to be silent. Somebody adds the bot, nothing happens, and unless they already know
which commands exist it sits there doing nothing. A short message naming the two or three
commands that matter is the difference between a configured server and a forgotten one.

Leaving used to leave everything behind. Settings, membership history, ratings, moderation
cases and role panels all stayed in Mongo forever after the bot was removed, which is both a
bill that only grows and not what somebody expects when they kick a bot out.

Deletion waits rather than happening on the spot. Bots get removed by accident, or kicked and
re-added while somebody sorts out permissions, and wiping a server's entire history the instant
that happens would be its own kind of bug. The guild is written down as departed, and if the
bot is added back within the grace period the note is torn up and nothing was lost.
"""

import asyncio
import datetime
import os
from typing import Optional

import discord
from discord.ext import commands, tasks

import Database

# How long a departed server's data is kept before it is deleted. Long enough to cover an
# accidental kick or a permissions fix, short enough that it isn't kept indefinitely.
GRACE_DAYS = 30
SWEEP_HOURS = 6

# Every collection holding per-guild data, and how a guild is identified in it. Anything added
# here later needs a line here too, which is what the test in tests/test_lifecycle.py checks.
BY_GUILD_ID = [
    "servers",        # all the per-server settings
    "memberships",    # join/leave spells behind /retention
    "roles",          # cohort roles for the survey reminders
    "ratings",        # one score per member
    "mod_cases",      # numbered moderation cases
    "role_panels",    # self-serve role messages
    "ping_events",    # the survey reminder log
]
# These two key on the guild id itself rather than a guild_id field.
BY_ID = ["config_dirty"]
COUNTER_PREFIX = "case:"

COLOR = 0x3DDC97


class Lifecycle(commands.Cog, name="Lifecycle"):
    """Greeting a new server, and clearing up after one that's gone."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(lambda: fn(*args, **kwargs))

    @property
    def _db(self):
        return Database.get_bot_database(self.bot.MongoClient)

    @property
    def departed(self):
        return self._db["departed_guilds"]

    async def cog_load(self):
        try:
            await self._run(
                self.departed.create_index, [("at", 1)], name="departed_at")
        except Exception as e:
            print(f"[Lifecycle] index setup failed: {e}")
        self.sweep.start()

    async def cog_unload(self):
        self.sweep.cancel()

    # ── joining ──────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        # Added back within the grace period, so the data was never deleted and the note that
        # said to delete it is now wrong.
        try:
            await self._run(self.departed.delete_one, {"_id": guild.id})
        except Exception as e:
            print(f"[Lifecycle] couldn't clear the departure note for {guild.id}: {e}")

        await self._say_hello(guild)

    @staticmethod
    def _postable(guild: discord.Guild) -> Optional[discord.TextChannel]:
        """Somewhere the bot can actually introduce itself.

        Discord gives no guaranteed channel, so this tries the one the server nominated for
        system messages and then falls back to the first it can post in.
        """
        def usable(channel):
            if channel is None:
                return False
            perms = channel.permissions_for(guild.me)
            return perms.view_channel and perms.send_messages and perms.embed_links

        if usable(guild.system_channel):
            return guild.system_channel
        for channel in guild.text_channels:
            if usable(channel):
                return channel
        return None

    def _hello(self, guild: discord.Guild) -> discord.Embed:
        embed = discord.Embed(
            title=f"Thanks for adding {self.bot.user.name}",
            color=COLOR,
            description=(
                "Nothing is switched on yet. Everything below is optional and off until you "
                "turn it on."),
        )
        embed.add_field(
            name="Worth doing first",
            value=("**`/setchannel`** in the channel you want the ratings survey in. That's "
                   "the main feature: new members get a quiet reminder about a week in, come "
                   "back to rate the server, and `/retention` shows you how many stayed."),
            inline=False)
        embed.add_field(
            name="If you want a server log",
            value="**`/logging setup`** builds the log channels and switches it all on in one "
                  "go. It's hidden from everybody but staff.",
            inline=False)
        embed.add_field(
            name="Everything else",
            value="**`/help`** lists every command, grouped by what it does. Welcome messages, "
                  "autorole, role buttons, moderation with numbered cases and more.",
            inline=False)

        url = (os.environ.get("DASHBOARD_URL") or "").strip().rstrip("/")
        if url:
            embed.add_field(
                name="Or use the dashboard",
                value=f"Most of it is easier to set up at {url}, with your Discord login.",
                inline=False)

        embed.set_footer(text="Only people with Manage Server can change any of this.")
        return embed

    async def _say_hello(self, guild: discord.Guild):
        embed = self._hello(guild)
        channel = self._postable(guild)
        if channel is not None:
            try:
                await channel.send(embed=embed)
                return
            except (discord.Forbidden, discord.HTTPException) as e:
                print(f"[Lifecycle] couldn't post the greeting in {guild.id}: {e}")

        # No channel it can post in. The owner is the one who added it, so tell them directly
        # rather than joining in silence.
        owner = guild.owner
        if owner is None:
            return
        try:
            await owner.send(
                content=f"I couldn't find a channel I'm allowed to post in on **{guild.name}**, "
                        f"so here's the welcome instead.",
                embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass          # plenty of people have DMs closed

    # ── leaving ──────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        when = datetime.datetime.now(datetime.timezone.utc)
        try:
            await self._run(
                self.departed.update_one,
                {"_id": guild.id},
                {"$set": {"at": when, "name": guild.name}},
                True)
            print(f"[Lifecycle] {guild.id} left, data scheduled for deletion in "
                  f"{GRACE_DAYS} days")
        except Exception as e:
            print(f"[Lifecycle] couldn't schedule deletion for {guild.id}: {e}")

    async def forget(self, guild_id: int) -> dict:
        """Delete everything belonging to one guild. Returns what went, per collection."""
        def wipe():
            removed = {}
            for name in BY_GUILD_ID:
                try:
                    removed[name] = self._db[name].delete_many(
                        {"guild_id": guild_id}).deleted_count
                except Exception as e:
                    print(f"[Lifecycle] couldn't clear {name} for {guild_id}: {e}")
            for name in BY_ID:
                try:
                    removed[name] = self._db[name].delete_many(
                        {"_id": guild_id}).deleted_count
                except Exception as e:
                    print(f"[Lifecycle] couldn't clear {name} for {guild_id}: {e}")
            try:
                # The case counter is keyed by a string, not a guild_id field.
                removed["counters"] = self._db["counters"].delete_many(
                    {"_id": f"{COUNTER_PREFIX}{guild_id}"}).deleted_count
            except Exception as e:
                print(f"[Lifecycle] couldn't clear the case counter for {guild_id}: {e}")
            return removed

        removed = await self._run(wipe)
        try:
            await self._run(self.departed.delete_one, {"_id": guild_id})
        except Exception as e:
            print(f"[Lifecycle] couldn't clear the departure note for {guild_id}: {e}")

        total = sum(removed.values())
        detail = ", ".join(f"{k} {v}" for k, v in removed.items() if v)
        print(f"[Lifecycle] forgot {guild_id}: {total} documents"
              + (f" ({detail})" if detail else ""))
        return removed

    @tasks.loop(hours=SWEEP_HOURS)
    async def sweep(self):
        """Delete the data of guilds whose grace period has run out."""
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=GRACE_DAYS))
        try:
            due = await self._run(lambda: list(
                self.departed.find({"at": {"$lte": cutoff}}).limit(50)))
        except Exception as e:
            print(f"[Lifecycle] couldn't look for expired guilds: {e}")
            return

        for record in due:
            guild_id = record.get("_id")
            # Belt and braces: if the bot is somehow back in the guild, the note is stale and
            # deleting a live server's settings would be the worst possible outcome.
            if self.bot.get_guild(guild_id) is not None:
                print(f"[Lifecycle] {guild_id} is back, cancelling its deletion")
                await self._run(self.departed.delete_one, {"_id": guild_id})
                continue
            await self.forget(guild_id)

    @sweep.before_loop
    async def before_sweep(self):
        # Waiting for the cache means get_guild above can be trusted.
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Lifecycle(bot))
    print("Lifecycle cog loaded ✓")
