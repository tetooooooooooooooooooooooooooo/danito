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


class FakeColl:
    def __init__(self, name):
        self.name = name
        self.docs = []
        self._ids = 0

    def create_index(self, *a, **k): pass

    def _match(self, doc, query):
        for key, value in query.items():
            if key == "partners" and isinstance(doc.get(key), list):
                if value not in doc[key]:
                    return False
            elif isinstance(value, dict) and "$gte" in value:
                if not (doc.get(key) is not None and doc[key] >= value["$gte"]):
                    return False
            elif doc.get(key) != value:
                return False
        return True

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
                out.append(f"{embed.title or ''} {embed.description or ''}")
                for field in embed.fields:
                    out.append(f"{field.name} {field.value}")
        return "\n".join(out)


def member(uid, name="someone", bot=False, joined=True):
    return types.SimpleNamespace(
        id=uid, bot=bot, display_name=name, name=name,
        mention=f"<@{uid}>",
        colour=types.SimpleNamespace(value=0),
        display_avatar=types.SimpleNamespace(url="http://x/a.png"),
        created_at=datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc),
        joined_at=(datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
                   if joined else None),
        premium_since=None,
        roles=[types.SimpleNamespace(mention="@everyone", is_default=lambda: True)])


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
        self.guild = types.SimpleNamespace(
            id=GUILD, name="Test Server", owner_id=1, description=None,
            member_count=3, members=list(guild_members) or [user],
            created_at=datetime.datetime(2021, 6, 1, tzinfo=datetime.timezone.utc),
            icon=None, features=["COMMUNITY", "VANITY_URL"],
            text_channels=[1, 2], voice_channels=[3], categories=[4],
            roles=[1, 2, 3], premium_subscription_count=2, premium_tier=1,
            get_member=lambda uid: next((m for m in guild_members if m.id == uid), None))
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

    print("\n=== userinfo, including the two things only this bot knows ===")
    DB["ratings"].docs.append({"guild_id": GUILD, "user_id": 1, "rating": 9})
    DB["marriages"].docs.append({"guild_id": GUILD, "partners": [1, 2],
                                 "since": datetime.datetime.now(datetime.timezone.utc)})
    i = FakeInteraction(alex, guild_members=everyone)
    await cog.userinfo.callback(cog, i, alex)
    body = i.text
    assert i.response.deferred, "it reads the database, so it defers first"
    assert "Alex" in body and "9/10" in body, body
    assert "Married to <@2>" in body, body
    assert "Member number" in body
    print("  name, rating, marriage and member number OK")

    print("\n=== and it works for somebody with none of that ===")
    i = FakeInteraction(alex, guild_members=everyone)
    await cog.userinfo.callback(cog, i, kit)
    assert "Kit" in i.text and "/10" not in i.text
    print("  no rating, no marriage, no crash OK")

    print("\n=== serverinfo ===")
    DB["memberships"].docs.append({
        "guild_id": GUILD,
        "joined_at": datetime.datetime.now(datetime.timezone.utc)})
    i = FakeInteraction(alex, guild_members=everyone)
    await cog.serverinfo.callback(cog, i)
    body = i.text
    for expected in ("Test Server", "Members", "Channels", "Roles", "Community",
                     "Joined this week"):
        assert expected in body, (expected, body)
    print("  counts, features and this week's joins OK")

    print("\n=== every collection it touches is per guild ===")
    # Both are named in Lifecycle so a server removing the bot takes them with it. The
    # lifecycle suite enforces that; this checks the names haven't drifted apart.
    import importlib
    lifecycle = importlib.import_module("Cogs.Lifecycle")
    for name in ("marriages", "wyr_polls"):
        assert name in lifecycle.BY_GUILD_ID, f"{name} would be left behind on removal"
        assert DB.c.get(name) is not None, f"{name} was never written to by these tests"
    print("  marriages and wyr_polls both cleaned up on removal OK")

    print("\n=== a click that isn't ours is left alone ===")
    other = FakeInteraction(alex, custom_id="rr:abc:123", guild_members=everyone)
    await cog.on_interaction(other)
    assert not other.response.sent and not other.response.edited
    print("  role button ids pass straight through OK")

    print("\nALL CHECKS PASSED")


asyncio.run(main())
