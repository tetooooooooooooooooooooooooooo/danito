"""Join and leave handling, and the retention data that comes out of it.

One handler for both events. This used to be split between main.py (welcome DM, departure
record) and eventcog (cohort role), so every join was processed twice with two independent
sets of database calls.

Retention is recorded as membership *spells*: one document per continuous stretch of someone
being in the server, opened on join and closed on leave. Somebody who leaves and comes back has
two spells, which is the honest way to represent it. That shape is what makes "of the people who
joined N days ago, how many were still here N days later" answerable at all.

This replaces the old `departures` collection, which was written on every join and leave and
then deleted unread: nothing ever queried it. Same write cost, except now the data is used.
"""

import asyncio
import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import Database

# Spells are only needed for the retention window, so Mongo expires them rather than growing
# forever on a public bot. Well past the longest bucket below.
SPELL_TTL_DAYS = 180
RETENTION_DAYS = (1, 7, 14, 30)
WINDOW_DAYS = 30
MAX_SPELLS = 5000        # ceiling on documents pulled into one /retention call

# How /retention groups the timeline. Monthly stops at 6 because spells expire after 180 days,
# so there is nothing older to show. (unit, buckets, label format, heading)
PERIODS = {
    "hourly": ("hour", 24, "%H:00", "Last 24 hours, by hour"),
    "daily": ("day", 14, "%d %b", "Last 14 days, by day"),
    "weekly": ("week", 12, "%d %b", "Last 12 weeks, by week"),
    "monthly": ("month", 6, "%b %Y", "Last 6 months, by month"),
}
DEFAULT_PERIOD = "daily"

COLOR_INFO = 0x5865F2
COLOR_WARN = 0xE67E22

WELCOME_MESSAGE = (
    "Hey!, {mention}!\n"
    "We'd love to interest you in checking out our partnered social mmo game, Meown!\n\n"
    "🔗 **playable at** https://meown.net\n"
    "🔗 **Discord:** https://discord.gg/VPjxQgTgBh"
)


def _aware(dt: datetime.datetime) -> datetime.datetime:
    """pymongo returns naive UTC datetimes; comparing one against an aware 'now' raises."""
    return dt.replace(tzinfo=datetime.timezone.utc) if dt.tzinfo is None else dt


def _pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.0f}%" if whole else "n/a"


class Members(commands.Cog, name="Members"):
    """Cohort roles, welcome DMs, and how well the server holds on to new people."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _run(self, fn, *args, **kwargs):
        # pymongo is synchronous. A raid means many joins at once, and doing these inline
        # blocked the event loop long enough to risk the gateway heartbeat.
        return await asyncio.to_thread(lambda: fn(*args, **kwargs))

    @property
    def _db(self):
        return Database.get_bot_database(self.bot.MongoClient)

    @property
    def spells(self):
        return self._db["memberships"]

    async def cog_load(self):
        try:
            await self._run(self._ensure_indexes)
        except Exception as e:
            print(f"[Members] index setup failed: {e}")

    def _ensure_indexes(self):
        self.spells.create_index([("guild_id", 1), ("cohort", 1)], name="guild_cohort")
        # Closing a spell looks up the open one for that member.
        self.spells.create_index([("guild_id", 1), ("user_id", 1), ("left_at", 1)],
                                 name="guild_user_open")
        self.spells.create_index([("guild_id", 1), ("joined_at", -1)], name="guild_joined")
        # No cleanup loop needed: Mongo expires these itself.
        self.spells.create_index("joined_at", expireAfterSeconds=SPELL_TTL_DAYS * 86400,
                                 name="ttl_joined")

    # ── joining ──────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        cohort = str(datetime.date.today())
        await self._open_spell(member, cohort)
        await self._assign_cohort_role(member, cohort)
        await self._welcome(member)

    async def _open_spell(self, member: discord.Member, cohort: str):
        """Record the start of a membership. Their join date doubles as the cohort key, so it
        lines up with the cohort role and with whatever the nudge later targets."""
        try:
            await self._run(self.spells.insert_one, {
                "guild_id": member.guild.id,
                "user_id": member.id,
                "cohort": cohort,
                "joined_at": datetime.datetime.now(datetime.timezone.utc),
                "left_at": None,
                "nudged": False,
            })
        except Exception as e:
            print(f"[Members] couldn't record the join: {e}")

    async def _assign_cohort_role(self, member: discord.Member, today: str):
        """Give the member the role for today's date, creating it if this is the day's first
        join. Everyone who joins today shares it, which is what lets the nudge target one group
        at a time instead of the whole server."""
        roles = self._db["roles"]

        if not member.guild.me.guild_permissions.manage_roles:
            print(f"[Members] no Manage Roles in {member.guild.id}, skipping cohort role")
            return

        try:
            record = await self._run(
                roles.find_one, {"date": today, "guild_id": member.guild.id})
        except Exception as e:
            print(f"[Members] cohort lookup failed: {e}")
            return

        role = member.guild.get_role(record["role_id"]) if record else None

        if role is None:
            try:
                role = await member.guild.create_role(
                    name=today, reason="Ratings cohort for today's joins")
            except discord.Forbidden:
                print(f"[Members] can't create the cohort role in {member.guild.id}")
                return
            except discord.HTTPException as e:
                print(f"[Members] cohort role creation failed: {e}")
                return
            try:
                # $set with upsert rather than replace, so a stale record is repointed at the
                # new role without dropping the "mentioned" flag if one is already there.
                await self._run(
                    roles.update_one,
                    {"date": today, "guild_id": member.guild.id},
                    {"$set": {"role_id": role.id}},
                    True)
            except Exception as e:
                print(f"[Members] couldn't record the cohort role: {e}")

        try:
            await member.add_roles(role, reason="Ratings cohort")
        except discord.Forbidden:
            print(f"[Members] can't assign the cohort role in {member.guild.id}, "
                  f"my role may be too low")
        except discord.HTTPException as e:
            print(f"[Members] add_roles failed: {e}")

    async def _welcome(self, member: discord.Member):
        try:
            await member.send(WELCOME_MESSAGE.format(mention=member.mention))
        except (discord.Forbidden, discord.HTTPException):
            pass          # plenty of people have DMs closed; not worth logging every time

    # ── leaving ──────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.bot:
            return
        try:
            # Close their most recent open spell. If there isn't one the bot wasn't running
            # when they joined, and inventing a join date would poison the numbers.
            await self._run(
                self.spells.find_one_and_update,
                {"guild_id": member.guild.id, "user_id": member.id, "left_at": None},
                {"$set": {"left_at": datetime.datetime.now(datetime.timezone.utc)}},
                sort=[("joined_at", -1)])
        except Exception as e:
            print(f"[Members] couldn't record the departure: {e}")

    # ── the maths ────────────────────────────────────────────────────
    @staticmethod
    def _survival(spells: list, now: datetime.datetime) -> dict:
        """For each window, how many of the people old enough to be measured lasted that long.

        The denominator differs per window on purpose: only somebody who joined at least 30
        days ago can tell you anything about 30-day retention. Counting recent joins as
        "survived" would flatter every number.
        """
        out = {}
        for days in RETENTION_DAYS:
            eligible = [s for s in spells
                        if (now - _aware(s["joined_at"])).days >= days]
            if not eligible:
                out[days] = None
                continue
            survived = 0
            for s in eligible:
                left = s.get("left_at")
                if left is None:
                    survived += 1                      # still here
                elif (_aware(left) - _aware(s["joined_at"])).days >= days:
                    survived += 1                      # left, but lasted the window
            out[days] = (survived, len(eligible))
        return out

    @staticmethod
    def _bucket_start(dt: datetime.datetime, unit: str) -> datetime.datetime:
        """Truncate a timestamp down to the start of its bucket."""
        dt = _aware(dt)
        if unit == "hour":
            return dt.replace(minute=0, second=0, microsecond=0)
        midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        if unit == "day":
            return midnight
        if unit == "week":
            return midnight - datetime.timedelta(days=midnight.weekday())   # back to Monday
        if unit == "month":
            return midnight.replace(day=1)
        raise ValueError(unit)

    @classmethod
    def _buckets(cls, now: datetime.datetime, unit: str, count: int) -> list:
        """The `count` most recent bucket starts, oldest first."""
        cursor = cls._bucket_start(now, unit)
        out = [cursor]
        for _ in range(count - 1):
            if unit == "hour":
                cursor -= datetime.timedelta(hours=1)
            elif unit == "day":
                cursor -= datetime.timedelta(days=1)
            elif unit == "week":
                cursor -= datetime.timedelta(weeks=1)
            else:
                # Step back into the previous month, whatever length it was.
                cursor = (cursor - datetime.timedelta(days=1)).replace(day=1)
            out.append(cursor)
        return list(reversed(out))

    @classmethod
    def _timeline(cls, spells: list, now: datetime.datetime, period: str) -> list:
        """Joins, leaves and how many of each intake are still around, per bucket."""
        unit, count, _, _ = PERIODS[period]
        buckets = cls._buckets(now, unit, count)
        index = {b: {"joined": 0, "left": 0, "still": 0, "nudged": 0} for b in buckets}
        oldest = buckets[0]

        for s in spells:
            joined = _aware(s["joined_at"])
            if joined >= oldest:
                key = cls._bucket_start(joined, unit)
                if key in index:
                    index[key]["joined"] += 1
                    if s.get("left_at") is None:
                        index[key]["still"] += 1
                    if s.get("nudged"):
                        # Counted rather than flagged: a weekly or monthly bucket spans several
                        # cohorts, so some of its intake may have been nudged and some not.
                        index[key]["nudged"] += 1
            left = s.get("left_at")
            if left is not None:
                left = _aware(left)
                if left >= oldest:
                    key = cls._bucket_start(left, unit)
                    if key in index:
                        index[key]["left"] += 1

        return [(b, index[b]) for b in buckets]

    # ── /retention ───────────────────────────────────────────────────
    @app_commands.command(
        name="retention",
        description="How many new members stick around, grouped however you like")
    @app_commands.describe(period="How to group the timeline. Defaults to daily.")
    @app_commands.choices(period=[
        app_commands.Choice(name="Hourly (last 24 hours)", value="hourly"),
        app_commands.Choice(name="Daily (last 14 days)", value="daily"),
        app_commands.Choice(name="Weekly (last 12 weeks)", value="weekly"),
        app_commands.Choice(name="Monthly (last 6 months)", value="monthly"),
    ])
    @app_commands.checks.cooldown(1, 30.0)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def retention(self, interaction: discord.Interaction,
                        period: Optional[app_commands.Choice[str]] = None):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        now = datetime.datetime.now(datetime.timezone.utc)
        chosen = period.value if period else DEFAULT_PERIOD
        unit, count, fmt, heading = PERIODS[chosen]

        try:
            spells = await self._run(lambda: list(
                self.spells.find({"guild_id": guild.id})
                .sort("joined_at", -1).limit(MAX_SPELLS)))
        except Exception as e:
            await interaction.followup.send(f"Couldn't read the data: {e}", ephemeral=True)
            return

        embed = discord.Embed(title=f"Retention for {guild.name}", color=COLOR_INFO,
                              timestamp=now)
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        if not spells:
            embed.description = (
                "Nothing recorded yet.\n\n"
                "Joins and leaves are tracked from now on, so this fills in as people come and "
                "go. The 7 day figure needs a week of data, the 30 day figure needs a month.")
            embed.color = COLOR_WARN
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        still_here = sum(1 for s in spells if s.get("left_at") is None)
        embed.description = (
            f"**{still_here}** of the **{len(spells)}** members I've seen join are still here."
        )

        # ── the timeline, at the chosen granularity ──
        timeline = self._timeline(spells, now, chosen)
        rows = []
        for bucket, tally in timeline:
            if not (tally["joined"] or tally["left"]):
                continue                     # skip quiet buckets rather than pad the list
            label = bucket.strftime(fmt)
            if unit == "week":
                label = f"w/c {label}"
            piece = f"`{label}`  +{tally['joined']} / -{tally['left']}"
            if tally["joined"]:
                piece += f"  ·  {tally['still']} here ({_pct(tally['still'], tally['joined'])})"
            if tally["nudged"] == tally["joined"] and tally["joined"]:
                piece += "  · nudged"
            elif tally["nudged"]:
                piece += f"  · {tally['nudged']} nudged"
            rows.append(piece)

        if rows:
            # A 24-row hourly view overflows a single 1024-char field.
            chunks, current = [], ""
            for row in rows:
                if len(current) + len(row) + 1 > 1024:
                    chunks.append(current)
                    current = ""
                current += row + "\n"
            if current:
                chunks.append(current)
            for i, chunk in enumerate(chunks[:5]):
                embed.add_field(
                    name=heading if i == 0 else f"{heading} (cont.)",
                    value=chunk, inline=False)
        else:
            embed.add_field(
                name=heading,
                value="Nothing happened in that window. Try a wider one.", inline=False)

        totals_joined = sum(t["joined"] for _, t in timeline)
        totals_left = sum(t["left"] for _, t in timeline)
        embed.add_field(
            name="That window in total",
            value=(f"Joined **{totals_joined}**  ·  Left **{totals_left}**  ·  "
                   f"Net **{totals_joined - totals_left:+d}**"),
            inline=False)

        # ── survival, which is about tenure and so unaffected by the grouping above ──
        survival = self._survival(spells, now)
        surv_rows = []
        for days in RETENTION_DAYS:
            result = survival[days]
            if result is None:
                surv_rows.append(f"`{days:>2}d`  not enough history yet")
            else:
                kept, of = result
                surv_rows.append(f"`{days:>2}d`  **{_pct(kept, of)}**  ({kept} of {of})")
        embed.add_field(
            name="Still here after (all time, not just the window)",
            value="\n".join(surv_rows),
            inline=False)

        notes = ["Each survival row counts only members who joined long enough ago to measure, "
                 "so those totals differ from each other."]
        if len(spells) >= MAX_SPELLS:
            notes.append(f"Based on the most recent {MAX_SPELLS} joins.")
        if chosen == "monthly":
            notes.append(f"Records expire after {SPELL_TTL_DAYS} days.")
        embed.set_footer(text="  ".join(notes))
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Members(bot))
    print("Members cog loaded ✓")
