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
# The sentinel Invites writes for a join through the vanity url, imported rather than
# repeated. Two copies of a magic string is how one of them quietly stops matching, and the
# only symptom here would be a card showing `vanity` as though it were an invite code.
from Cogs.Invites import VANITY

COLOR = 0x3DDC97
COLOR_LOVE = 0xE85D9C

# Polls are disposable. Mongo drops them rather than keeping every question ever asked.
POLL_TTL_DAYS = 30

# marry:<proposer id>:<target id>:<yes|no>
MARRY_ID = re.compile(r"^marry:(\d+):(\d+):(yes|no)$")
# wyr:<a|b>
WYR_ID = re.compile(r"^wyr:([ab])$")
# rps:<solo|duel>:<rock|paper|scissors>:<who may press, 0 for either player>
RPS_ID = re.compile(r"^rps:(solo|duel):(rock|paper|scissors):(\d+)$")

# Games are disposable, like the polls.
GAME_TTL_DAYS = 7

THROWS = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
# What beats what. Only needs one direction: the reverse is a loss and a match is a draw.
BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}

# Twenty, the way the toy has them: ten yes, five maybe, five no. Weighted that way on
# purpose, because an eight ball that says no half the time stops being fun immediately.
EIGHT_BALL = [
    ("yes", "It is certain."), ("yes", "Without a doubt."), ("yes", "You may rely on it."),
    ("yes", "Yes, definitely."), ("yes", "As I see it, yes."), ("yes", "Most likely."),
    ("yes", "Outlook good."), ("yes", "Signs point to yes."), ("yes", "Yes."),
    ("yes", "Absolutely, and don't let anyone tell you otherwise."),
    ("maybe", "Reply hazy, try again."), ("maybe", "Ask again later."),
    ("maybe", "Better not tell you now."), ("maybe", "Cannot predict now."),
    ("maybe", "Concentrate and ask again."),
    ("no", "Don't count on it."), ("no", "My reply is no."), ("no", "My sources say no."),
    ("no", "Outlook not so good."), ("no", "Very doubtful."),
]
EIGHT_BALL_COLOURS = {"yes": 0x3DDC97, "maybe": 0xF0B45F, "no": 0xF27272}

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


# The badge names are the ones /stats badges already uses, so the same flag is called the same
# thing wherever it turns up. Imported rather than copied for exactly that reason.
BADGES = {
    "staff": "Discord Staff", "partner": "Partner", "hypesquad": "HypeSquad Events",
    "bug_hunter": "Bug Hunter", "bug_hunter_level_2": "Bug Hunter L2",
    "hypesquad_bravery": "Bravery", "hypesquad_brilliance": "Brilliance",
    "hypesquad_balance": "Balance", "early_supporter": "Early Supporter",
    "verified_bot_developer": "Early Verified Bot Dev",
    "discord_certified_moderator": "Mod Programs Alumni",
    "active_developer": "Active Developer",
}

# Permissions worth naming. Everything else is either implied by these or nobody asks about
# it, and a list of forty is not information.
NOTABLE = [
    ("administrator", "Administrator"), ("manage_guild", "Manage Server"),
    ("manage_roles", "Manage Roles"), ("manage_channels", "Manage Channels"),
    ("manage_messages", "Manage Messages"), ("ban_members", "Ban"),
    ("kick_members", "Kick"), ("moderate_members", "Timeout"),
    ("mention_everyone", "Mention Everyone"), ("manage_webhooks", "Manage Webhooks"),
]

STATUS_WORDS = {"online": "🟢 Online", "idle": "🟡 Idle",
                "dnd": "🔴 Do not disturb", "offline": "⚫ Offline"}

VERIFICATION = {"none": "None", "low": "Low, verified email",
                "medium": "Medium, registered 5 minutes",
                "high": "High, member 10 minutes", "highest": "Highest, verified phone"}

CONTENT_FILTER = {"disabled": "Off", "no_role": "Members without roles",
                  "all_members": "Everyone"}


def stamp(when, style: str = "D") -> str:
    """Discord renders these in the reader's own timezone, which is the whole point of using
    them instead of writing a date out."""
    return f"<t:{int(when.timestamp())}:{style}>" if when else "unknown"


def meter(done: int, total: int, width: int = 10) -> str:
    filled = 0 if total <= 0 else min(width, round(done / total * width))
    return "▰" * filled + "▱" * (width - filled)


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
        games = self._db["rps_games"]
        games.create_index("started_at", expireAfterSeconds=GAME_TTL_DAYS * 86400,
                           name="ttl_started")
        games.create_index([("guild_id", 1)], name="guild")

    # ── who somebody is ──────────────────────────────────────────────
    @app_commands.command(name="userinfo", description="Everything the bot knows about somebody")
    @app_commands.describe(member="Who to look up. Defaults to you.")
    @app_commands.checks.cooldown(5, 30.0)
    @app_commands.guild_only()
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user
        await interaction.response.defer()

        # Discord's name situation: `name` is the unique handle, `global_name` is what they
        # chose to be called everywhere, and a nickname overrides both here. Showing the
        # handle matters because it is the only one that is actually theirs.
        titles = [f"**{member.display_name}**", f"`@{member.name}`"]
        if member.nick:
            titles.append(f"nicknamed here, otherwise {member.global_name or member.name}")

        embed = discord.Embed(
            colour=member.colour if member.colour.value else COLOR,
            description=f"{member.mention}\n" + " · ".join(titles[:2])
                        + (f"\n-# {titles[2]}" if len(titles) > 2 else ""))
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"ID {member.id}")

        # ── when ────────────────────────────────────────────────────
        age = (discord.utils.utcnow() - member.created_at).days
        embed.add_field(name="Account made",
                        value=f"{stamp(member.created_at)}\n{stamp(member.created_at, 'R')}\n"
                              f"-# {age:,} days old")
        if member.joined_at:
            here = (discord.utils.utcnow() - member.joined_at).days
            embed.add_field(name="Joined here",
                            value=f"{stamp(member.joined_at)}\n"
                                  f"{stamp(member.joined_at, 'R')}\n-# {here:,} days ago")
            # Where they sit in the queue, which is the bit people actually want to know.
            order = sorted((m for m in interaction.guild.members if m.joined_at),
                           key=lambda m: m.joined_at)
            try:
                place = order.index(member) + 1
                embed.add_field(name="Member number",
                                value=f"**#{place:,}**\n-# of {len(order):,} here")
            except ValueError:
                pass

        # ── what they are here ──────────────────────────────────────
        roles = [r for r in reversed(member.roles) if not r.is_default()]
        shown = " ".join(r.mention for r in roles[:20])
        embed.add_field(
            name=f"Roles ({len(roles)})",
            value=(shown + (f" and {len(roles) - 20} more" if len(roles) > 20 else ""))
                  if roles else "None yet",
            inline=False)

        if roles:
            top = roles[0]
            embed.add_field(name="Top role",
                            value=f"{top.mention}\n-# "
                                  f"{'#%06x' % top.colour.value if top.colour.value else 'no colour'}")

        perms = [label for attr, label in NOTABLE
                 if getattr(member.guild_permissions, attr, False)]
        if perms:
            # Administrator makes the rest true by definition, so listing them alongside it
            # says nothing and hides the one that matters.
            if "Administrator" in perms:
                perms = ["Administrator"]
            embed.add_field(name="Can", value=", ".join(perms[:8]), inline=False)

        badges = [label for attr, label in BADGES.items()
                  if getattr(member.public_flags, attr, False)]
        if badges:
            embed.add_field(name="Badges", value=", ".join(badges), inline=False)

        # ── flags worth noticing ────────────────────────────────────
        notes = []
        if member.id == interaction.guild.owner_id:
            notes.append("👑 Owns this server")
        if member.bot:
            notes.append("🤖 Is a bot")
        if member.premium_since:
            notes.append(f"💎 Boosting since {stamp(member.premium_since, 'R')}")
        if member.is_timed_out():
            notes.append(f"🔇 Timed out until {stamp(member.timed_out_until, 'R')}")
        if self.bot.intents.presences:
            notes.append(STATUS_WORDS.get(str(member.status), str(member.status).title()))
            playing = next((a for a in member.activities
                            if getattr(a, "name", None) and a.type is not
                            discord.ActivityType.custom), None)
            if playing:
                notes.append(f"🎮 {playing.type.name.title()} {playing.name}")

        # ── the parts only this bot knows ───────────────────────────
        try:
            notes += await self._member_history(interaction, member)
        except Exception as e:
            print(f"[Fun] userinfo history failed: {e}")

        if notes:
            embed.add_field(name="Also", value="\n".join(notes), inline=False)
        await interaction.followup.send(embed=embed)

    async def _member_history(self, interaction: discord.Interaction,
                              member: discord.Member) -> list:
        """What this bot knows about somebody that Discord doesn't.

        Kept apart because it is the only part that touches the database, and because it is
        the only part that has to think about who is allowed to see it.
        """
        guild_id = interaction.guild.id
        notes = []

        rated = await self._run(self._db["ratings"].find_one,
                                {"guild_id": guild_id, "user_id": member.id})
        if rated and isinstance(rated.get("rating"), int):
            notes.append(f"⭐ Rated this server **{rated['rating']}/10**")

        wed = await self._marriage(guild_id, member.id)
        if wed:
            other = next((p for p in wed["partners"] if p != member.id), None)
            partner = interaction.guild.get_member(other)
            notes.append(f"💍 Married to {partner.mention if partner else 'somebody who left'}"
                         f", {stamp(wed['since'], 'R')}")

        # Membership spells: how they got here, and whether this is their first time.
        spells = await self._run(
            lambda: list(self._db["memberships"]
                         .find({"guild_id": guild_id, "user_id": member.id})
                         .sort("joined_at", -1).limit(20)))
        if len(spells) > 1:
            notes.append(f"🔁 Has joined **{len(spells)}** times")
        if spells and spells[0].get("invite_code"):
            code = spells[0]["invite_code"]
            by = spells[0].get("inviter_name")
            notes.append(f"🔗 Came through `{'the vanity url' if code == VANITY else code}`"
                         + (f", made by **{by}**" if by else ""))

        # Warnings are nobody else's business. Shown only to somebody who could already look
        # them up with /warnings, which is the same bar the moderation commands use.
        if interaction.user.guild_permissions.moderate_members:
            cases = await self._run(
                self._db["mod_cases"].count_documents,
                {"guild_id": guild_id, "user_id": member.id})
            if cases:
                notes.append(f"📁 **{cases}** moderation case{'' if cases == 1 else 's'} "
                             f"-# only you can see this")
        return notes

    # ── what the server is ───────────────────────────────────────────
    @app_commands.command(name="serverinfo", description="Everything the bot knows about here")
    @app_commands.checks.cooldown(5, 30.0)
    @app_commands.guild_only()
    async def serverinfo(self, interaction: discord.Interaction):
        guild = interaction.guild
        await interaction.response.defer()

        humans = sum(1 for m in guild.members if not m.bot)
        bots = guild.member_count - humans

        embed = discord.Embed(colour=COLOR, description=guild.description or None)
        embed.set_author(name=guild.name,
                         icon_url=guild.icon.url if guild.icon else None)
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        # The banner is the one thing that makes this card look like the server rather than
        # like every other server.
        if guild.banner:
            embed.set_image(url=guild.banner.url)
        embed.set_footer(text=f"ID {guild.id}")

        # ── the basics ──────────────────────────────────────────────
        age = (discord.utils.utcnow() - guild.created_at).days
        embed.add_field(name="Made",
                        value=f"{stamp(guild.created_at)}\n{stamp(guild.created_at, 'R')}\n"
                              f"-# {age:,} days old")
        embed.add_field(name="Owner", value=f"<@{guild.owner_id}>")
        embed.add_field(name="Members",
                        value=f"**{guild.member_count:,}**\n"
                              f"-# {humans:,} {'person' if humans == 1 else 'people'}\n"
                              f"-# {bots:,} bot{'' if bots == 1 else 's'}")

        # ── the furniture ───────────────────────────────────────────
        threads = len(getattr(guild, "threads", []) or [])
        channels = [f"{len(guild.text_channels)} text", f"{len(guild.voice_channels)} voice"]
        if getattr(guild, "stage_channels", None):
            channels.append(f"{len(guild.stage_channels)} stage")
        if getattr(guild, "forums", None):
            channels.append(f"{len(guild.forums)} forum")
        if threads:
            channels.append(f"{threads} threads")
        embed.add_field(
            name=f"Channels ({len(guild.channels) - len(guild.categories)})",
            value="\n".join(f"-# {line}" for line in channels)
                  + f"\n-# in {len(guild.categories)} "
                    f"categor{'y' if len(guild.categories) == 1 else 'ies'}")
        embed.add_field(name="Roles", value=f"**{len(guild.roles) - 1}**\n-# not counting "
                                            f"@everyone")
        embed.add_field(
            name="Emoji and stickers",
            value=f"**{len(guild.emojis)}** emoji\n-# of {guild.emoji_limit} allowed\n"
                  f"**{len(guild.stickers)}** "
                  f"sticker{'' if len(guild.stickers) == 1 else 's'}")

        # ── boosts, with how far off the next level ─────────────────
        # The numbers Discord asks for. Worth spelling out because the tier alone doesn't say
        # how close you are, which is the only thing anybody wants to know about boosts.
        needed = {0: 2, 1: 7, 2: 14}.get(guild.premium_tier)
        boosts = guild.premium_subscription_count or 0
        if needed:
            embed.add_field(
                name=f"Boosts · level {guild.premium_tier}",
                value=f"{meter(boosts, needed)}\n**{boosts}** of {needed} "
                      f"for level {guild.premium_tier + 1}", inline=False)
        else:
            embed.add_field(name=f"Boosts · level {guild.premium_tier}",
                            value=f"**{boosts}**, which is the top level")

        # ── how locked down it is ───────────────────────────────────
        safety = [f"Verification: {VERIFICATION.get(str(guild.verification_level), '?')}",
                  f"Media scanning: "
                  f"{CONTENT_FILTER.get(str(guild.explicit_content_filter), '?')}",
                  f"Two factor for moderators: {'on' if guild.mfa_level else 'off'}"]
        embed.add_field(name="Safety", value="\n".join(f"-# {s}" for s in safety),
                        inline=False)

        # ── what Discord has switched on ────────────────────────────
        wanted = {"COMMUNITY": "Community", "DISCOVERABLE": "In Discovery",
                  "VANITY_URL": "Vanity url", "PARTNERED": "Partnered",
                  "VERIFIED": "Verified", "WELCOME_SCREEN_ENABLED": "Welcome screen",
                  "MEMBER_VERIFICATION_GATE_ENABLED": "Rules gate",
                  "ANIMATED_ICON": "Animated icon", "BANNER": "Banner",
                  "INVITE_SPLASH": "Invite splash"}
        on = [label for flag, label in wanted.items() if flag in guild.features]
        if guild.vanity_url_code:
            on.append(f"discord.gg/{guild.vanity_url_code}")
        embed.add_field(name="Switched on", value=", ".join(on) if on else "Nothing special",
                        inline=False)

        # ── the bot's own angle, and the reason it is here ──────────
        try:
            embed.add_field(name="Since I've been watching",
                            value="\n".join(await self._server_history(guild)), inline=False)
        except Exception as e:
            print(f"[Fun] serverinfo history failed: {e}")

        await interaction.followup.send(embed=embed)

    async def _server_history(self, guild: discord.Guild) -> list:
        """Joins, leaves and the week's best invite, off the records this bot already keeps."""
        now = datetime.datetime.now(datetime.timezone.utc)
        week = now - datetime.timedelta(days=7)
        spells = await self._run(
            lambda: list(self._db["memberships"].find({"guild_id": guild.id})
                         .sort("joined_at", -1).limit(20000)))
        if not spells:
            return ["-# Nothing yet. The count starts the first time somebody joins."]

        recent = [s for s in spells if (s.get("joined_at") or now).replace(
            tzinfo=datetime.timezone.utc) >= week] if spells else []
        left = sum(1 for s in spells if s.get("left_at"))
        lines = [f"**{len(spells):,}** joins recorded, **{len(spells) - left:,}** still here",
                 f"**{len(recent)}** joined in the last 7 days"]

        # Which invite is bringing people right now, which is the one thing an owner would
        # act on today.
        counts = {}
        for spell in recent:
            code = spell.get("invite_code")
            if code:
                counts[code] = counts.get(code, 0) + 1
        if counts:
            code, n = max(counts.items(), key=lambda kv: kv[1])
            lines.append(f"Best invite this week: `{'the vanity url' if code == VANITY else code}`"
                         f" with **{n}**")

        rated = await self._run(
            lambda: list(self._db["ratings"].find({"guild_id": guild.id}).limit(20000)))
        scores = [r["rating"] for r in rated if isinstance(r.get("rating"), int)]
        if scores:
            lines.append(f"Rated **{sum(scores) / len(scores):.1f}/10** by "
                         f"**{len(scores)}** {'person' if len(scores) == 1 else 'people'}")
        return lines

    # ── what a role is ───────────────────────────────────────────────
    @app_commands.command(name="roleinfo", description="Everything about one role")
    @app_commands.describe(role="The role to look at")
    @app_commands.checks.cooldown(5, 30.0)
    @app_commands.guild_only()
    async def roleinfo(self, interaction: discord.Interaction, role: discord.Role):
        guild = interaction.guild
        holders = role.members
        colour = role.colour if role.colour.value else COLOR

        embed = discord.Embed(colour=colour, description=role.mention)
        embed.set_author(name=role.name)
        embed.set_footer(text=f"ID {role.id}")

        embed.add_field(name="Made", value=f"{stamp(role.created_at)}\n"
                                           f"{stamp(role.created_at, 'R')}")
        embed.add_field(name="Colour",
                        value=f"`#{role.colour.value:06x}`" if role.colour.value
                              else "-# none, so it inherits")
        embed.add_field(name="Position",
                        value=f"**{role.position}**\n-# of {len(guild.roles) - 1}")

        embed.add_field(
            name=f"Has it ({len(holders)})",
            value=(", ".join(m.display_name for m in holders[:20])
                   + (f" and {len(holders) - 20} more" if len(holders) > 20 else ""))
                  if holders else "Nobody yet",
            inline=False)

        perms = [label for attr, label in NOTABLE if getattr(role.permissions, attr, False)]
        if perms:
            if "Administrator" in perms:
                perms = ["Administrator, which is everything"]
            embed.add_field(name="Grants", value=", ".join(perms[:8]), inline=False)

        notes = [f"{'Shown' if role.hoist else 'Not shown'} separately in the member list",
                 f"{'Anyone' if role.mentionable else 'Only people with Mention Everyone'} "
                 f"can ping it"]
        # The same rule the dashboard enforces, said here so it can be found without opening
        # the website: Discord refuses these assignments with a bare 403 and no explanation.
        if role.managed:
            notes.append("⚠️ Managed by an integration, so nobody can assign it by hand")
        elif guild.me and role >= guild.me.top_role:
            notes.append("⚠️ Sits at or above my highest role, so I can't hand it out")
        embed.add_field(name="Worth knowing", value="\n".join(f"-# {n}" for n in notes),
                        inline=False)
        await interaction.response.send_message(embed=embed)

    # ── pictures ─────────────────────────────────────────────────────
    @app_commands.command(name="avatar", description="Somebody's avatar, full size")
    @app_commands.describe(member="Whose. Defaults to you.")
    @app_commands.checks.cooldown(5, 30.0)
    @app_commands.guild_only()
    async def avatar(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user
        embed = discord.Embed(colour=member.colour if member.colour.value else COLOR)
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        embed.set_image(url=member.display_avatar.url)

        # A per-server avatar is a different picture from the account one, and somebody
        # asking for an avatar usually wants whichever they didn't just see.
        links = [f"[Shown here]({member.display_avatar.url})"]
        if member.guild_avatar and member.avatar:
            links.append(f"[Their account one]({member.avatar.url})")
            embed.set_footer(text="They've set a different avatar just for this server.")
        embed.description = " · ".join(links)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="banner", description="Somebody's banner, if they have one")
    @app_commands.describe(member="Whose. Defaults to you.")
    @app_commands.checks.cooldown(5, 30.0)
    @app_commands.guild_only()
    async def banner(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user
        await interaction.response.defer()

        # Banners are not on the cached member, only on a freshly fetched user. There is no
        # way round the extra call, which is why this defers first.
        try:
            user = await self.bot.fetch_user(member.id)
        except discord.HTTPException:
            await interaction.followup.send(
                "Discord wouldn't tell me. Try again in a moment.", ephemeral=True)
            return

        embed = discord.Embed(colour=user.accent_colour or member.colour or COLOR)
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        if user.banner:
            embed.set_image(url=user.banner.url)
            embed.description = f"[Full size]({user.banner.url})"
        elif user.accent_colour:
            embed.description = (f"No banner, just a colour: "
                                 f"`#{user.accent_colour.value:06x}`")
        else:
            embed.description = f"{member.display_name} hasn't set a banner."
        await interaction.followup.send(embed=embed)

    # ── the eight ball ───────────────────────────────────────────────
    @app_commands.command(name="8ball", description="Ask it something. It answers badly.")
    @app_commands.describe(question="What you want to know")
    @app_commands.checks.cooldown(5, 30.0)
    @app_commands.guild_only()
    async def eight_ball(self, interaction: discord.Interaction, question: str):
        mood, answer = random.choice(EIGHT_BALL)
        embed = discord.Embed(colour=EIGHT_BALL_COLOURS[mood],
                              description=f"🎱 **{answer}**")
        # Their words, quoted rather than repeated, so a long question can't be used to make
        # the bot post a wall of text with its name on it.
        embed.set_author(name=question[:250], icon_url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    # ── rock paper scissors ──────────────────────────────────────────
    @app_commands.command(name="rps", description="Rock paper scissors, against me or anyone")
    @app_commands.describe(member="Who to challenge. Leave it out to play me.")
    @app_commands.checks.cooldown(4, 30.0)
    @app_commands.guild_only()
    async def rps(self, interaction: discord.Interaction, member: discord.Member = None):
        if member and member.id == interaction.user.id:
            await interaction.response.send_message(
                "You'd win, but at what cost.", ephemeral=True)
            return
        if member and member.bot:
            await interaction.response.send_message(
                "Leave it out. Run it without anybody to play me.", ephemeral=True)
            return

        if member is None:
            # Nothing to remember: I pick when you click, and the id says who may click.
            view = self._throws("solo", interaction.user.id)
            embed = discord.Embed(
                colour=COLOR, title="🪨 📄 ✂️",
                description=f"{interaction.user.mention}, pick one. I'll go at the same time.")
            await interaction.response.send_message(embed=embed, view=view)
            return

        view = self._throws("duel", 0)
        embed = discord.Embed(
            colour=COLOR, title="🪨 📄 ✂️",
            description=f"{interaction.user.mention} challenges {member.mention}.\n"
                        f"Both pick. Nobody sees anything until you both have.")
        await interaction.response.send_message(content=member.mention, embed=embed, view=view)
        posted = await interaction.original_response()
        try:
            await self._run(self._db["rps_games"].insert_one, {
                "_id": posted.id,
                "guild_id": interaction.guild.id,
                "players": [interaction.user.id, member.id],
                "picks": {},
                "started_at": datetime.datetime.now(datetime.timezone.utc),
            })
        except Exception as e:
            print(f"[Fun] couldn't record the game: {e}")

    @staticmethod
    def _throws(mode: str, who: int) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        for throw, emoji in THROWS.items():
            view.add_item(discord.ui.Button(
                label=throw.title(), emoji=emoji, style=discord.ButtonStyle.secondary,
                custom_id=f"rps:{mode}:{throw}:{who}"))
        return view

    @staticmethod
    def _outcome(mine: str, theirs: str) -> int:
        """1 if the first wins, -1 if the second does, 0 for a draw."""
        if mine == theirs:
            return 0
        return 1 if BEATS[mine] == theirs else -1

    async def _play_solo(self, interaction: discord.Interaction, throw: str, who: int):
        if interaction.user.id != who:
            await interaction.response.send_message(
                "That's somebody else's game. Run `/rps` for your own.", ephemeral=True)
            return
        mine = random.choice(list(THROWS))
        result = self._outcome(throw, mine)
        embed = discord.Embed(
            colour={1: 0x3DDC97, 0: 0xF0B45F, -1: 0xF27272}[result],
            title={1: "You win", 0: "A draw", -1: "I win"}[result],
            description=f"{THROWS[throw]} **{throw.title()}**  vs  "
                        f"**{mine.title()}** {THROWS[mine]}")
        embed.set_author(name=interaction.user.display_name,
                         icon_url=interaction.user.display_avatar.url)
        await interaction.response.edit_message(embed=embed, view=None)

    async def _play_duel(self, interaction: discord.Interaction, throw: str):
        try:
            game = await self._run(self._db["rps_games"].find_one,
                                   {"_id": interaction.message.id})
        except Exception as e:
            print(f"[Fun] game lookup failed: {e}")
            game = None

        if not game:
            await interaction.response.send_message(
                "That game is too old to finish. Start a new one with `/rps`.", ephemeral=True)
            return
        if interaction.user.id not in game["players"]:
            await interaction.response.send_message(
                "You're not in this one.", ephemeral=True)
            return

        me = str(interaction.user.id)
        if me in (game.get("picks") or {}):
            await interaction.response.send_message(
                f"You've already gone. You picked {THROWS[game['picks'][me]]}.", ephemeral=True)
            return

        try:
            game = await self._run(
                self._db["rps_games"].find_one_and_update,
                {"_id": interaction.message.id, f"picks.{me}": {"$exists": False}},
                {"$set": {f"picks.{me}": throw}}, return_document=True)
        except Exception as e:
            print(f"[Fun] couldn't record the pick: {e}")
            game = None
        if not game:
            # Somebody double clicked and the other press won. Nothing to add.
            await interaction.response.send_message("Already counted.", ephemeral=True)
            return

        picks = game.get("picks") or {}
        one, two = game["players"]
        if len(picks) < 2:
            # Told privately, so pressing a button doesn't leak which one to the other player.
            await interaction.response.send_message(
                f"{THROWS[throw]} locked in. Waiting for the other one.", ephemeral=True)
            return

        first, second = picks[str(one)], picks[str(two)]
        result = self._outcome(first, second)
        if result == 0:
            title, colour = "A draw", 0xF0B45F
        else:
            winner = one if result == 1 else two
            title, colour = f"<@{winner}> wins", 0x3DDC97
        embed = discord.Embed(
            colour=colour, title="🪨 📄 ✂️",
            description=f"{title}\n\n<@{one}> {THROWS[first]} **{first.title()}**\n"
                        f"<@{two}> {THROWS[second]} **{second.title()}**")
        await interaction.response.edit_message(content=None, embed=embed, view=None)
        try:
            await self._run(self._db["rps_games"].delete_one, {"_id": interaction.message.id})
        except Exception as e:
            print(f"[Fun] couldn't clear the finished game: {e}")

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
            return

        game = RPS_ID.match(custom_id)
        if game:
            mode, throw, who = game.group(1), game.group(2), int(game.group(3))
            if mode == "solo":
                await self._play_solo(interaction, throw, who)
            else:
                await self._play_duel(interaction, throw)


async def setup(bot: commands.Bot):
    await bot.add_cog(Fun(bot))
    print("✓ Fun cog loaded")
