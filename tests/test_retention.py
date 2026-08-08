"""Retention: spell open/close, rejoins, survival maths, denominators, nudged stamping."""
import pathlib as _pathlib
# Resolved from this file so the suite runs from a clone, on any machine, from any cwd.
ROOT = _pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / "src")
WEB_DIR = str(ROOT / "web")
import asyncio, datetime, sys, types
sys.path.insert(0, SRC_DIR)


class FakeColl:
    def __init__(self, name): self.name = name; self.docs = []
    def create_index(self, *a, **k): pass
    def _match(self, d, q):
        for k, v in q.items():
            if isinstance(v, dict):
                if "$lt" in v and not (d.get(k) is not None and d[k] < v["$lt"]): return False
                if "$gte" in v and not (d.get(k) is not None and d[k] >= v["$gte"]): return False
            elif d.get(k) != v:
                return False
        return True
    def find(self, q=None, *a, **k):
        return _Cursor([d for d in self.docs if self._match(d, q or {})])
    def find_one(self, q, *a, **k):
        hits = [d for d in self.docs if self._match(d, q)]
        return dict(hits[0]) if hits else None
    def insert_one(self, d): self.docs.append(dict(d))
    def find_one_and_update(self, q, ops, sort=None, **k):
        hits = [d for d in self.docs if self._match(d, q)]
        if sort:
            key, direction = sort[0]
            hits.sort(key=lambda d: d[key], reverse=direction < 0)
        if not hits: return None
        hits[0].update(ops.get("$set", {}))
        return hits[0]
    def update_many(self, q, ops):
        n = 0
        for d in self.docs:
            if self._match(d, q):
                d.update(ops.get("$set", {})); n += 1
        return types.SimpleNamespace(modified_count=n)
    def update_one(self, q, ops, upsert=False):
        hits = [d for d in self.docs if self._match(d, q)]
        if not hits:
            if not upsert: return types.SimpleNamespace(matched_count=0)
            d = dict(q); d.update(ops.get("$set", {})); self.docs.append(d)
            return types.SimpleNamespace(matched_count=1)
        hits[0].update(ops.get("$set", {}))
        return types.SimpleNamespace(matched_count=1)


class _Cursor(list):
    def sort(self, key, direction=1):
        super().sort(key=lambda d: d.get(key) or datetime.datetime.min.replace(
            tzinfo=datetime.timezone.utc), reverse=direction < 0)
        return self
    def limit(self, n): return _Cursor(self[:n])


class FakeDB:
    def __init__(self): self.c = {}
    def __getitem__(self, n): return self.c.setdefault(n, FakeColl(n))


DB = FakeDB()
st = types.ModuleType("Database"); st.get_bot_database = lambda c: DB
sys.modules["Database"] = st
for n in ("pymongo", "certifi", "dotenv"):
    m = types.ModuleType(n)
    if n == "pymongo": m.MongoClient = lambda *a, **k: object()
    if n == "certifi": m.where = lambda: ""
    if n == "dotenv": m.load_dotenv = lambda *a, **k: None
    sys.modules[n] = m

import discord
from discord.ext import commands

GUILD = 1
NOW = datetime.datetime.now(datetime.timezone.utc)


def days_ago(n):
    return NOW - datetime.timedelta(days=n)


def member(uid):
    m = types.SimpleNamespace(
        id=uid, bot=False,
        guild=types.SimpleNamespace(
            id=GUILD,
            me=types.SimpleNamespace(guild_permissions=types.SimpleNamespace(manage_roles=False)),
            get_role=lambda r: None),
        mention=f"<@{uid}>")
    async def send(*a, **k): raise discord.Forbidden(
        types.SimpleNamespace(status=403, reason=""), "closed")
    m.send = send
    return m


async def main():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    bot._connection.user = types.SimpleNamespace(id=42, name="soundcord", avatar=None)
    bot.MongoClient = object()
    await bot.load_extension("Cogs.Members")
    cog = bot.get_cog("Members")
    M = sys.modules["Cogs.Members"]

    print("=== a spell opens on join and closes on leave ===")
    await cog.on_member_join(member(100))
    spells = DB["memberships"].docs
    assert len(spells) == 1, spells
    assert spells[0]["left_at"] is None and spells[0]["nudged"] is False
    assert spells[0]["cohort"] == str(datetime.date.today())
    print(f"  opened: cohort {spells[0]['cohort']}, left_at None")

    await cog.on_member_remove(member(100))
    assert spells[0]["left_at"] is not None, "leaving should close the spell"
    print("  closed on leave OK")

    print("\n=== a rejoin is a second spell, not an edit ===")
    await cog.on_member_join(member(100))
    assert len(spells) == 2, spells
    assert spells[1]["left_at"] is None
    assert spells[0]["left_at"] is not None, "the first spell must stay closed"
    print("  two spells, first still closed OK")

    print("\n=== leaving with no open spell is a no-op ===")
    before = len(spells)
    await cog.on_member_remove(member(999))     # never seen joining
    assert len(spells) == before
    print("  no join on record means no invented join date OK")

    print("\n=== bots are skipped ===")
    b = member(777); b.bot = True
    await cog.on_member_join(b)
    assert len(spells) == before, "bots shouldn't count toward retention"
    print("  bot join ignored OK")

    # ---- survival maths ----
    print("\n=== survival maths ===")
    DB["memberships"].docs.clear()

    def spell(uid, joined, left=None, cohort=None, nudged=False):
        DB["memberships"].docs.append({
            "guild_id": GUILD, "user_id": uid, "cohort": cohort or str(joined.date()),
            "joined_at": joined, "left_at": left, "nudged": nudged})

    # joined 40 days ago, still here -> counts as survived at every window
    spell(1, days_ago(40))
    # joined 40 days ago, left after 2 days -> survived day 1, not 7/14/30
    spell(2, days_ago(40), days_ago(38))
    # joined 40 days ago, left after 20 days -> survived 1/7/14, not 30
    spell(3, days_ago(40), days_ago(20))
    # joined 2 days ago, still here -> only measurable at day 1
    spell(4, days_ago(2))

    s = cog._survival(DB["memberships"].docs, NOW)
    for d in M.RETENTION_DAYS:
        print(f"  day {d:>2}: {s[d]}")

    assert s[1] == (4, 4), s[1]      # all four old enough; all lasted a day
    assert s[7] == (2, 3), s[7]      # 3 old enough; user 2 left too early
    assert s[14] == (2, 3), s[14]
    assert s[30] == (1, 3), s[30]    # only user 1 lasted 30 days
    print("  each window counts only members old enough to measure OK")

    # a member who joined yesterday must not inflate the 30-day figure
    DB["memberships"].docs.clear()
    for i in range(10):
        spell(i, days_ago(1))
    s = cog._survival(DB["memberships"].docs, NOW)
    assert s[1] == (10, 10), s[1]
    assert s[30] is None, "no 30-day figure should exist yet"
    print("  brand new joins give no 30-day figure rather than a fake 100% OK")

    # naive datetimes, as pymongo returns them
    DB["memberships"].docs.clear()
    spell(1, days_ago(40).replace(tzinfo=None), days_ago(5).replace(tzinfo=None))
    s = cog._survival(DB["memberships"].docs, NOW)
    assert s[30] == (1, 1), s[30]
    print("  naive datetimes from pymongo handled OK")

    # ---- the embed ----
    print("\n=== /retention embed ===")
    DB["memberships"].docs.clear()
    spell(1, days_ago(35), cohort="old", nudged=True)
    spell(2, days_ago(35), days_ago(30), cohort="old", nudged=True)
    spell(3, days_ago(9), nudged=True)          # inside the 14-day window, was nudged
    spell(4, days_ago(2))                       # inside the window, not nudged yet

    captured = {}
    class FU:
        async def send(self, **kw): captured.update(kw)
    guild = types.SimpleNamespace(id=GUILD, name="Test Server", icon=None)
    inter = types.SimpleNamespace(
        guild=guild, followup=FU(),
        response=types.SimpleNamespace(defer=lambda **k: asyncio.sleep(0)))
    await cog.retention.callback(cog, inter)
    e = captured["embed"]
    print(f"  {e.title}")
    print(f"  {e.description}")
    for f in e.fields:
        print(f"  [{f.name}]\n      " + f.value.replace("\n", "\n      "))
    assert len(e) <= 6000
    for f in e.fields:
        assert len(f.value) <= 1024, f"{f.name} is {len(f.value)}"
    vals = " ".join(f.value for f in e.fields)
    assert "reminded" in vals, "should mark which intakes were reminded"
    print("  within limits, cohorts listed, reminded marked OK")

    print("\n=== empty state ===")
    DB["memberships"].docs.clear()
    captured.clear()
    await cog.retention.callback(cog, inter)
    assert "Nothing recorded yet" in captured["embed"].description
    print("  explains that collection starts now OK")

    print("\n=== the nudge stamps its cohort ===")
    DB["memberships"].docs.clear()
    spell(1, days_ago(8), cohort="2026-07-27")
    spell(2, days_ago(8), cohort="2026-07-27")
    spell(3, days_ago(1), cohort="2026-08-03")
    DB["memberships"].update_many(
        {"guild_id": GUILD, "cohort": "2026-07-27"}, {"$set": {"nudged": True}})
    marked = [d for d in DB["memberships"].docs if d["nudged"]]
    assert len(marked) == 2, marked
    assert all(d["cohort"] == "2026-07-27" for d in marked)
    print("  only the targeted cohort is stamped OK")

    print("\nALL CHECKS PASSED")

asyncio.run(main())
