"""Reminders, delivered by direct message.

The whole feature is a loop over one indexed query, so the only decisions worth writing down
are about what happens when things go wrong.

- Delivery is a DM, falling back to the channel it was set in. Somebody with DMs closed still
  gets their reminder rather than silently never hearing about it.
- A reminder is deleted the moment it is claimed by the loop, before it is sent, not after. A
  restart mid-send loses one reminder; deleting after sending would resend every reminder in
  flight on every restart, which is worse and much more annoying.
- They belong to the server they were set in, so removing the bot takes them with it. The
  channel they name would be gone anyway.
"""

import asyncio
import datetime
import re

import discord
from discord import app_commands
from discord.ext import commands, tasks

import Database

COLOR = 0x3DDC97

CHECK_SECONDS = 30
MAX_PENDING = 25            # per person, so nobody can queue a thousand
MAX_TEXT = 400
MIN_DELAY = 30              # seconds. Anything shorter is a stopwatch, not a reminder.
MAX_DELAY = 365 * 86400     # a year, which is well past anything anybody means

# "10m", "2h30m", "1d 12h", "45s". Written out rather than parsed as a date because nobody
# types a date, they type how long from now.
UNITS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}
DURATION = re.compile(r"(\d+)\s*([wdhms])", re.IGNORECASE)


def parse_delay(text: str):
    """Seconds from now, or None if it doesn't read as a length of time.

    Deliberately strict about leftovers: "2h and a bit" is a request nobody can honour, and
    quietly hearing "2h" would set a reminder they didn't ask for.
    """
    if not text:
        return None
    found = DURATION.findall(text)
    if not found:
        return None
    if DURATION.sub("", text).replace(",", " ").replace("and", " ").strip():
        return None
    total = sum(int(amount) * UNITS[unit.lower()] for amount, unit in found)
    return total if MIN_DELAY <= total <= MAX_DELAY else None


class Reminders(commands.Cog, name="Reminders"):
    """Ask to be told about something later."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(lambda: fn(*args, **kwargs))

    @property
    def _db(self):
        return Database.get_bot_database(self.bot.MongoClient)

    @property
    def store(self):
        return self._db["reminders"]

    async def cog_load(self):
        try:
            await self._run(self._ensure_indexes)
        except Exception as e:
            print(f"[Reminders] index setup failed: {e}")
        self.deliver.start()

    async def cog_unload(self):
        self.deliver.cancel()

    def _ensure_indexes(self):
        # The loop's only query, run twice a minute forever.
        self.store.create_index([("due", 1)], name="due")
        self.store.create_index([("user_id", 1), ("due", 1)], name="user_due")
        self.store.create_index([("guild_id", 1)], name="guild")

    # ── setting one ──────────────────────────────────────────────────
    @app_commands.command(name="remindme", description="Be told about something later")
    @app_commands.describe(when="How long from now, like 10m or 2h30m or 3d",
                           what="What to remind you about")
    @app_commands.checks.cooldown(5, 60.0)
    @app_commands.guild_only()
    async def remindme(self, interaction: discord.Interaction, when: str, what: str):
        seconds = parse_delay(when)
        if seconds is None:
            await interaction.response.send_message(
                f"I couldn't read `{when[:60]}` as a length of time. Try something like "
                f"`10m`, `2h30m` or `3d`. Nothing under {MIN_DELAY} seconds or over a year.",
                ephemeral=True)
            return

        text = (what or "").strip()[:MAX_TEXT]
        if not text:
            await interaction.response.send_message(
                "Remind you about what?", ephemeral=True)
            return

        pending = await self._run(self.store.count_documents,
                                  {"user_id": interaction.user.id})
        if pending >= MAX_PENDING:
            await interaction.response.send_message(
                f"You already have {pending} reminders waiting, which is the limit. "
                f"`/reminders` shows them, and can cancel one.", ephemeral=True)
            return

        due = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            seconds=seconds)
        try:
            await self._run(self.store.insert_one, {
                "user_id": interaction.user.id,
                "guild_id": interaction.guild.id,
                "channel_id": interaction.channel_id,
                "text": text,
                "due": due,
                "set_at": datetime.datetime.now(datetime.timezone.utc),
            })
        except Exception as e:
            print(f"[Reminders] couldn't save: {e}")
            await interaction.response.send_message(
                "Something went wrong saving that. Try again in a moment.", ephemeral=True)
            return

        embed = discord.Embed(
            colour=COLOR,
            description=f"⏰ <t:{int(due.timestamp())}:R>, I'll remind you:\n> {text}")
        embed.set_footer(text="By direct message, or here if your DMs are closed.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── seeing and cancelling ────────────────────────────────────────
    @app_commands.command(name="reminders", description="What you've asked to be reminded of")
    @app_commands.describe(cancel="The number of one to call off, from the list")
    @app_commands.checks.cooldown(5, 60.0)
    @app_commands.guild_only()
    async def reminders(self, interaction: discord.Interaction, cancel: int = None):
        mine = await self._run(
            lambda: list(self.store.find({"user_id": interaction.user.id})
                         .sort("due", 1).limit(MAX_PENDING)))

        if cancel is not None:
            if not 1 <= cancel <= len(mine):
                await interaction.response.send_message(
                    f"There's no reminder {cancel}. You have {len(mine)}.", ephemeral=True)
                return
            doomed = mine[cancel - 1]
            await self._run(self.store.delete_one, {"_id": doomed["_id"]})
            await interaction.response.send_message(
                f"Called off: {doomed['text'][:100]}", ephemeral=True)
            return

        if not mine:
            await interaction.response.send_message(
                "Nothing waiting. `/remindme` sets one.", ephemeral=True)
            return

        # Numbered, because the numbers are what /reminders cancel takes. Positions rather
        # than ids: nobody is going to type an ObjectId.
        lines = []
        for n, item in enumerate(mine, 1):
            due = item["due"]
            if due.tzinfo is None:
                due = due.replace(tzinfo=datetime.timezone.utc)
            lines.append(f"**{n}.** <t:{int(due.timestamp())}:R> · {item['text'][:120]}")

        embed = discord.Embed(colour=COLOR, title="⏰ Waiting for you",
                              description="\n".join(lines))
        embed.set_footer(text="/reminders cancel:2 calls one off.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── delivering them ──────────────────────────────────────────────
    @tasks.loop(seconds=CHECK_SECONDS)
    async def deliver(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        try:
            due = await self._run(
                lambda: list(self.store.find({"due": {"$lte": now}}).limit(100)))
        except Exception as e:
            print(f"[Reminders] couldn't read the queue: {e}")
            return

        for item in due:
            # Claimed before it is sent. A crash between the two loses one reminder; deleting
            # afterwards would resend everything in flight on the next restart instead.
            try:
                claimed = await self._run(self.store.find_one_and_delete, {"_id": item["_id"]})
            except Exception as e:
                print(f"[Reminders] couldn't claim one: {e}")
                continue
            if not claimed:
                continue                    # another worker got there first
            await self._send(claimed)

    async def _send(self, item: dict):
        user = self.bot.get_user(item["user_id"])
        if user is None:
            try:
                user = await self.bot.fetch_user(item["user_id"])
            except discord.HTTPException:
                return

        set_at = item.get("set_at")
        when = (f" · asked <t:{int(set_at.timestamp())}:R>"
                if isinstance(set_at, datetime.datetime) else "")
        embed = discord.Embed(colour=COLOR, title="⏰ You asked to be reminded",
                              description=item["text"])
        guild = self.bot.get_guild(item.get("guild_id"))
        embed.set_footer(text=f"From {guild.name}{when}" if guild else when.strip(" ·"))

        try:
            await user.send(embed=embed)
            return
        except discord.HTTPException:
            pass                            # DMs closed, so try where they set it

        channel = self.bot.get_channel(item.get("channel_id"))
        if channel is None:
            return
        try:
            await channel.send(content=user.mention, embed=embed)
        except discord.HTTPException as e:
            print(f"[Reminders] couldn't deliver to {item['user_id']}: {e}")

    @deliver.before_loop
    async def before_deliver(self):
        # get_user and get_channel below are only worth anything once the cache exists.
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Reminders(bot))
    print("✓ Reminders cog loaded")
