"""Everything that happens when somebody joins or leaves.

This used to be split across two places: main.py handled the welcome DM and the departure
record as Bot methods, while eventcog handled the cohort role as listeners. Both fired for
every join, so each one cost two independent sets of database calls, and the ordering between
them was down to chance. It's one handler now.

The cohort role is the front half of the ratings nudge: everybody who joins on the same day
shares a role named after that date, and Ratings/mention_players pings it 8 days later.
"""

import asyncio
import datetime

import discord
from discord.ext import commands, tasks

import Database

DEPARTURE_RETENTION_DAYS = 30

WELCOME_MESSAGE = (
    "Hey!, {mention}!\n"
    "We'd love to interest you in checking out our partnered social mmo game, Meown!\n\n"
    "🔗 **playable at** https://meown.net\n"
    "🔗 **Discord:** https://discord.gg/VPjxQgTgBh"
)


class Members(commands.Cog, name="Members"):
    """Cohort roles, welcome DMs and departure tracking."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _run(self, fn, *args, **kwargs):
        # pymongo is synchronous. A raid means many joins at once, and doing these inline
        # blocked the event loop long enough to risk the gateway heartbeat.
        return await asyncio.to_thread(lambda: fn(*args, **kwargs))

    @property
    def _db(self):
        return Database.get_bot_database(self.bot.MongoClient)

    async def cog_load(self):
        self.cleanup_departures.start()

    async def cog_unload(self):
        self.cleanup_departures.cancel()

    # ── joining ──────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        await self._assign_cohort_role(member)
        await self._clear_departure(member)
        await self._welcome(member)

    async def _assign_cohort_role(self, member: discord.Member):
        """Give the member the role for today's date, creating it if this is the day's first
        join. Everyone who joins today shares it, which is what makes the nudge target one
        group at a time instead of the whole server."""
        roles = self._db["roles"]
        today = str(datetime.date.today())

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

    async def _clear_departure(self, member: discord.Member):
        try:
            await self._run(
                self._db["departures"].find_one_and_delete,
                {"user_id": member.id, "guild_id": member.guild.id})
        except Exception as e:
            print(f"[Members] couldn't clear the departure record: {e}")

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
            await self._run(self._db["departures"].insert_one, {
                "user_id": member.id,
                "guild_id": member.guild.id,
                "departure_time": datetime.datetime.now(datetime.timezone.utc),
            })
        except Exception as e:
            print(f"[Members] couldn't record departure: {e}")

    @tasks.loop(hours=24)
    async def cleanup_departures(self):
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            days=DEPARTURE_RETENTION_DAYS)
        try:
            result = await self._run(
                self._db["departures"].delete_many, {"departure_time": {"$lt": cutoff}})
            if result.deleted_count:
                print(f"Cleaned up {result.deleted_count} old departure records.")
        except Exception as e:
            print(f"[Members] departure cleanup failed: {e}")

    @cleanup_departures.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Members(bot))
    print("Members cog loaded ✓")
