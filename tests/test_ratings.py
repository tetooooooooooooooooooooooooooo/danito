"""Ratings: custom_id capture, upsert-not-duplicate, /ratings maths and embeds."""
import pathlib as _pathlib
# Resolved from this file so the suite runs from a clone, on any machine, from any cwd.
ROOT = _pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / "src")
WEB_DIR = str(ROOT / "web")
import asyncio, datetime, sys, types

sys.path.insert(0, SRC_DIR)

# Fake Mongo good enough for the queries this cog makes.
class FakeColl:
    def __init__(self): self.docs = []
    def create_index(self, *a, **k): pass
    def _ref(self, q):
        return next(iter(self.find(q)), None)
    def find_one(self, q, *a, **k):
        # pymongo decodes a fresh dict from BSON, so callers get a detached copy.
        hit = self._ref(q)
        return dict(hit) if hit is not None else None
    def find(self, q=None, *a, **k):
        q = q or {}
        return [d for d in self.docs
                if all(d.get(k2) == v for k2, v in q.items() if not isinstance(v, dict))]
    def count_documents(self, q): return len(list(self.find(q)))
    def update_one(self, q, ops, upsert=False):
        hit = self._ref(q)
        if hit is None:
            if not upsert: return types.SimpleNamespace(matched_count=0)
            hit = dict(q); hit.update(ops.get("$setOnInsert", {})); self.docs.append(hit)
        hit.update(ops.get("$set", {}))
        return types.SimpleNamespace(matched_count=1)

class FakeDB:
    def __init__(self): self.c = {}
    def __getitem__(self, n): return self.c.setdefault(n, FakeColl())

DB = FakeDB()
stub = types.ModuleType("Database"); stub.get_bot_database = lambda c: DB
sys.modules["Database"] = stub
for n in ("pymongo", "certifi", "dotenv"):
    m = types.ModuleType(n)
    if n == "pymongo": m.MongoClient = object
    if n == "certifi": m.where = lambda: ""
    if n == "dotenv": m.load_dotenv = lambda *a, **k: None
    sys.modules[n] = m

import discord
from discord.ext import commands

GUILD, CHAN, MSG = 100, 200, 300


class Resp:
    def __init__(self): self.sent = None
    def is_done(self): return self.sent is not None
    async def send_message(self, content=None, **kw): self.sent = content


def click(user_id, custom_id):
    return types.SimpleNamespace(
        type=discord.InteractionType.component,
        message=types.SimpleNamespace(
            id=MSG, components=[], author=types.SimpleNamespace(id=42),
            edit=lambda **kw: asyncio.sleep(0)),
        guild=types.SimpleNamespace(id=GUILD),
        channel=types.SimpleNamespace(id=CHAN),
        user=types.SimpleNamespace(id=user_id),
        data={"custom_id": custom_id},
        response=Resp(),
    )


async def main():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    bot._connection.user = types.SimpleNamespace(
        id=42, name="soundcord", avatar=types.SimpleNamespace(url="https://e.com/a.png"))
    bot.MongoClient = object()
    await bot.load_extension("Cogs.Ratings")
    cog = bot.get_cog("Server Ratings")
    assert cog is not None, f"cog missing; have {list(bot.cogs)}"

    print("=== commands ===")
    for c in bot.tree.walk_commands():
        print(f"  /{c.qualified_name} - {c.description}")
    names = {c.name for c in bot.tree.walk_commands()}
    assert names == {"setchannel", "ratings", "forcesurvey", "discoveryhelp"}, names

    # survey message must be registered or clicks are ignored
    DB["servers"].docs.append(
        {"guild_id": GUILD, "discovery_channel": CHAN, "discovery_message": MSG})

    print("\n=== capturing clicks ===")
    i = click(1, "rating:8")
    await cog.on_interaction(i)
    print(f"  new vote     -> {i.response.sent}")
    assert "8/10" in i.response.sent
    assert len(DB["ratings"].docs) == 1, DB["ratings"].docs
    assert DB["ratings"].docs[0]["rating"] == 8

    # same person, same score
    i = click(1, "rating:8")
    await cog.on_interaction(i)
    print(f"  same again   -> {i.response.sent}")
    assert "already" in i.response.sent.lower()
    assert len(DB["ratings"].docs) == 1, "should not duplicate"

    # same person changes their mind
    i = click(1, "rating:3")
    await cog.on_interaction(i)
    print(f"  changed mind -> {i.response.sent}")
    assert "Updated" in i.response.sent and "was 8" in i.response.sent
    assert len(DB["ratings"].docs) == 1, "still one row per member"
    assert DB["ratings"].docs[0]["rating"] == 3
    print("  -> one row per member, updates in place OK")

    # a legacy survey button with Discord's random id
    i = click(2, "b3f9a1c2-random")
    await cog.on_interaction(i)
    print(f"  legacy id    -> {i.response.sent[:70]}...")
    assert "older version" in i.response.sent
    assert len(DB["ratings"].docs) == 1, "unparseable id must not save"

    # out-of-range must be rejected outright
    i = click(3, "rating:99")
    await cog.on_interaction(i)
    assert i.response.sent is None, "out-of-range should be ignored"
    assert len(DB["ratings"].docs) == 1
    print("  -> legacy ids and out-of-range scores rejected OK")

    # clicks on an unrelated message are ignored
    stray = click(4, "rating:5"); stray.message = types.SimpleNamespace(id=999)
    await cog.on_interaction(stray)
    assert stray.response.sent is None, "unrelated message should be ignored"
    print("  -> clicks on other messages ignored OK")

    # ---- /ratings maths ----
    print("\n=== /ratings ===")
    DB["ratings"].docs.clear()
    now = datetime.datetime.now(datetime.timezone.utc)
    spread = [10, 10, 10, 9, 9, 8, 7, 5, 3, 1]
    for uid, score in enumerate(spread, start=10):
        DB["ratings"].docs.append({"guild_id": GUILD, "user_id": uid, "rating": score,
                                   "created_at": now, "updated_at": now})

    captured = {}
    class FU:
        async def send(self, **kw): captured.update(kw)
    guild = types.SimpleNamespace(id=GUILD, name="Test Server", icon=None,
                                  get_channel=lambda i: None,
                                  me=types.SimpleNamespace(guild_permissions=types.SimpleNamespace(manage_roles=True)))
    inter = types.SimpleNamespace(
        guild=guild, followup=FU(),
        response=types.SimpleNamespace(defer=lambda **k: asyncio.sleep(0)))
    await cog.ratings.callback(cog, inter)

    e = captured["embed"]
    print(f"  {e.title}")
    print(f"  {e.description}")
    for f in e.fields:
        print(f"  [{f.name}] {f.value}")

    expected_avg = sum(spread) / len(spread)      # 7.2
    assert f"{expected_avg:.1f}" in e.description, e.description
    promoters = sum(1 for s in spread if s >= 9)   # 5
    detractors = sum(1 for s in spread if s <= 6)  # 3
    expected_nps = round((promoters - detractors) / len(spread) * 100)   # +20
    vals = " ".join(f.value for f in e.fields)
    assert f"{expected_nps:+d}" in vals, f"expected {expected_nps:+d} in {vals}"
    assert str(promoters) in vals and str(detractors) in vals
    assert len(e) <= 6000, len(e)
    for f in e.fields:
        assert len(f.value) <= 1024, f"{f.name} is {len(f.value)} chars"
    print(f"  -> average {expected_avg:.1f}, net {expected_nps:+d}, embed limits OK")

    # empty state must not divide by zero
    DB["ratings"].docs.clear()
    captured.clear()
    await cog.ratings.callback(cog, inter)
    assert "No ratings yet" in captured["embed"].description
    print("  -> empty state handled without dividing by zero OK")

    print("\nALL CHECKS PASSED")

asyncio.run(main())
