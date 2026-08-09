"""The commands nobody needs and everybody uses.

Worth having in a bot about retention rather than in spite of it. A server people enjoy being
in is the whole thing the rest of this bot measures, and a bot with no sense of humour gets
replaced by one that has.

Two decisions run through the file:

- Buttons are handled in on_interaction against an explicit custom_id, not by a View object's
  callbacks. A View lives in the process and dies with it, so after a deploy every button on
  every older message would go dead. Everything needed to act on a click is encoded in the id
  itself, which means a proposal from last week still works after a restart with nothing
  stored about it anywhere.
- /ship is worked out from the two user ids rather than rolled. A compatibility score that
  changes every time you ask is not a joke anybody laughs at twice.
"""

import asyncio
import datetime
import hashlib
import random
import re

import discord
from discord import app_commands
from discord.ext import commands

import Database

COLOR = 0x3DDC97
COLOR_LOVE = 0xE85D9C

# Polls are disposable. Mongo drops them rather than keeping every question ever asked.
POLL_TTL_DAYS = 30

# marry:<proposer id>:<target id>:<yes|no>
MARRY_ID = re.compile(r"^marry:(\d+):(\d+):(yes|no)$")
# wyr:<a|b>
WYR_ID = re.compile(r"^wyr:([ab])$")

# Deliberately safe for a server anybody can join, and deliberately not topical: a question
# that needs context stops being funny the moment somebody reads it a month later.
QUESTIONS = [
    ("Always be 10 minutes late", "Always be 20 minutes early"),
    ("Have unlimited battery on every device", "Have free wifi everywhere you go"),
    ("Fight one horse-sized duck", "Fight a hundred duck-sized horses"),
    ("Never be able to skip an ad again", "Never be able to use headphones again"),
    ("Only be able to whisper", "Only be able to shout"),
    ("Live without music", "Live without films"),
    ("Know when you'll die", "Know how you'll die"),
    ("Be the funniest person in the room", "Be the smartest person in the room"),
    ("Never have to sleep", "Never have to eat"),
    ("Speak every language", "Play every instrument"),
    ("Have a rewind button for your life", "Have a pause button"),
    ("Read minds", "Be invisible"),
    ("Never use a touchscreen again", "Never use a keyboard again"),
    ("Always have to say what you're thinking", "Never be able to speak again"),
    ("Be stuck on a broken lift for a day", "Be stuck in traffic for a day"),
    ("Have hiccups for a year", "Feel like you need to sneeze for a year"),
    ("Give up seasoning", "Give up cheese"),
    ("Be famous for something embarrassing", "Never be recognised for anything"),
    ("Have every song stuck in your head for a week", "Never hear a new song again"),
    ("Fight your way out of a paper bag on camera", "Explain every meme to your gran"),
    ("Lose all your photos", "Lose all your messages"),
    ("Have a dragon", "Be a dragon"),
    ("Live in a world with no lies", "Live in a world with no laws"),
    ("Always be slightly too hot", "Always be slightly too cold"),
    ("Only eat breakfast food forever", "Never eat breakfast food again"),
    ("Be able to teleport but only to places you've been", "Fly, but only at walking pace"),
    ("Have a personal chef", "Have a personal driver"),
    ("Never wait in a queue again", "Never get a red light again"),
    ("Know every conspiracy is true", "Know none of them are"),
    ("Have to sing instead of speak", "Have to dance everywhere you walk"),
    ("Restart your favourite game with no memory of it", "Get a perfect sequel to it"),
    ("Be trapped in a horror film", "Be trapped in a soap opera"),
    ("Have your search history made public", "Have your bank balance made public"),
    ("Never get another notification", "Never miss another notification"),
    ("Own a house you can't leave", "Travel forever with no house"),
    ("Be great at a job you hate", "Be terrible at a job you love"),
]


def compatibility(one: int, two: int) -> int:
    """A stable 0 to 100 for a pair of people, in either order.

    Hashed rather than random on purpose. Two people who ask twice get the same answer, which
    is the only thing that makes it worth asking once.
    """
    low, high = sorted((int(one), int(two)))
    digest = hashlib.sha256(f"{low}:{high}".encode()).hexdigest()
    return int(digest[:8], 16) % 101


# Welding two names together lands on something unfortunate more often than you would guess:
# Sam and Alex produce exactly one of these. Not an attempt at a swear filter, just enough to
# stop the obvious ones coming out of a bot that also ships an automod for the same words.
# Only exact matches, because that is where short blends actually go wrong.
AWKWARD = {"sex", "ass", "tit", "cum", "fag", "dick", "cock", "piss", "shit", "fuck",
           "bum", "wank", "twat", "crap", "anal", "slut", "hoe"}


def ship_name(one: str, two: str) -> str:
    """The front of one name welded to the back of the other."""
    one, two = (one or "?").strip(), (two or "?").strip()

    def blend(a, b):
        return (a[:max(1, len(a) // 2)] + (b[len(b) // 2:] or b[-1])).title()

    name = blend(one, two)
    if name.lower() in AWKWARD:
        # Take the halves from the other name each. It nearly always lands somewhere else,
        # and if it somehow doesn't, the pair keep whatever they get.
        name = blend(two, one)
    return name


def bar(percent: int, width: int = 10) -> str:
    """Hearts rather than blocks. It is a love meter, and Discord renders emoji large enough
    to read at a glance, which is the whole job."""
    filled = round(percent / 100 * width)
    return "❤️" * filled + "🤍" * (width - filled)


# score floor -> (emoji, colour, what to say about it). Read from the top down, first match
# wins, so the order matters more than the numbers.
VERDICTS = [
    (95, "💞", 0xFF4D8D, "a formality at this point"),
    (80, "💖", 0xFF4D8D, "alarmingly strong"),
    (60, "💗", 0xE85D9C, "genuinely quite good"),
    (45, "💓", 0xE85D9C, "some promise"),
    (25, "💔", 0xF0B45F, "not impossible, with work"),
    (10, "🥀", 0x8BA79B, "a lost cause"),
    (0, "🧊", 0x8BA79B, "nothing at all"),
]


def verdict(score: int) -> tuple:
    """The emoji, colour and wording for a score."""
    for floor, emoji, colour, words in VERDICTS:
        if score >= floor:
            return emoji, colour, words
    return VERDICTS[-1][1:]


class Fun(commands.Cog, name="Fun"):
    """Profiles, polls and a wedding chapel."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _run(self, fn, *args, **kwargs):
        # pymongo is synchronous, and blocking the loop on a fun command is still blocking it.
        return await asyncio.to_thread(lambda: fn(*args, **kwargs))

    @property
    def _db(self):
        return Database.get_bot_database(self.bot.MongoClient)

    async def cog_load(self):
        try:
            await self._run(self._ensure_indexes)
        except Exception as e:
            print(f"[Fun] index setup failed: {e}")

    def _ensure_indexes(self):
        marriages = self._db["marriages"]
        # One marriage per person per server, enforced by the database rather than by hoping
        # two clicks never land at once.
        marriages.create_index([("guild_id", 1), ("partners", 1)], name="guild_partners")
        polls = self._db["wyr_polls"]
        polls.create_index("asked_at", expireAfterSeconds=POLL_TTL_DAYS * 86400,
                           name="ttl_asked")
        polls.create_index([("guild_id", 1)], name="guild")

    # ── who somebody is ──────────────────────────────────────────────
    @app_commands.command(name="userinfo", description="Everything the bot knows about somebody")
    @app_commands.describe(member="Who to look up. Defaults to you.")
    @app_commands.checks.cooldown(5, 30.0)
    @app_commands.guild_only()
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user
        await interaction.response.defer()

        created = int(member.created_at.timestamp())
        embed = discord.Embed(
            title=member.display_name,
            colour=member.colour if member.colour.value else COLOR,
            description=f"{member.mention} · `{member.id}`")
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Account made", value=f"<t:{created}:D>\n<t:{created}:R>")

        if member.joined_at:
            joined = int(member.joined_at.timestamp())
            embed.add_field(name="Joined here", value=f"<t:{joined}:D>\n<t:{joined}:R>")
            # Where they sit in the queue, which is the bit people actually want to know.
            order = sorted((m for m in interaction.guild.members if m.joined_at),
                           key=lambda m: m.joined_at)
            embed.add_field(name="Member number", value=f"#{order.index(member) + 1}")

        # Roles highest first, and @everyone left out because everyone has it.
        roles = [r.mention for r in reversed(member.roles) if not r.is_default()]
        embed.add_field(
            name=f"Roles ({len(roles)})",
            value=" ".join(roles[:15]) + (" …" if len(roles) > 15 else "") if roles else "None",
            inline=False)

        extras = []
        if member.premium_since:
            extras.append(f"Boosting since <t:{int(member.premium_since.timestamp())}:R>")
        if member.bot:
            extras.append("Is a bot")
        if member.id == interaction.guild.owner_id:
            extras.append("Owns this server")

        # The two things only this bot can add. Both are quiet when there is nothing to say.
        try:
            rated = await self._run(self._db["ratings"].find_one,
                                    {"guild_id": interaction.guild.id, "user_id": member.id})
            if rated and isinstance(rated.get("rating"), int):
                extras.append(f"Rated this server **{rated['rating']}/10**")
            wed = await self._marriage(interaction.guild.id, member.id)
            if wed:
                other = next((p for p in wed["partners"] if p != member.id), None)
                partner = interaction.guild.get_member(other)
                extras.append(f"Married to {partner.mention if partner else 'somebody who left'}"
                              f" since <t:{int(wed['since'].timestamp())}:D>")
        except Exception as e:
            print(f"[Fun] userinfo extras failed: {e}")

        if extras:
            embed.add_field(name="Also", value="\n".join(extras), inline=False)
        await interaction.followup.send(embed=embed)

    # ── what the server is ───────────────────────────────────────────
    @app_commands.command(name="serverinfo", description="Everything the bot knows about here")
    @app_commands.checks.cooldown(5, 30.0)
    @app_commands.guild_only()
    async def serverinfo(self, interaction: discord.Interaction):
        guild = interaction.guild
        await interaction.response.defer()

        humans = sum(1 for m in guild.members if not m.bot)
        created = int(guild.created_at.timestamp())

        embed = discord.Embed(title=guild.name, colour=COLOR,
                              description=guild.description or None)
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.add_field(name="Made", value=f"<t:{created}:D>\n<t:{created}:R>")
        embed.add_field(name="Owner", value=f"<@{guild.owner_id}>")
        embed.add_field(name="Members",
                        value=f"{guild.member_count}\n{humans} people, "
                              f"{guild.member_count - humans} bots")
        embed.add_field(name="Channels",
                        value=f"{len(guild.text_channels)} text\n"
                              f"{len(guild.voice_channels)} voice\n"
                              f"{len(guild.categories)} categories")
        embed.add_field(name="Roles", value=str(len(guild.roles) - 1))
        embed.add_field(name="Boosts",
                        value=f"{guild.premium_subscription_count or 0}\nLevel "
                              f"{guild.premium_tier}")

        # The features people care about, named the way Discord's own settings name them.
        wanted = {"COMMUNITY": "Community", "DISCOVERABLE": "In Discovery",
                  "VANITY_URL": "Vanity url", "PARTNERED": "Partnered", "VERIFIED": "Verified"}
        on = [label for flag, label in wanted.items() if flag in guild.features]
        if on:
            embed.add_field(name="Switched on", value=", ".join(on), inline=False)

        # The bot's own angle, and the reason it is here at all.
        try:
            week = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)
            joined = await self._run(
                self._db["memberships"].count_documents,
                {"guild_id": guild.id, "joined_at": {"$gte": week}})
            if joined:
                embed.add_field(name="Joined this week", value=str(joined), inline=False)
        except Exception as e:
            print(f"[Fun] serverinfo joins failed: {e}")

        embed.set_footer(text=f"ID {guild.id}")
        await interaction.followup.send(embed=embed)

    # ── would you rather ─────────────────────────────────────────────
    @app_commands.command(name="wouldyourather", description="A question, and two buttons")
    @app_commands.checks.cooldown(3, 60.0, key=lambda i: i.channel_id)
    @app_commands.guild_only()
    async def wouldyourather(self, interaction: discord.Interaction):
        left, right = random.choice(QUESTIONS)
        embed = discord.Embed(title="Would you rather…", colour=COLOR,
                              description=f"🅰️ {left}\n\n🅱️ {right}")
        embed.set_footer(text="Nobody has voted yet")

        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="A", style=discord.ButtonStyle.blurple,
                                        custom_id="wyr:a"))
        view.add_item(discord.ui.Button(label="B", style=discord.ButtonStyle.green,
                                        custom_id="wyr:b"))

        await interaction.response.send_message(embed=embed, view=view)
        posted = await interaction.original_response()
        try:
            await self._run(self._db["wyr_polls"].insert_one, {
                "_id": posted.id,
                "guild_id": interaction.guild.id,
                "a": [], "b": [],
                "left": left, "right": right,
                "asked_at": datetime.datetime.now(datetime.timezone.utc),
            })
        except Exception as e:
            # The buttons will say the poll has gone rather than silently doing nothing.
            print(f"[Fun] couldn't record the poll: {e}")

    async def _vote(self, interaction: discord.Interaction, choice: str):
        other = "b" if choice == "a" else "a"
        try:
            # One update: out of whichever side they were on, into the one they picked. Two
            # separate operations would let a double click land them in both.
            poll = await self._run(
                self._db["wyr_polls"].find_one_and_update,
                {"_id": interaction.message.id},
                {"$pull": {other: interaction.user.id},
                 "$addToSet": {choice: interaction.user.id}},
                return_document=True)
        except Exception as e:
            print(f"[Fun] vote failed: {e}")
            poll = None

        if not poll:
            await interaction.response.send_message(
                "That poll is too old to count, sorry. Ask a new one with "
                "`/wouldyourather`.", ephemeral=True)
            return

        a, b = len(poll.get("a") or []), len(poll.get("b") or [])
        total = a + b
        share = round(a / total * 100) if total else 0
        embed = interaction.message.embeds[0]
        embed.set_footer(text=f"A {share}%  ·  B {100 - share}%  ·  "
                              f"{total} vote{'' if total == 1 else 's'}")
        await interaction.response.edit_message(embed=embed)

    # ── marriage ─────────────────────────────────────────────────────
    async def _marriage(self, guild_id: int, user_id: int):
        return await self._run(self._db["marriages"].find_one,
                               {"guild_id": guild_id, "partners": user_id})

    @app_commands.command(name="marry", description="Propose to somebody. They have to agree.")
    @app_commands.describe(member="Who you're asking")
    @app_commands.checks.cooldown(2, 60.0)
    @app_commands.guild_only()
    async def marry(self, interaction: discord.Interaction, member: discord.Member):
        if member.id == interaction.user.id:
            await interaction.response.send_message(
                "Self-love is important, but no.", ephemeral=True)
            return
        if member.bot:
            await interaction.response.send_message(
                "I'm flattered on their behalf, but bots don't marry.", ephemeral=True)
            return

        for person in (interaction.user, member):
            if await self._marriage(interaction.guild.id, person.id):
                who = "You're" if person == interaction.user else f"{person.display_name} is"
                await interaction.response.send_message(
                    f"{who} already married here. `/divorce` first.", ephemeral=True)
                return

        view = discord.ui.View(timeout=None)
        # Everything the accept needs is in the id, so this still works after a restart and
        # nothing has to be written down about a proposal that may never be answered.
        view.add_item(discord.ui.Button(
            label="Say yes", emoji="💍", style=discord.ButtonStyle.success,
            custom_id=f"marry:{interaction.user.id}:{member.id}:yes"))
        view.add_item(discord.ui.Button(
            label="Say no", style=discord.ButtonStyle.secondary,
            custom_id=f"marry:{interaction.user.id}:{member.id}:no"))

        score = compatibility(interaction.user.id, member.id)
        emoji, _colour, words = verdict(score)
        embed = discord.Embed(
            title="💍  A proposal",
            colour=COLOR_LOVE,
            description=f"**{interaction.user.display_name}** has asked "
                        f"**{member.display_name}** to marry them.")
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        # The score is on the proposal rather than saved for the wedding, because it is much
        # funnier as something they have to look at before deciding.
        embed.add_field(name="The numbers, before you answer",
                        value=f"{bar(score)}\n{emoji} **{score}%**, which is {words}.",
                        inline=False)
        embed.set_footer(text=f"Only {member.display_name} can answer this.")
        await interaction.response.send_message(content=member.mention, embed=embed, view=view)

    async def _answer_proposal(self, interaction: discord.Interaction,
                               proposer_id: int, target_id: int, answer: str):
        if interaction.user.id != target_id:
            await interaction.response.send_message(
                "This one isn't yours to answer.", ephemeral=True)
            return

        if answer == "no":
            embed = discord.Embed(
                title="🥀  Turned down",
                colour=0x8BA79B,
                description=f"<@{target_id}> said no to <@{proposer_id}>.\n"
                            f"These things happen. There are other servers.")
            await interaction.response.edit_message(content=None, embed=embed, view=None)
            return

        # Checked again here rather than trusting the check at proposal time: a proposal can
        # sit unanswered for a week, and either of them may have married somebody else in it.
        for person_id in (proposer_id, target_id):
            if await self._marriage(interaction.guild.id, person_id):
                await interaction.response.send_message(
                    f"<@{person_id}> has married somebody else since this was asked.",
                    ephemeral=True)
                return

        now = datetime.datetime.now(datetime.timezone.utc)
        try:
            await self._run(self._db["marriages"].insert_one, {
                "guild_id": interaction.guild.id,
                # Sorted so a pair reads the same however it was asked.
                "partners": sorted((proposer_id, target_id)),
                "since": now,
            })
        except Exception as e:
            print(f"[Fun] couldn't record the marriage: {e}")
            await interaction.response.send_message(
                "Something went wrong writing that down. Try again in a moment.",
                ephemeral=True)
            return

        score = compatibility(proposer_id, target_id)
        embed = discord.Embed(
            title="💒  Married",
            colour=COLOR_LOVE,
            description=f"<@{proposer_id}>  💞  <@{target_id}>\n\n"
                        f"Since <t:{int(now.timestamp())}:D>. The numbers said **{score}%**, "
                        f"and nobody asked them.")
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(text="/divorce, if it comes to that. Either of you can.")
        await interaction.response.edit_message(content=None, embed=embed, view=None)

    @app_commands.command(name="divorce", description="End your marriage in this server")
    @app_commands.checks.cooldown(2, 60.0)
    @app_commands.guild_only()
    async def divorce(self, interaction: discord.Interaction):
        wed = await self._marriage(interaction.guild.id, interaction.user.id)
        if not wed:
            await interaction.response.send_message(
                "You aren't married here.", ephemeral=True)
            return

        other = next((p for p in wed["partners"] if p != interaction.user.id), None)
        try:
            await self._run(self._db["marriages"].delete_one, {"_id": wed["_id"]})
        except Exception as e:
            print(f"[Fun] couldn't record the divorce: {e}")
            await interaction.response.send_message(
                "Something went wrong. Try again in a moment.", ephemeral=True)
            return

        embed = discord.Embed(
            title="📄  Divorced", colour=0x8BA79B,
            description=f"{interaction.user.mention} and <@{other}> are no longer married."
                        if other else f"{interaction.user.mention} is single again.")
        # How long it lasted is the only detail anybody wants, and it costs nothing: the date
        # it started is already on the record being deleted.
        since = wed.get("since")
        if since is not None:
            if since.tzinfo is None:
                since = since.replace(tzinfo=datetime.timezone.utc)
            days = (datetime.datetime.now(datetime.timezone.utc) - since).days
            embed.set_footer(
                text="It lasted less than a day." if days < 1 else
                     f"It lasted {days} day{'' if days == 1 else 's'}.")
        await interaction.response.send_message(embed=embed)

    # ── ship ─────────────────────────────────────────────────────────
    @app_commands.command(name="ship", description="How compatible are two people, allegedly")
    @app_commands.describe(member="The first person",
                           other="The second person. Defaults to you.")
    @app_commands.checks.cooldown(5, 30.0)
    @app_commands.guild_only()
    async def ship(self, interaction: discord.Interaction, member: discord.Member,
                   other: discord.Member = None):
        other = other or interaction.user
        if member.id == other.id:
            await interaction.response.send_message(
                "That's just one person.", ephemeral=True)
            return

        await interaction.response.defer()
        score = compatibility(member.id, other.id)
        emoji, colour, words = verdict(score)
        name = ship_name(member.display_name, other.display_name)

        embed = discord.Embed(
            title=f"{emoji}  {name}",
            colour=colour,
            description=f"{member.mention}  ×  {other.mention}\n\n"
                        f"{bar(score)}\n"
                        f"### {score}%\n"
                        f"The numbers say {words}.")
        # One face on the byline and the other in the corner. Two avatars is as close to a
        # composite as this gets without putting an image library on the dyno for a joke.
        # Same order as the description, so the top of the card and the middle agree.
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        embed.set_thumbnail(url=other.display_avatar.url)

        # If they really did marry each other in this server, the joke lands better when it
        # knows. Never fatal: a database blip just costs the extra line.
        footer = "Worked out from their IDs, so it never changes. Sorry."
        try:
            wed = await self._marriage(interaction.guild.id, member.id)
            if wed and other.id in wed["partners"]:
                embed.add_field(
                    name="For what it's worth",
                    value=f"These two are actually married here, since "
                          f"<t:{int(wed['since'].timestamp())}:D>.",
                    inline=False)
                footer = "The numbers had nothing to do with it."
        except Exception as e:
            print(f"[Fun] ship marriage check failed: {e}")

        embed.set_footer(text=footer)
        await interaction.followup.send(embed=embed)

    # ── button clicks ────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """Every component click in every server passes through here, so it leaves early."""
        if interaction.type is not discord.InteractionType.component:
            return
        if interaction.guild is None:
            return

        custom_id = (interaction.data or {}).get("custom_id", "")

        poll = WYR_ID.match(custom_id)
        if poll:
            await self._vote(interaction, poll.group(1))
            return

        proposal = MARRY_ID.match(custom_id)
        if proposal:
            await self._answer_proposal(interaction, int(proposal.group(1)),
                                        int(proposal.group(2)), proposal.group(3))


async def setup(bot: commands.Bot):
    await bot.add_cog(Fun(bot))
    print("✓ Fun cog loaded")
