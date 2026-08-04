"""Server ratings, plus the nudges that get people to leave one.

The loop, spread across three places:

1. eventcog.on_member_join gives every joining member a role named after that day's date, so
   everyone who joined the same day shares one "cohort" role.
2. main.mention_players (driven by the midday loop in main.py) finds the cohort that joined
   8 days ago, pings its role once in the ratings channel, and deletes the ping 2s later.
3. On day 9 the same routine deletes the role and its record so cohorts don't pile up.

Ratings themselves are captured by on_interaction below and stored one per member per guild,
upserted so somebody changing their mind replaces their old score instead of stuffing the
ballot.
"""

import asyncio
import datetime
import re

import discord
from discord import app_commands
from discord.ext import commands

import Database

PING_AFTER_DAYS = 8
CLEANUP_AFTER_DAYS = 9
SCALE = 10

COLOR_INFO = 0x5865F2
COLOR_GOOD = 0x2ECC71
COLOR_WARN = 0xE67E22

# Explicit custom_ids are what make the score recoverable. Without them Discord generates
# random ones and there is no way to tell a click on "3" from a click on "9".
RATING_ID = re.compile(r"^rating:(\d{1,2})$")


def bar(count: int, biggest: int, width: int = 12) -> str:
    if biggest <= 0:
        return ""
    filled = round(count / biggest * width)
    return "█" * filled + "░" * (width - filled)


class ServerRatings(commands.Cog, name="Server Ratings"):
    """Collects member ratings of your server and nudges newcomers to leave one."""

    def __init__(self, client: commands.Bot):
        self.Client = client
        self.bot = client

    async def _run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(lambda: fn(*args, **kwargs))

    @property
    def _db(self):
        return Database.get_bot_database(self.Client.MongoClient)

    async def cog_load(self):
        try:
            await self._run(self._ensure_indexes)
        except Exception as e:
            print(f"[Ratings] index setup failed: {e}")

    def _ensure_indexes(self):
        # One rating per member per server, so a rating can be updated but not duplicated.
        self._db["ratings"].create_index(
            [("guild_id", 1), ("user_id", 1)], unique=True, name="one_per_member")
        self._db["ratings"].create_index([("guild_id", 1), ("rating", 1)], name="guild_rating")

    # ── capturing a click ────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        if not interaction.message or interaction.guild is None:
            return

        try:
            server_data = await self._run(self._db["servers"].find_one, {
                "guild_id": interaction.guild.id,
                "discovery_channel": interaction.channel.id,
                "discovery_message": interaction.message.id,
            })
        except Exception as e:
            print(f"[Ratings] lookup failed: {e}")
            return
        if not server_data:
            return

        custom_id = (interaction.data or {}).get("custom_id", "")
        match = RATING_ID.match(custom_id)
        if not match:
            # A survey posted before ratings were saved. Its buttons carry Discord's random
            # ids, so the score can't be read back.
            await interaction.response.send_message(
                "Thanks for rating! This survey was posted by an older version of the bot, "
                "so your score can't be recorded. An admin can run `/setchannel` again to "
                "post a fresh one.",
                ephemeral=True)
            return

        score = int(match.group(1))
        if not 1 <= score <= SCALE:
            return

        now = datetime.datetime.now(datetime.timezone.utc)
        try:
            existing = await self._run(self._db["ratings"].find_one, {
                "guild_id": interaction.guild.id, "user_id": interaction.user.id})
            # Read the old score out now, as a plain int. Keeping the document around and
            # reading it after the write would depend on find_one having handed back a
            # detached copy.
            previous = existing.get("rating") if existing else None
            await self._run(
                self._db["ratings"].update_one,
                {"guild_id": interaction.guild.id, "user_id": interaction.user.id},
                {"$set": {"rating": score, "updated_at": now},
                 "$setOnInsert": {"created_at": now}},
                upsert=True)
        except Exception as e:
            print(f"[Ratings] save failed: {e}")
            await interaction.response.send_message(
                "Something went wrong saving your rating. Try again in a moment.",
                ephemeral=True)
            return

        if previous is not None and previous != score:
            text = (f"Updated your rating to **{score}/{SCALE}** "
                    f"(was {previous}). Thanks!")
        elif previous is not None:
            text = f"You'd already given **{score}/{SCALE}**. Thanks again!"
        else:
            text = f"Thanks for rating the server **{score}/{SCALE}**! Enjoy your stay."
        await interaction.response.send_message(text, ephemeral=True)

    # ── /setchannel ──────────────────────────────────────────────────
    @app_commands.command(
        name="setchannel", description="Post the rating survey here and use this channel for nudges"
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def set_discovery_channel(self, interaction: discord.Interaction):
        # timeout=None because these buttons need to keep working after the bot restarts.
        # The click is handled by on_interaction above, not by this View object.
        view = discord.ui.View(timeout=None)
        for i in range(1, SCALE + 1):
            view.add_item(discord.ui.Button(
                label=str(i), style=discord.ButtonStyle.blurple, custom_id=f"rating:{i}"))

        embed = discord.Embed(
            title="How's your experience been so far?",
            description=f"Tap a number from 1 to {SCALE}. It only takes a second, and it's "
                        f"only visible to the server staff.",
            color=COLOR_INFO,
        )
        message = await interaction.channel.send(embed=embed, view=view)

        # $set rather than replacing the document, so the media log and mod log settings on
        # this same doc survive.
        await self._run(
            self._db["servers"].update_one,
            {"guild_id": interaction.guild.id},
            {"$set": {"discovery_channel": interaction.channel.id,
                      "discovery_message": message.id}},
            upsert=True,
        )
        await interaction.response.send_message(
            f"Survey posted in {interaction.channel.mention}. Members who joined "
            f"{PING_AFTER_DAYS} days ago will get a quiet nudge to come and rate the server.\n"
            f"Use `/ratings` to see the scores, or `/discoveryhelp` for how the whole thing works.",
            ephemeral=True,
        )

    # ── /ratings ─────────────────────────────────────────────────────
    @app_commands.command(name="ratings", description="See how members have rated this server")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def ratings(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        try:
            rows = await self._run(
                lambda: list(self._db["ratings"].find({"guild_id": guild.id})))
            server_data = await self._run(
                self._db["servers"].find_one, {"guild_id": guild.id}) or {}
        except Exception as e:
            await interaction.followup.send(
                f"Couldn't read the ratings: {e}", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"Server ratings for {guild.name}",
            color=COLOR_INFO,
            timestamp=discord.utils.utcnow(),
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        if not rows:
            configured = bool(server_data.get("discovery_channel"))
            embed.description = (
                "No ratings yet.\n\n"
                + ("The survey is up, so this will fill in as people vote. Members who joined "
                   f"{PING_AFTER_DAYS} days ago get nudged to come and rate."
                   if configured else
                   "Run `/setchannel` in the channel where you want the survey to live.")
            )
            embed.color = COLOR_WARN
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        scores = [r["rating"] for r in rows if isinstance(r.get("rating"), int)]
        total = len(scores)
        average = sum(scores) / total if total else 0
        counts = {n: scores.count(n) for n in range(1, SCALE + 1)}
        biggest = max(counts.values()) if counts else 0

        promoters = sum(c for n, c in counts.items() if n >= 9)
        detractors = sum(c for n, c in counts.items() if n <= 6)
        nps = round((promoters - detractors) / total * 100) if total else 0

        stars = round(average / 2)
        embed.description = (
            f"**{average:.1f} / {SCALE}** from **{total}** "
            f"{'rating' if total == 1 else 'ratings'}\n"
            f"{'⭐' * stars}{'▫️' * (5 - stars)}"
        )

        lines = [
            f"`{n:>2}` {bar(counts[n], biggest)} `{counts[n]}`"
            for n in range(SCALE, 0, -1)
        ]
        embed.add_field(name="Breakdown", value="\n".join(lines), inline=False)

        embed.add_field(
            name="Happy (9 to 10)", value=f"{promoters} ({promoters / total * 100:.0f}%)",
            inline=True)
        embed.add_field(
            name="Unhappy (1 to 6)", value=f"{detractors} ({detractors / total * 100:.0f}%)",
            inline=True)
        embed.add_field(name="Net score", value=f"{nps:+d}", inline=True)

        recent = sorted(
            (r for r in rows if isinstance(r.get("updated_at"), datetime.datetime)),
            key=lambda r: r["updated_at"], reverse=True)[:5]
        if recent:
            embed.add_field(
                name="Latest",
                value="\n".join(
                    f"<@{r['user_id']}> gave **{r['rating']}**  "
                    f"<t:{int(r['updated_at'].replace(tzinfo=datetime.timezone.utc).timestamp())}:R>"
                    for r in recent),
                inline=False)

        embed.set_footer(text="Net score is the share of 9s and 10s minus the share of 1s to 6s.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /forcesurvey ─────────────────────────────────────────────────
    @app_commands.command(
        name="forcesurvey", description="Send today's nudge now instead of waiting for midday"
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def force_survey(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.Client.mention_players()
        except Exception as e:
            await interaction.followup.send(f"The nudge failed: {e}", ephemeral=True)
            return
        await interaction.followup.send(
            f"Done. Anyone who joined {PING_AFTER_DAYS} days ago has been nudged. Groups that "
            f"were already nudged get skipped, so running this twice won't spam anybody.",
            ephemeral=True,
        )

    # ── /discoveryhelp ───────────────────────────────────────────────
    @app_commands.command(
        name="discoveryhelp",
        description="How server ratings and the retention nudges work",
    )
    @app_commands.guild_only()
    async def discoveryhelp(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        try:
            server_data = await self._run(
                self._db["servers"].find_one, {"guild_id": guild.id}) or {}
            cohorts = await self._run(
                lambda: list(self._db["roles"].find({"guild_id": guild.id})))
            rating_count = await self._run(
                self._db["ratings"].count_documents, {"guild_id": guild.id})
        except Exception as e:
            server_data, cohorts, rating_count = {}, [], 0
            print(f"[Ratings] status lookup failed: {e}")

        embed = discord.Embed(
            title="Server Ratings",
            description="Asks your members how they're finding the server, and gives people who "
                        "joined a week ago a quiet nudge to come and answer.",
            color=COLOR_INFO,
            timestamp=discord.utils.utcnow(),
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        embed.add_field(
            name="How it works",
            value=(
                "**1.** Someone joins and gets a hidden role named after that day's date, like "
                "`2026-08-04`. Everyone who joined the same day shares it.\n\n"
                f"**2.** {PING_AFTER_DAYS} days later, around midday, the bot pings that group "
                "once in your ratings channel and deletes the ping two seconds afterwards. They "
                "get the notification, your channel stays tidy.\n\n"
                f"**3.** They come back and tap a score from 1 to {SCALE}. It saves straight "
                "away, and you can read the results with `/ratings`.\n\n"
                f"**4.** On day {CLEANUP_AFTER_DAYS} the role gets deleted so your role list "
                "doesn't fill up."
            ),
            inline=False,
        )

        embed.add_field(
            name="Why it's worth doing",
            value=(
                "Most people decide whether a server is worth sticking with in their first "
                "couple of weeks. Asking for a score right at that point tells you what's "
                "actually landing, and the nudge itself pulls quiet members back in.\n\n"
                "Retention is also one of the things Discord weighs up when deciding which "
                "servers to surface in Server Discovery, so keeping people around genuinely "
                "helps your chances of getting listed."
            ),
            inline=False,
        )

        embed.add_field(
            name="Where the scores go",
            value=(f"Each score is saved against the member who gave it, one per person, in the "
                   f"bot's database. Changing your mind replaces your old score rather than "
                   f"adding a second one. Only people with Manage Server can read them, using "
                   f"`/ratings`."),
            inline=False,
        )

        channel = guild.get_channel(server_data.get("discovery_channel") or 0)
        pending = [c for c in cohorts if not c.get("mentioned")]

        if channel is None:
            status = "**Not set up yet.** Run `/setchannel` in the channel you want the survey in."
            embed.color = COLOR_WARN
        else:
            today = datetime.date.today()
            next_up = []
            for c in sorted(pending, key=lambda x: x.get("date", "")):
                try:
                    joined = datetime.date.fromisoformat(c["date"])
                except (ValueError, KeyError, TypeError):
                    continue
                ping_on = joined + datetime.timedelta(days=PING_AFTER_DAYS)
                days = (ping_on - today).days
                when = "today" if days == 0 else (f"in {days} days" if days > 0 else "overdue")
                next_up.append(f"joined `{c['date']}`, nudge {when}")

            status = (f"**Running** in {channel.mention}\n"
                      f"{rating_count} {'rating' if rating_count == 1 else 'ratings'} collected, "
                      f"{len(pending)} group{'' if len(pending) == 1 else 's'} waiting to be "
                      f"nudged")
            if next_up:
                status += "\n\n" + "\n".join(f"-# {line}" for line in next_up[:5])

            missing = [n for n, ok in (
                ("Manage Roles", guild.me.guild_permissions.manage_roles),
                ("Send Messages", channel.permissions_for(guild.me).send_messages),
            ) if not ok]
            if missing:
                status += f"\n\n**I'm missing {', '.join(missing)}**, so this can't work properly."
                embed.color = COLOR_WARN

        embed.add_field(name="Status here", value=status, inline=False)
        embed.set_footer(text="/setchannel to set up  •  /ratings to see scores")
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(client: commands.Bot):
    await client.add_cog(ServerRatings(client))
    print("ServerRatings cog loaded ✓")
