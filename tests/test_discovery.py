"""The Discovery readiness check.

Two things matter here beyond the arithmetic. It has to keep our own retention figure clearly
separated from Discord's, because they are not the same measurement and presenting ours as
theirs would be a lie a server owner acts on. And it has to say out loud that the numbers
Discord actually judges on are not visible to a bot at all.
"""
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
    def find(self, q=None, *a, **k):
        q = q or {}
        return _Cursor([d for d in self.docs
                        if all(d.get(k2) == v for k2, v in q.items())])


class _Cursor(list):
    def sort(self, *a, **k): return self
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


def make_guild(*, community=True, rules=True, updates=True, filter_all=True,
               verification=discord.VerificationLevel.high,
               mfa=discord.MFALevel.require_2fa, members=900, weeks=40,
               icon=True, description="A nice place", listed=False):
    features = []
    if community: features.append("COMMUNITY")
    if listed: features.append("DISCOVERABLE")
    return types.SimpleNamespace(
        id=GUILD, name="Cool Server", features=features,
        rules_channel=object() if rules else None,
        public_updates_channel=object() if updates else None,
        explicit_content_filter=(discord.ContentFilter.all_members if filter_all
                                 else discord.ContentFilter.disabled),
        verification_level=verification, mfa_level=mfa,
        member_count=members, members=[],
        created_at=NOW - datetime.timedelta(weeks=weeks),
        icon=types.SimpleNamespace(url="https://e.com/i.png") if icon else None,
        description=description)


class Resp:
    def __init__(self): self.deferred = False
    async def defer(self, **kw): self.deferred = True


class Follow:
    def __init__(self): self.calls = []
    async def send(self, *a, **kw): self.calls.append((a, kw))


def interaction(guild):
    i = types.SimpleNamespace(guild=guild, response=Resp())
    i.followup = Follow()
    return i


def spell(days_ago, left_after=None):
    joined = NOW - datetime.timedelta(days=days_ago)
    return {"guild_id": GUILD, "user_id": 1, "cohort": "x", "joined_at": joined,
            "left_at": joined + datetime.timedelta(days=left_after)
            if left_after is not None else None, "nudged": True}


def states(checks):
    return {label: state for state, label, _ in checks}


def by_prefix(checks, prefix):
    return next(s for s, label, _ in checks if label.startswith(prefix))


async def main():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    bot._connection.user = types.SimpleNamespace(id=42, name="Newt", avatar=None)
    bot.MongoClient = object()
    await bot.load_extension("Cogs.Members")
    cog = bot.get_cog("Members")
    import Cogs.Members as M

    print("=== a server that has everything ===")
    checks = cog._discovery_checks(make_guild(), (9, 10))
    assert all(s == "pass" for s, _, _ in checks), states(checks)
    print(f"  all {len(checks)} checks pass OK")

    print("\n=== each requirement is actually checked ===")
    cases = [
        ("Community enabled", dict(community=False), "fail"),
        ("Rules channel set", dict(rules=False), "fail"),
        ("Moderator updates channel set", dict(updates=False), "fail"),
        ("Media scanned for everyone", dict(filter_all=False), "fail"),
        ("Two factor required", dict(mfa=discord.MFALevel.disabled), "fail"),
        ("Verification level", dict(verification=discord.VerificationLevel.low), "warn"),
        ("Server icon", dict(icon=False), "warn"),
        ("Server description", dict(description="   "), "warn"),
    ]
    for prefix, kwargs, expected in cases:
        checks = cog._discovery_checks(make_guild(**kwargs), (9, 10))
        got = by_prefix(checks, prefix)
        assert got == expected, (prefix, got, expected)
        # and the same check passes when the thing is in place
        assert by_prefix(cog._discovery_checks(make_guild(), (9, 10)), prefix) == "pass", prefix
        print(f"  {prefix:32} -> {expected} when missing, pass when set OK")

    print("\n=== counting checks say how far off you are ===")
    checks = cog._discovery_checks(make_guild(members=120), (9, 10))
    state, label, detail = next(c for c in checks if c[1].endswith("members"))
    assert state == "fail" and "120" in label
    assert f"{M.DISCOVERY_MIN_MEMBERS - 120:,} to go" in detail, detail
    print(f"  {label} -> {detail}")

    checks = cog._discovery_checks(make_guild(weeks=3), (9, 10))
    state, label, detail = next(c for c in checks if c[1].endswith("weeks old"))
    assert state == "fail" and "3 weeks" in label
    assert f"{M.DISCOVERY_MIN_AGE_WEEKS - 3} to go" in detail, detail
    print(f"  {label} -> {detail}")

    print("\n=== a brand new server doesn't produce negative numbers ===")
    checks = cog._discovery_checks(make_guild(members=0, weeks=0), None)
    for _, label, detail in checks:
        assert "-" not in detail.replace("Two factor", ""), (label, detail)
    print("  no '-4 to go' anywhere OK")

    print("\n=== our retention figure is labelled as ours ===")
    state, label, detail = next(
        c for c in cog._discovery_checks(make_guild(), (2, 10)) if "retention" in c[1])
    assert state == "warn", state
    assert "20%" in label, label
    assert "not Discord's" in detail, detail
    print(f"  {label} -> {detail[:56]}")

    state, label, detail = next(
        c for c in cog._discovery_checks(make_guild(), (9, 10)) if "retention" in c[1])
    assert state == "pass" and "90%" in label
    print(f"  {label} -> pass OK")

    state, label, detail = next(
        c for c in cog._discovery_checks(make_guild(), None) if "retention" in c[1])
    assert state == "unknown", state
    assert "Not enough history" in detail
    print("  with no history it says so rather than guessing OK")

    print("\n=== the command groups them and never claims to decide ===")
    DB["memberships"].docs.clear()
    for _ in range(9):
        DB["memberships"].docs.append(spell(20))          # joined 20 days ago, still here
    DB["memberships"].docs.append(spell(20, left_after=1))  # one left the next day

    i = interaction(make_guild(community=False, members=100, icon=False))
    await cog.discovery.callback(cog, i)
    assert i.response.deferred
    embed = i.followup.calls[0][1]["embed"]
    names = [f.name for f in embed.fields]
    assert any(n.startswith("Blocking") for n in names), names
    assert any(n.startswith("Worth fixing") for n in names), names
    assert "Only Discord can see these" in names, names
    assert "to sort out" in embed.description
    assert "not a verdict" in embed.footer.text
    body = "\n".join(f.value for f in embed.fields)
    assert "Server Insights" in body, "it should point at where the real numbers live"
    print(f"  fields: {names}")

    print("\n=== a ready server is told to go and apply ===")
    i = interaction(make_guild())
    await cog.discovery.callback(cog, i)
    embed = i.followup.calls[0][1]["embed"]
    assert "Everything I can check" in embed.description, embed.description
    assert not any(f.name.startswith("Blocking") for f in embed.fields)
    assert any(f.name.startswith("Already fine") for f in embed.fields)
    print(f"  {embed.description}")

    print("\n=== a server already listed says so ===")
    i = interaction(make_guild(listed=True))
    await cog.discovery.callback(cog, i)
    assert "already in Discovery" in i.followup.calls[0][1]["embed"].description
    print("  recognised OK")

    print("\n=== a database blip doesn't take the command down ===")
    def boom(*a, **k):
        raise RuntimeError("mongo is having a moment")
    DB["memberships"].find = boom
    i = interaction(make_guild())
    await cog.discovery.callback(cog, i)
    assert len(i.followup.calls) == 1, "it should still answer"
    body = "\n".join(f.value for f in i.followup.calls[0][1]["embed"].fields)
    assert "Not enough history" in body, "and fall back to saying it can't tell"
    print("  answered without the retention figure OK")

    print("\n=== every field stays inside Discord's limits ===")
    DB["memberships"].docs.clear()
    i = interaction(make_guild(community=False, rules=False, updates=False, filter_all=False,
                               mfa=discord.MFALevel.disabled,
                               verification=discord.VerificationLevel.none,
                               members=1, weeks=0, icon=False, description=""))
    await cog.discovery.callback(cog, i)
    embed = i.followup.calls[0][1]["embed"]
    assert len(embed) <= 6000, len(embed)
    for f in embed.fields:
        assert len(f.value) <= 1024, (f.name, len(f.value))
    print(f"  worst case is {len(embed)} characters across {len(embed.fields)} fields OK")

    print("\nALL CHECKS PASSED")

asyncio.run(main())
