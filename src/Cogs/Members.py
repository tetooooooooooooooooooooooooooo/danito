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
MAX_COHORT_ROWS = 8

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

    # ── /retention ───────────────────────────────────────────────────
    @app_commands.command(
        name="retention",
        description="How many new members stick around, and how each joining group is doing")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.cooldown(1, 30.0)
    @app_commands.guild_only()
    async def retention(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        now = datetime.datetime.now(datetime.timezone.utc)

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

        cutoff = now - datetime.timedelta(days=WINDOW_DAYS)
        joined_recently = [s for s in spells if _aware(s["joined_at"]) >= cutoff]
        left_recently = [s for s in spells if s.get("left_at")
                         and _aware(s["left_at"]) >= cutoff]
        still_here = sum(1 for s in spells if s.get("left_at") is None)

        embed.description = (
            f"**{still_here}** of the **{len(spells)}** members I've seen join are still here."
        )
        embed.add_field(
            name=f"Last {WINDOW_DAYS} days",
            value=(f"Joined **{len(joined_recently)}**  ·  Left **{len(left_recently)}**  ·  "
                   f"Net **{len(joined_recently) - len(left_recently):+d}**"),
            inline=False)

        survival = self._survival(spells, now)
        rows = []
        for days in RETENTION_DAYS:
            result = survival[days]
            if result is None:
                rows.append(f"`{days:>2}d`  not enough history yet")
            else:
                kept, of = result
                rows.append(f"`{days:>2}d`  **{_pct(kept, of)}**  ({kept} of {of})")
        embed.add_field(
            name="Still here after",
            value="\n".join(rows),
            inline=False)

        # Per cohort, so a bad day is visible rather than averaged away.
        by_cohort: dict[str, list] = {}
        for s in spells:
            by_cohort.setdefault(s.get("cohort", "?"), []).append(s)

        cohort_rows = []
        for cohort in sorted(by_cohort, reverse=True)[:MAX_COHORT_ROWS]:
            group = by_cohort[cohort]
            here = sum(1 for s in group if s.get("left_at") is None)
            nudged = any(s.get("nudged") for s in group)
            cohort_rows.append(
                f"`{cohort}`  {len(group)} joined, {here} here "
                f"({_pct(here, len(group))}){'  · nudged' if nudged else ''}")
        embed.add_field(
            name="Recent joining groups",
            value="\n".join(cohort_rows) or "none",
            inline=False)

        embed.set_footer(
            text="Each window only counts people who joined long enough ago to measure, so the "
                 "totals differ between rows.")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Members(bot))
    print("Members cog loaded ✓")
