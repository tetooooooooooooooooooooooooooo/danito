"""/retention period grouping: bucket truncation, sequences, tallies, embed limits."""
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
        return all(d.get(k) == v for k, v in q.items() if not isinstance(v, dict))
    def find(self, q=None, *a, **k):
        return _Cursor([d for d in self.docs if self._match(d, q or {})])
    def find_one(self, q, *a, **k):
        hits = [d for d in self.docs if self._match(d, q)]
        return dict(hits[0]) if hits else None
    def insert_one(self, d): self.docs.append(dict(d))
    def update_one(self, q, ops, upsert=False):
        return types.SimpleNamespace(matched_count=0)


class _Cursor(list):
    def sort(self, key, direction=1):
        super().sort(key=lambda d: d.get(key), reverse=direction < 0)
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
# Pinned to half past the hour. Using the live clock made this flaky: a spell placed at
# "10 minutes ago" only lands in the current hour bucket if we happen to be past ten past.
NOW = datetime.datetime.now(datetime.timezone.utc).replace(
    minute=30, second=0, microsecond=0)


def spell(uid, joined, left=None, nudged=False):
    DB["memberships"].docs.append({
        "guild_id": GUILD, "user_id": uid, "cohort": str(joined.date()),
        "joined_at": joined, "left_at": left, "nudged": nudged})


async def main():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    bot._connection.user = types.SimpleNamespace(id=42, name="soundcord", avatar=None)
    bot.MongoClient = object()
    await bot.load_extension("Cogs.Members")
    cog = bot.get_cog("Members")
    M = sys.modules["Cogs.Members"]

    print("=== bucket truncation ===")
    t = datetime.datetime(2026, 8, 4, 15, 37, 42, tzinfo=datetime.timezone.utc)   # a Tuesday
    hour = cog._bucket_start(t, "hour")
    day = cog._bucket_start(t, "day")
    week = cog._bucket_start(t, "week")
    month = cog._bucket_start(t, "month")
    print(f"  {t:%d %b %H:%M}  ->  hour {hour:%H:%M}  day {day:%d %b %H:%M}  "
          f"week {week:%d %b}  month {month:%d %b}")
    assert hour.hour == 15 and hour.minute == 0 and hour.second == 0
    assert day.hour == 0
    assert week.weekday() == 0 and week.day == 3, week      # Monday 3 Aug
    assert month.day == 1
    cog._bucket_start(t.replace(tzinfo=None), "day")        # naive must not raise
    print("  truncation correct, naive input handled OK")

    print("\n=== bucket sequences ===")
    for period, (unit, count, _f, _h) in M.PERIODS.items():
        seq = cog._buckets(t, unit, count)
        assert len(seq) == count, (period, len(seq))
        assert seq == sorted(seq), f"{period}: buckets should run oldest to newest"
        assert len(set(seq)) == count, f"{period}: duplicate buckets"
        print(f"  {period:8} {count:>2} buckets  {seq[0]:%d %b %H:%M} -> {seq[-1]:%d %b %H:%M}")
    across_feb = cog._buckets(
        datetime.datetime(2026, 3, 31, tzinfo=datetime.timezone.utc), "month", 3)
    assert [b.month for b in across_feb] == [1, 2, 3], [b.month for b in across_feb]
    print("  stepping back through February (28 days) OK")

    print("\n=== timeline tallies ===")
    DB["memberships"].docs.clear()
    for uid in (1, 2, 3):
        spell(uid, NOW - datetime.timedelta(minutes=10))
    DB["memberships"].docs[2]["left_at"] = NOW - datetime.timedelta(minutes=1)
    spell(4, NOW - datetime.timedelta(hours=3))

    tl = dict(cog._timeline(DB["memberships"].docs, NOW, "hourly"))
    this_hour = tl[cog._bucket_start(NOW, "hour")]
    print(f"  current hour: {this_hour}")
    assert this_hour["joined"] == 3, this_hour
    assert this_hour["left"] == 1, this_hour
    assert this_hour["still"] == 2, this_hour
    three_ago = tl[cog._bucket_start(NOW - datetime.timedelta(hours=3), "hour")]
    print(f"  3 hours ago:  {three_ago}")
    assert three_ago["joined"] == 1 and three_ago["still"] == 1
    print("  joins, leaves and still-here tallied per bucket OK")

    spell(99, NOW - datetime.timedelta(days=40))
    tl2 = cog._timeline(DB["memberships"].docs, NOW, "hourly")
    assert sum(x["joined"] for _b, x in tl2) == 4, "a 40-day-old join is outside 24 hours"
    print("  spells outside the window excluded OK")

    print("\n=== nudged is counted, not just flagged ===")
    DB["memberships"].docs.clear()
    base = NOW - datetime.timedelta(days=3)
    spell(1, base, nudged=True)
    spell(2, base, nudged=False)
    wk = dict(cog._timeline(DB["memberships"].docs, NOW, "weekly"))
    entry = wk[cog._bucket_start(base, "week")]
    print(f"  a week with a mixed intake: {entry}")
    assert entry["joined"] == 2 and entry["nudged"] == 1, entry
    print("  a bucket spanning several cohorts can be partly nudged OK")

    print("\n=== every period renders within Discord's limits ===")
    DB["memberships"].docs.clear()
    # busy server: a join every 20 minutes for ~40 days, a third of them leaving
    for i in range(3000):
        joined = NOW - datetime.timedelta(minutes=20 * i)
        left = joined + datetime.timedelta(days=2) if i % 3 == 0 else None
        if left and left > NOW:
            left = None
        spell(i, joined, left, nudged=(i % 2 == 0))

    captured = {}
    class FU:
        async def send(self, **kw): captured.update(kw)
    guild = types.SimpleNamespace(id=GUILD, name="Busy Server", icon=None)
    inter = types.SimpleNamespace(
        guild=guild, followup=FU(),
        response=types.SimpleNamespace(defer=lambda **k: asyncio.sleep(0)))

    for period in M.PERIODS:
        captured.clear()
        await cog.retention.callback(cog, inter, period=types.SimpleNamespace(value=period))
        e = captured["embed"]
        assert len(e) <= 6000, f"{period}: {len(e)} chars"
        assert len(e.fields) <= 25, f"{period}: {len(e.fields)} fields"
        for f in e.fields:
            assert len(f.value) <= 1024, f"{period}/{f.name}: {len(f.value)} chars"
            assert len(f.name) <= 256, f"{period}/{f.name}: name too long"
        assert any(M.PERIODS[period][3] in f.name for f in e.fields), [f.name for f in e.fields]
        print(f"  {period:8} {len(e):5} chars, {len(e.fields)} fields")
    print("  all four periods fit on a 3000-member history OK")

    print("\n=== the hourly view, rendered ===")
    DB["memberships"].docs.clear()
    for i in range(4):
        spell(i, NOW - datetime.timedelta(minutes=5), nudged=True)
    spell(50, NOW - datetime.timedelta(hours=2))
    DB["memberships"].docs[-1]["left_at"] = NOW - datetime.timedelta(hours=1)
    captured.clear()
    await cog.retention.callback(cog, inter, period=types.SimpleNamespace(value="hourly"))
    e = captured["embed"]
    print(f"  {e.description}")
    for f in e.fields:
        print(f"  [{f.name}]")
        for line in f.value.splitlines():
            if line.strip():
                print(f"      {line}")

    print("\n=== default is daily ===")
    captured.clear()
    await cog.retention.callback(cog, inter, period=None)
    assert any(M.PERIODS["daily"][3] in f.name for f in captured["embed"].fields)
    print("  omitting the option gives the daily view OK")

    print("\nALL CHECKS PASSED")

asyncio.run(main())
