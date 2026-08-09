"""The fun commands: profiles, would you rather, marriage and ship.

Light features, but the marriage half has real state and every bug in it is one somebody will
find on purpose. The interesting cases are all about who is allowed to press what: a proposal
is a button anybody in the channel can see, and only one person may answer it.

The button ids carry everything needed to act on a click, so a proposal still works after a
restart. That is also what makes it forgeable, which is why the checks live in the handler
rather than in the view that drew it.
"""
import pathlib as _pathlib
ROOT = _pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / "src")

import asyncio, datetime, sys, types
sys.path.insert(0, SRC_DIR)


class _Cursor(list):
    def sort(self, key, direction=1):
        # Two part key so a document missing the field never gets compared against one that
        # has it, which is a TypeError in Python and merely a null in Mongo.
        return _Cursor(sorted(self, key=lambda d: (d.get(key) is not None, d.get(key)),
                              reverse=direction < 0))

    def limit(self, n):
        return _Cursor(self[:n])


class FakeColl:
    def __init__(self, name):
        self.name = name
        self.docs = []
        self._ids = 0

    def create_index(self, *a, **k): pass

    @staticmethod
    def _read(doc, path):
        """Follows a dotted key the way Mongo does, so "picks.7" reaches into a sub-document."""
        current = doc
        for part in path.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current

    @staticmethod
    def _write(doc, path, value):
        parts = path.split(".")
        current = doc
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = value

    def _match(self, doc, query):
        for key, value in query.items():
            held = self._read(doc, key)
            if key == "partners" and isinstance(held, list):
                if value not in held:
                    return False
            elif isinstance(value, dict) and "$gte" in value:
                if not (held is not None and held >= value["$gte"]):
                    return False
            elif isinstance(value, dict) and "$exists" in value:
                # The one that matters for the duel: only record a pick if there isn't one.
                if (held is not None) != value["$exists"]:
                    return False
            elif held != value:
                return False
        return True

    def find(self, query=None, *a, **k):
        return _Cursor(d for d in self.docs if self._match(d, query or {}))

    def find_one(self, query, *a, **k):
        return next((d for d in self.docs if self._match(d, query)), None)

    def count_documents(self, query, *a, **k):
        return sum(1 for d in self.docs if self._match(d, query))

    def insert_one(self, doc):
        self._ids += 1
        stored = dict(doc)
        stored.setdefault("_id", self._ids)
        self.docs.append(stored)
        return types.SimpleNamespace(inserted_id=stored["_id"])

    def delete_one(self, query):
        for i, doc in enumerate(self.docs):
            if self._match(doc, query):
                del self.docs[i]
                return types.SimpleNamespace(deleted_count=1)
        return types.SimpleNamespace(deleted_count=0)

    def find_one_and_update(self, query, ops, return_document=None, **k):
        doc = self.find_one(query)
        if doc is None:
            return None
        for field, value in ops.get("$pull", {}).items():
            doc[field] = [v for v in doc.get(field, []) if v != value]
        for field, value in ops.get("$addToSet", {}).items():
            doc.setdefault(field, [])
            if value not in doc[field]:
                doc[field].append(value)
        for field, value in ops.get("$set", {}).items():
            self._write(doc, field, value)
        return doc


class FakeDB:
    def __init__(self): self.c = {}
    def __getitem__(self, n): return self.c.setdefault(n, FakeColl(n))


DB = FakeDB()
stub = types.ModuleType("Database"); stub.get_bot_database = lambda c: DB
sys.modules["Database"] = stub
for name in ("pymongo", "certifi", "dotenv"):
    mod = types.ModuleType(name)
    if name == "pymongo": mod.MongoClient = lambda *a, **k: object()
    if name == "certifi": mod.where = lambda: ""
    if name == "dotenv": mod.load_dotenv = lambda *a, **k: None
    sys.modules[name] = mod

import discord
from discord.ext import commands

GUILD = 500


class Reply:
    """Captures whatever a command sent back."""

    def __init__(self):
        self.sent = []
        self.edited = []
        self.deferred = False

    async def send_message(self, content=None, *, embed=None, view=None, ephemeral=False, **k):
        self.sent.append({"content": content, "embed": embed, "view": view,
                          "ephemeral": ephemeral})

    # Followups use this name instead. Same capture, so a check doesn't have to know which
    # route a command took.
    send = send_message

    async def edit_message(self, *, content=None, embed=None, view=None, **k):
        self.edited.append({"content": content, "embed": embed, "view": view})

    async def defer(self, **k):
        self.deferred = True

    @property
    def text(self):
        """Everything said, flattened, so a check doesn't care where the words landed."""
        out = []
        for item in self.sent + self.edited:
            if item.get("content"):
                out.append(str(item["content"]))
            embed = item.get("embed")
            if embed is not None:
                author = embed.author.name if embed.author else ""
                footer = embed.footer.text if embed.footer else ""
                out.append(f"{author} {embed.title or ''} {embed.description or ''} {footer}")
                for field in embed.fields:
                    out.append(f"{field.name} {field.value}")
        return "\n".join(out)


def stub_fetch(value):
    """Stands in for a coroutine method that just hands something back."""
    async def fetch(*a, **k):
        return value
    return fetch


class FakeRole:
    """A class rather than a namespace because roleinfo compares roles by position, and
    SimpleNamespace can't carry the comparison operators that needs."""

    def __init__(self, name, value=0, default=False, position=1):
        self.id = 40000 + position
        self.name = name
        self.mention = f"@{name}"
        # A real Colour, because the embed refuses anything else and that is
        # exactly what discord.py hands the command in production.
        self.colour = discord.Colour(value)
        self.position = position
        self.created_at = datetime.datetime(2023, 3, 1, tzinfo=datetime.timezone.utc)
        self.hoist = self.mentionable = self.managed = False
        self.members = []
        self.permissions = None
        self._default = default

    def is_default(self):
        return self._default

    def __ge__(self, other):
        return self.position >= other.position

    def __lt__(self, other):
        return self.position < other.position


def role(name, value=0, default=False, position=1):
    return FakeRole(name, value, default, position)


def permissions(**granted):
    """Everything false unless named, which is how a fresh member actually is."""
    attrs = {attr: False for attr, _ in sys.modules["Cogs.Fun"].NOTABLE}
    attrs.update(granted)
    return types.SimpleNamespace(**attrs)


def member(uid, name="someone", bot=False, joined=True, roles=None, nick=None,
           perms=None, badges=(), boosting=False, timed_out=False):
    return types.SimpleNamespace(
        id=uid, bot=bot, display_name=nick or name, name=name.lower(), global_name=name,
        nick=nick,
        mention=f"<@{uid}>",
        colour=discord.Colour(0),
        display_avatar=types.SimpleNamespace(url="http://x/a.png"),
        created_at=datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc),
        joined_at=(datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
                   if joined else None),
        premium_since=(datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)
                       if boosting else None),
        roles=[role("everyone", default=True)] + list(roles or []),
        guild_permissions=perms or permissions(),
        public_flags=types.SimpleNamespace(**{b: b in badges
                                              for b in sys.modules["Cogs.Fun"].BADGES}),
        is_timed_out=lambda: timed_out,
        timed_out_until=(datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)
                         if timed_out else None),
        status="online", activities=[])


class FakeInteraction:
    def __init__(self, user, custom_id=None, message_id=None, guild_members=()):
        self.user = user
        self.response = Reply()
        self.followup = Reply()
        self.data = {"custom_id": custom_id} if custom_id else {}
        self.type = (discord.InteractionType.component if custom_id
                     else discord.InteractionType.application_command)
        self.message = types.SimpleNamespace(
            id=message_id,
            embeds=[discord.Embed(title="Would you rather…", description="a or b")])
        self.channel_id = 9
        members = list(guild_members) or [user]
        self.guild = types.SimpleNamespace(
            id=GUILD, name="Test Server", owner_id=1,
            description="A server for testing things",
            member_count=len(members), members=members,
            created_at=datetime.datetime(2021, 6, 1, tzinfo=datetime.timezone.utc),
            icon=types.SimpleNamespace(url="http://x/icon.png"), banner=None,
            features=["COMMUNITY", "VANITY_URL", "WELCOME_SCREEN_ENABLED"],
            text_channels=[1, 2], voice_channels=[3], categories=[4],
            stage_channels=[], forums=[5], threads=[6, 7],
            channels=[1, 2, 3, 4, 5],
            roles=[1, 2, 3], premium_subscription_count=2, premium_tier=1,
            emojis=[1, 2, 3], stickers=[1], emoji_limit=50,
            verification_level="medium", explicit_content_filter="all_members",
            mfa_level=1, vanity_url_code="testing",
            get_member=lambda uid: next((m for m in members if m.id == uid), None))
        self._original = types.SimpleNamespace(id=message_id or 777)

    async def original_response(self):
        return self._original

    @property
    def text(self):
        return self.response.text + "\n" + self.followup.text


async def main():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    bot.MongoClient = object()
    await bot.load_extension("Cogs.Fun")
    cog = bot.get_cog("Fun")
    F = sys.modules["Cogs.Fun"]

    alex, sam, kit = member(1, "Alex"), member(2, "Sam"), member(3, "Kit")
    robot = member(9, "Botly", bot=True)
    everyone = [alex, sam, kit, robot]

    print("=== a ship score is the same every time, either way round ===")
    first = F.compatibility(1, 2)
    assert first == F.compatibility(2, 1), "order must not matter"
    assert first == F.compatibility(1, 2), "and it must not move between calls"
    assert 0 <= first <= 100, first
    # Different pairs get different answers, or the joke is that everyone is 50%.
    spread = {F.compatibility(1, n) for n in range(2, 40)}
    assert len(spread) > 20, f"only {len(spread)} distinct scores across 38 pairs"
    print(f"  {first}% for 1 and 2, stable both ways, {len(spread)} distinct across 38 pairs")

    print("\n=== the ship name and the bar ===")
    assert F.ship_name("Alex", "Sam") == "Alam", F.ship_name("Alex", "Sam")
    assert F.ship_name("A", "B") == "Ab", F.ship_name("A", "B")
    # Two ordinary names, one unfortunate blend. It has to take the other split instead: a
    # bot that filters these words should not be generating them.
    assert F.ship_name("Sam", "Alex") == "Alam", F.ship_name("Sam", "Alex")
    for a, b in (("Sam", "Alex"), ("Assa", "Assb"), ("Tim", "Kit"), ("Cuma", "Cumb")):
        assert F.ship_name(a, b).lower() not in F.AWKWARD, (a, b, F.ship_name(a, b))
    # Ten hearts wide whatever the score, or the meter would change length as it fills.
    for score in (0, 1, 50, 99, 100):
        filled, empty = F.bar(score).count("❤️"), F.bar(score).count("🤍")
        assert filled + empty == 10, (score, filled, empty)
    assert F.bar(100).count("❤️") == 10 and F.bar(0).count("❤️") == 0
    assert F.bar(50).count("❤️") == 5
    print(f"  Alex + Sam = {F.ship_name('Alex', 'Sam')}, and the meter is always 10 wide")

    print("\n=== every score gets a verdict, and they get warmer ===")
    seen = []
    for score in range(0, 101):
        emoji, colour, words = F.verdict(score)
        assert emoji and words and isinstance(colour, int), score
        seen.append((emoji, words))
    assert len(set(seen)) == len(F.VERDICTS), set(seen)
    # The bands have to be in order, or a 90% would read colder than a 30%.
    assert F.verdict(0)[2] != F.verdict(100)[2]
    assert F.verdict(100)[1] == 0xFF4D8D, "the top band is the brightest"
    print(f"  {len(set(seen))} bands, 0% is '{F.verdict(0)[2]}', "
          f"100% is '{F.verdict(100)[2]}'")

    print("\n=== ship refuses one person twice ===")
    i = FakeInteraction(alex, guild_members=everyone)
    await cog.ship.callback(cog, i, alex, alex)
    assert "just one person" in i.text, i.text
    assert i.response.sent[0]["ephemeral"] is True
    print("  said so quietly rather than shipping somebody with themselves OK")

    i = FakeInteraction(alex, guild_members=everyone)
    await cog.ship.callback(cog, i, sam)
    assert f"{F.compatibility(1, 2)}%" in i.text, i.text
    print("  and defaults the second person to whoever asked OK")

    print("\n=== you can't marry yourself, a bot, or twice ===")
    i = FakeInteraction(alex, guild_members=everyone)
    await cog.marry.callback(cog, i, alex)
    assert "no" in i.text.lower() and not DB["marriages"].docs
    print("  self: refused")

    i = FakeInteraction(alex, guild_members=everyone)
    await cog.marry.callback(cog, i, robot)
    assert "bots don't marry" in i.text, i.text
    assert not DB["marriages"].docs
    print("  a bot: refused")

    print("\n=== a proposal is a button only its target can press ===")
    i = FakeInteraction(alex, guild_members=everyone)
    await cog.marry.callback(cog, i, sam)
    view = i.response.sent[0]["view"]
    ids = [b.custom_id for b in view.children]
    assert ids == ["marry:1:2:yes", "marry:1:2:no"], ids
    assert view.timeout is None, "it has to outlive the process that drew it"
    print(f"  {ids[0]} and {ids[1]}, with no timeout OK")

    # The score is shown before they answer, which is the point of putting it there.
    proposal = i.response.sent[0]["embed"]
    assert f"{F.compatibility(1, 2)}%" in proposal.fields[0].value, proposal.fields[0].value
    assert "❤️" in proposal.fields[0].value or "🤍" in proposal.fields[0].value
    assert "Sam" in (proposal.footer.text or ""), proposal.footer.text
    print("  and it shows the score, and whose answer it is, before anyone clicks OK")

    # Somebody else pressing accept must not marry anyone.
    outsider = FakeInteraction(kit, custom_id="marry:1:2:yes", guild_members=everyone)
    await cog.on_interaction(outsider)
    assert "isn't yours to answer" in outsider.text, outsider.text
    assert not DB["marriages"].docs, "a third party must not be able to accept"
    print("  a bystander pressing accept changes nothing OK")

    # Nor may the proposer accept their own proposal.
    selfish = FakeInteraction(alex, custom_id="marry:1:2:yes", guild_members=everyone)
    await cog.on_interaction(selfish)
    assert not DB["marriages"].docs, "the proposer must not be able to accept for them"
    print("  and neither can the person who asked OK")

    print("\n=== the target accepting is what marries them ===")
    accept = FakeInteraction(sam, custom_id="marry:1:2:yes", guild_members=everyone)
    await cog.on_interaction(accept)
    assert len(DB["marriages"].docs) == 1, DB["marriages"].docs
    wed = DB["marriages"].docs[0]
    assert wed["partners"] == [1, 2], "stored sorted, so a pair reads the same either way"
    assert wed["guild_id"] == GUILD
    assert "Married" in accept.text, accept.text
    # The proposal buttons have to go, or it can be accepted again.
    assert accept.response.edited[0]["view"] is None
    print("  married, stored sorted, and the buttons removed OK")

    print("\n=== and neither of them can marry again ===")
    for asker, target in ((alex, kit), (sam, kit)):
        i = FakeInteraction(asker, guild_members=everyone)
        await cog.marry.callback(cog, i, target)
        assert "already married" in i.text, i.text
    assert len(DB["marriages"].docs) == 1
    # Including from the other direction: proposing to somebody who is taken.
    i = FakeInteraction(kit, guild_members=everyone)
    await cog.marry.callback(cog, i, sam)
    assert "already married" in i.text, i.text
    print("  refused from both sides OK")

    print("\n=== a stale proposal can't sneak past that ===")
    # The proposal was made before either married. Accepting it later must re-check, or an
    # old button is a way to end up married twice.
    stale = FakeInteraction(kit, custom_id="marry:2:3:yes", guild_members=everyone)
    await cog.on_interaction(stale)
    assert len(DB["marriages"].docs) == 1, "a stale accept must not add a second marriage"
    assert "married somebody else" in stale.text, stale.text
    print("  re-checked at the moment of accepting, not only when asked OK")

    print("\n=== declining says so and marries nobody ===")
    i = FakeInteraction(alex, guild_members=everyone)
    await cog.marry.callback(cog, i, kit)     # alex is married, so this is refused
    decline = FakeInteraction(kit, custom_id="marry:1:3:no", guild_members=everyone)
    await cog.on_interaction(decline)
    assert "said no" in decline.text, decline.text
    assert len(DB["marriages"].docs) == 1
    assert decline.response.edited[0]["view"] is None
    print("  turned down, buttons gone, nothing written OK")

    print("\n=== divorce, and only your own ===")
    i = FakeInteraction(kit, guild_members=everyone)
    await cog.divorce.callback(cog, i)
    assert "aren't married" in i.text, i.text
    assert len(DB["marriages"].docs) == 1, "somebody unmarried must not end someone else's"

    print("\n=== shipping a married pair says so ===")
    i = FakeInteraction(alex, guild_members=everyone)
    await cog.ship.callback(cog, i, sam)
    assert "actually married" in i.text, i.text
    # And a pair who aren't must not get that line.
    j = FakeInteraction(alex, guild_members=everyone)
    await cog.ship.callback(cog, j, kit)
    assert "actually married" not in j.text, j.text
    print("  the joke knows when it's wrong OK")

    i = FakeInteraction(sam, guild_members=everyone)
    await cog.divorce.callback(cog, i)
    assert not DB["marriages"].docs, "either partner can end it"
    assert "no longer married" in i.text, i.text
    footer = i.response.sent[0]["embed"].footer.text
    assert "lasted" in footer, footer
    print(f"  only a partner can, either partner can, and it says how long: {footer}")

    print("\n=== would you rather counts one vote each ===")
    i = FakeInteraction(alex, guild_members=everyone)
    await cog.wouldyourather.callback(cog, i)
    poll_id = i._original.id
    view = i.response.sent[0]["view"]
    assert [b.custom_id for b in view.children] == ["wyr:a", "wyr:b"]
    stored = DB["wyr_polls"].find_one({"_id": poll_id})
    assert stored and stored["a"] == [] and stored["b"] == []
    assert (stored["left"], stored["right"]) in F.QUESTIONS
    print(f"  posted, recorded, question is one of the {len(F.QUESTIONS)} OK")

    vote = FakeInteraction(alex, custom_id="wyr:a", message_id=poll_id,
                           guild_members=everyone)
    await cog.on_interaction(vote)
    stored = DB["wyr_polls"].find_one({"_id": poll_id})
    assert stored["a"] == [1] and stored["b"] == []

    # Voting again for the same side must not count twice.
    again = FakeInteraction(alex, custom_id="wyr:a", message_id=poll_id,
                            guild_members=everyone)
    await cog.on_interaction(again)
    stored = DB["wyr_polls"].find_one({"_id": poll_id})
    assert stored["a"] == [1], stored
    print("  pressing the same button twice is still one vote OK")

    print("\n=== and changing your mind moves it rather than adding one ===")
    switch = FakeInteraction(alex, custom_id="wyr:b", message_id=poll_id,
                             guild_members=everyone)
    await cog.on_interaction(switch)
    stored = DB["wyr_polls"].find_one({"_id": poll_id})
    assert stored["a"] == [] and stored["b"] == [1], stored
    footer = switch.response.edited[0]["embed"].footer.text
    assert "1 vote" in footer, footer
    print(f"  moved sides, footer reads: {footer}")

    sam_votes = FakeInteraction(sam, custom_id="wyr:a", message_id=poll_id,
                                guild_members=everyone)
    await cog.on_interaction(sam_votes)
    footer = sam_votes.response.edited[0]["embed"].footer.text
    assert "2 votes" in footer, footer
    assert "50%" in footer, footer
    print(f"  two people, two sides: {footer}")

    print("\n=== a poll the database has forgotten says so ===")
    gone = FakeInteraction(alex, custom_id="wyr:a", message_id=999999,
                           guild_members=everyone)
    await cog.on_interaction(gone)
    assert "too old" in gone.text, gone.text
    assert gone.response.sent[0]["ephemeral"] is True
    print("  told quietly rather than the button doing nothing OK")

    print("\n=== userinfo says who they are, three different ways ===")
    DB["ratings"].docs.append({"guild_id": GUILD, "user_id": 1, "rating": 9})
    DB["marriages"].docs.append({"guild_id": GUILD, "partners": [1, 2],
                                 "since": datetime.datetime.now(datetime.timezone.utc)})
    DB["memberships"].docs.extend([
        {"guild_id": GUILD, "user_id": 1, "invite_code": "reddit2024",
         "inviter_name": "marcus",
         "joined_at": datetime.datetime.now(datetime.timezone.utc)},
        {"guild_id": GUILD, "user_id": 1, "invite_code": None, "inviter_name": None,
         "joined_at": datetime.datetime.now(datetime.timezone.utc)
                      - datetime.timedelta(days=200), "left_at":
             datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=100)},
    ])
    boss = member(1, "Alex", nick="Big Al", boosting=True,
                  roles=[role("Admins", value=0xFF0000)],
                  perms=permissions(administrator=True, ban_members=True),
                  badges=("active_developer", "early_supporter"))
    i = FakeInteraction(boss, guild_members=[boss, sam, kit, robot])
    await cog.userinfo.callback(cog, i, boss)
    body = i.text + "\n" + "\n".join(
        f"{f.name} {f.value}" for f in i.followup.sent[0]["embed"].fields)
    assert i.response.deferred, "it reads the database, so it defers first"
    # The nickname, the handle and the real name are three different things.
    assert "Big Al" in body and "@alex" in body, body
    assert "days old" in body and "days ago" in body, "account age and time here"
    assert "Member number" in body
    print("  nickname, handle, ages and join position OK")

    print("\n=== and everything it can add on top ===")
    for expected in ("Administrator", "Active Developer", "Early Supporter", "@Admins",
                     "Boosting since", "Owns this server",
                     "9/10", "Married to <@2>",
                     "reddit2024", "marcus", "joined **2** times"):
        assert expected in body, (expected, body)
    # Administrator makes every other permission true, so listing them alongside says nothing.
    assert "Ban" not in body.split("Can")[1].split("Badges")[0], body
    print("  permissions, badges, roles, boost, rating, marriage, invite and rejoins OK")

    print("\n=== warnings are only shown to somebody who could already look them up ===")
    DB["mod_cases"].docs.append({"guild_id": GUILD, "user_id": 2, "case_id": 1})
    nosy = FakeInteraction(kit, guild_members=everyone)          # no permissions
    await cog.userinfo.callback(cog, nosy, sam)
    assert "moderation case" not in nosy.text, nosy.text

    mod = member(3, "Kit", perms=permissions(moderate_members=True))
    allowed = FakeInteraction(mod, guild_members=[alex, sam, mod, robot])
    await cog.userinfo.callback(cog, allowed, sam)
    seen = "\n".join(f"{f.name} {f.value}"
                     for f in allowed.followup.sent[0]["embed"].fields)
    assert "**1** moderation case" in seen, seen
    assert "only you can see this" in seen, "and it says the rest of the room can't"
    print("  hidden from everyone else, shown to a moderator OK")

    print("\n=== and it holds up for somebody with nothing on record ===")
    i = FakeInteraction(alex, guild_members=everyone)
    await cog.userinfo.callback(cog, i, kit)
    body = i.text + "\n".join(f"{f.name} {f.value}"
                              for f in i.followup.sent[0]["embed"].fields)
    assert "Kit" in body and "/10" not in body and "Married" not in body
    assert "None yet" in body, "no roles has to say so rather than being blank"
    print("  no rating, no marriage, no roles, no crash OK")

    print("\n=== serverinfo covers the furniture and the rules ===")
    i = FakeInteraction(alex, guild_members=everyone)
    await cog.serverinfo.callback(cog, i)
    body = "\n".join(f"{f.name} {f.value}"
                     for f in i.followup.sent[0]["embed"].fields) + i.text
    for expected in ("Test Server", "Owner", "Members", "2 text", "1 voice", "1 forum",
                     "2 threads", "1 category", "Roles", "emoji", "stickers",
                     "Verification: Medium", "Media scanning: Everyone",
                     "Two factor for moderators: on",
                     "Community", "Welcome screen", "discord.gg/testing"):
        assert expected in body, (expected, body)
    # Singulars, because "1 bots" and "1 categories" are the kind of thing people notice.
    assert "1 bots" not in body and "1 categories" not in body, body
    print("  channels, emoji, safety levels, features and the vanity url OK")

    print("\n=== the boost meter shows how far off the next level is ===")
    assert "of 7 for level 2" in body, body
    assert "▰" in body and "▱" in body
    print("  a bar plus the number needed, not just the tier OK")

    print("\n=== and it reports what this bot has watched happen ===")
    for expected in ("joins recorded", "still here", "last 7 days",
                     "Best invite this week", "reddit2024", "Rated **9.0/10**"):
        assert expected in body, (expected, body)
    print("  joins, leavers, the week's best invite and the average rating OK")

    print("\n=== a server it has never seen a join in says so ===")
    kept = DB["memberships"].docs
    DB.c["memberships"] = FakeColl("memberships")
    i = FakeInteraction(alex, guild_members=everyone)
    await cog.serverinfo.callback(cog, i)
    body = "\n".join(f"{f.name} {f.value}"
                     for f in i.followup.sent[0]["embed"].fields)
    assert "Nothing yet" in body, body
    assert "joins recorded" not in body
    DB.c["memberships"].docs = kept
    print("  an explanation rather than a row of zeroes OK")

    print("\n=== the eight ball answers, and leans yes ===")
    moods = [m for m, _ in F.EIGHT_BALL]
    assert len(F.EIGHT_BALL) == 20, len(F.EIGHT_BALL)
    # An eight ball that says no half the time stops being fun on the second question.
    assert moods.count("yes") == 10 and moods.count("no") == 5, moods
    assert set(moods) <= set(F.EIGHT_BALL_COLOURS), set(moods)
    i = FakeInteraction(alex, guild_members=everyone)
    await cog.eight_ball.callback(cog, i, "will this work")
    card = i.response.sent[0]["embed"]
    assert card.author.name == "will this work", card.author.name
    assert any(answer in card.description for _, answer in F.EIGHT_BALL), card.description
    print("  20 answers, 10 of them yes, and it quotes the question OK")

    print("\n=== and a long question can't turn it into a billboard ===")
    i = FakeInteraction(alex, guild_members=everyone)
    await cog.eight_ball.callback(cog, i, "x" * 900)
    assert len(i.response.sent[0]["embed"].author.name) <= 256
    print("  cut to fit rather than refused by Discord OK")

    print("\n=== rock paper scissors knows who beats what ===")
    for throw in F.THROWS:
        assert cog._outcome(throw, throw) == 0, throw
        assert cog._outcome(throw, F.BEATS[throw]) == 1, throw
        assert cog._outcome(F.BEATS[throw], throw) == -1, throw
    print("  every pairing, both ways round OK")

    print("\n=== against the bot, and only for whoever asked ===")
    i = FakeInteraction(alex, guild_members=everyone)
    await cog.rps.callback(cog, i, None)
    ids = [b.custom_id for b in i.response.sent[0]["view"].children]
    assert ids == ["rps:solo:rock:1", "rps:solo:paper:1", "rps:solo:scissors:1"], ids

    butt = FakeInteraction(sam, custom_id="rps:solo:rock:1", guild_members=everyone)
    await cog.on_interaction(butt)
    assert "somebody else's game" in butt.text, butt.text
    assert not butt.response.edited, "and it must not resolve the game"

    mine = FakeInteraction(alex, custom_id="rps:solo:rock:1", guild_members=everyone)
    await cog.on_interaction(mine)
    result = mine.response.edited[0]["embed"]
    assert result.title in ("You win", "A draw", "I win"), result.title
    assert "Rock" in result.description, result.description
    assert mine.response.edited[0]["view"] is None, "the buttons have to go"
    print(f"  a stranger is turned away, the owner gets: {result.title}")

    print("\n=== a duel waits for both, and tells neither ===")
    i = FakeInteraction(alex, guild_members=everyone)
    await cog.rps.callback(cog, i, sam)
    game_id = i._original.id
    ids = [b.custom_id for b in i.response.sent[0]["view"].children]
    assert all(cid.startswith("rps:duel:") and cid.endswith(":0") for cid in ids), ids
    stored = DB["rps_games"].find_one({"_id": game_id})
    assert stored and stored["players"] == [1, 2] and stored["picks"] == {}

    outsider = FakeInteraction(kit, custom_id="rps:duel:rock:0", message_id=game_id,
                               guild_members=everyone)
    await cog.on_interaction(outsider)
    assert "not in this one" in outsider.text, outsider.text

    one = FakeInteraction(alex, custom_id="rps:duel:rock:0", message_id=game_id,
                          guild_members=everyone)
    await cog.on_interaction(one)
    # Privately, or pressing a button would tell the other player what you picked.
    assert one.response.sent and one.response.sent[0]["ephemeral"] is True
    assert not one.response.edited, "nothing on the message until both have gone"
    assert "Waiting" in one.text, one.text
    print("  an outsider is refused, and the first pick is kept quiet OK")

    print("\n=== pressing twice doesn't change your mind ===")
    twice = FakeInteraction(alex, custom_id="rps:duel:paper:0", message_id=game_id,
                            guild_members=everyone)
    await cog.on_interaction(twice)
    assert "already gone" in twice.text, twice.text
    assert DB["rps_games"].find_one({"_id": game_id})["picks"] == {"1": "rock"}
    print("  the first pick stands OK")

    print("\n=== and the second pick settles it ===")
    two = FakeInteraction(sam, custom_id="rps:duel:scissors:0", message_id=game_id,
                          guild_members=everyone)
    await cog.on_interaction(two)
    final = two.response.edited[0]["embed"]
    # Rock beats scissors, so the challenger takes it.
    assert "<@1> wins" in final.description, final.description
    assert "Rock" in final.description and "Scissors" in final.description
    assert two.response.edited[0]["view"] is None
    assert DB["rps_games"].find_one({"_id": game_id}) is None, "a finished game is cleared"
    print("  revealed both, named the winner, and cleaned up OK")

    print("\n=== a game the database has forgotten says so ===")
    lost = FakeInteraction(alex, custom_id="rps:duel:rock:0", message_id=555444,
                           guild_members=everyone)
    await cog.on_interaction(lost)
    assert "too old" in lost.text, lost.text
    print("  told quietly rather than the button doing nothing OK")

    print("\n=== and you can't duel yourself or a bot ===")
    for target, expect in ((alex, "at what cost"), (robot, "Leave it out")):
        i = FakeInteraction(alex, guild_members=everyone)
        await cog.rps.callback(cog, i, target)
        assert expect in i.text, (target.display_name, i.text)
        assert i.response.sent[0]["ephemeral"] is True
    print("  both refused OK")

    print("\n=== roleinfo ===")
    top = role("Bot", position=90)              # the bot's own highest
    admins = role("Admins", value=0xFF0000, position=5)
    admins.hoist = True
    admins.members = [alex, sam]
    admins.permissions = permissions(ban_members=True, manage_messages=True)

    i = FakeInteraction(alex, guild_members=everyone)
    i.guild.me = types.SimpleNamespace(top_role=top)
    await cog.roleinfo.callback(cog, i, admins)
    card = i.response.sent[0]["embed"]
    body = f"{card.author.name} {card.description} " + " ".join(
        f"{f.name} {f.value}" for f in card.fields)
    for expected in ("Admins", "#ff0000", "Has it (2)", "Alex", "Sam", "Ban",
                     "Shown separately", "Only people with Mention Everyone"):
        assert expected in body, (expected, body)
    print("  colour, holders, permissions and the two display flags OK")

    print("\n=== and it warns when the bot couldn't hand it out ===")
    managed = role("Integration", position=9)
    managed.managed = True
    managed.permissions = permissions()
    i = FakeInteraction(alex, guild_members=everyone)
    i.guild.me = types.SimpleNamespace(top_role=top)
    await cog.roleinfo.callback(cog, i, managed)
    body = " ".join(f"{f.name} {f.value}" for f in i.response.sent[0]["embed"].fields)
    assert "Managed by an integration" in body, body
    assert "Nobody yet" in body, "an empty role has to say so"

    # And the other reason the bot can't hand one out: it sits too high.
    above = role("Owner only", position=99)
    above.permissions = permissions()
    i = FakeInteraction(alex, guild_members=everyone)
    i.guild.me = types.SimpleNamespace(top_role=top)
    await cog.roleinfo.callback(cog, i, above)
    body = " ".join(f"{f.name} {f.value}" for f in i.response.sent[0]["embed"].fields)
    assert "above my highest role" in body, body
    print("  both reasons the dashboard would grey it out, said in Discord OK")

    print("\n=== avatar, including a server-only one ===")
    plain = member(11, "Plain")
    plain.guild_avatar, plain.avatar = None, None
    i = FakeInteraction(alex, guild_members=everyone)
    await cog.avatar.callback(cog, i, plain)
    card = i.response.sent[0]["embed"]
    assert card.image.url == plain.display_avatar.url
    assert "Their account one" not in (card.description or "")

    dual = member(12, "Dual")
    dual.guild_avatar = types.SimpleNamespace(url="http://x/server.png")
    dual.avatar = types.SimpleNamespace(url="http://x/global.png")
    i = FakeInteraction(alex, guild_members=everyone)
    await cog.avatar.callback(cog, i, dual)
    card = i.response.sent[0]["embed"]
    assert "Their account one" in card.description, card.description
    assert "just for this server" in (card.footer.text or "")
    print("  one link normally, both when they differ OK")

    print("\n=== banner, whether or not they have one ===")
    class FetchedUser:
        def __init__(self, banner=None, accent=None):
            self.banner = banner
            self.accent_colour = accent

    bot.fetch_user = stub_fetch(FetchedUser(
        banner=types.SimpleNamespace(url="http://x/banner.png")))
    i = FakeInteraction(alex, guild_members=everyone)
    await cog.banner.callback(cog, i, sam)
    card = i.followup.sent[0]["embed"]
    assert card.image.url == "http://x/banner.png"
    assert "Full size" in card.description

    bot.fetch_user = stub_fetch(FetchedUser())
    i = FakeInteraction(alex, guild_members=everyone)
    await cog.banner.callback(cog, i, sam)
    assert "hasn't set a banner" in i.followup.sent[0]["embed"].description
    print("  the image when there is one, a sentence when there isn't OK")

    print("\n=== every collection it touches is per guild ===")
    # Both are named in Lifecycle so a server removing the bot takes them with it. The
    # lifecycle suite enforces that; this checks the names haven't drifted apart.
    import importlib
    lifecycle = importlib.import_module("Cogs.Lifecycle")
    for name in ("marriages", "wyr_polls", "rps_games"):
        assert name in lifecycle.BY_GUILD_ID, f"{name} would be left behind on removal"
        assert DB.c.get(name) is not None, f"{name} was never written to by these tests"
    print("  marriages, wyr_polls and rps_games all cleaned up on removal OK")

    print("\n=== a click that isn't ours is left alone ===")
    other = FakeInteraction(alex, custom_id="rr:abc:123", guild_members=everyone)
    await cog.on_interaction(other)
    assert not other.response.sent and not other.response.edited
    print("  role button ids pass straight through OK")

    print("\nALL CHECKS PASSED")


asyncio.run(main())
