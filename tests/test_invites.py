"""Invite attribution: which invite a member came through.

Discord never says. The bot keeps its own count of every invite's uses and, on each join,
looks for the one that went up. That makes the interesting cases the ones where the answer
cannot be known, and every one of them has to record "unknown" rather than a guess: crediting
the wrong invite quietly tells somebody their worst campaign is their best.
"""
import pathlib as _pathlib
ROOT = _pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / "src")

import asyncio, datetime, sys, types
sys.path.insert(0, SRC_DIR)


class FakeColl:
    """Enough of a collection to test a two step write: insert, then patch by _id."""

    def __init__(self, name):
        self.name = name
        self.docs = []
        self._ids = 0

    def create_index(self, *a, **k): pass
    def find(self, q=None, *a, **k): return []
    def find_one(self, q, *a, **k): return None
    def find_one_and_update(self, *a, **k): return None

    def insert_one(self, d):
        self._ids += 1
        doc = dict(d, _id=self._ids)
        self.docs.append(doc)
        return types.SimpleNamespace(inserted_id=doc["_id"])

    def update_one(self, q, ops, upsert=False):
        for doc in self.docs:
            if all(doc.get(key) == value for key, value in q.items()):
                doc.update(ops.get("$set", {}))
                return types.SimpleNamespace(matched_count=1)
        return types.SimpleNamespace(matched_count=0)


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


class FakeInvite:
    def __init__(self, code, uses, inviter=None, guild=None):
        self.code, self.uses, self.inviter, self.guild = code, uses, inviter, guild


def user(uid, name, global_name=None):
    return types.SimpleNamespace(id=uid, name=name, global_name=global_name)


class FakeGuild:
    """Only the parts the cog touches."""

    def __init__(self, gid, invites=(), can_manage=True, vanity=None, features=()):
        self.id = gid
        self.features = list(features)
        self._invites = list(invites)
        self._vanity = vanity
        self.forbidden = False
        self.me = types.SimpleNamespace(guild_permissions=types.SimpleNamespace(
            manage_guild=can_manage, manage_roles=False))

    # So the same object can stand in for member.guild and the real join path can run through
    # it, rather than the test stubbing out the lookup it is supposed to be checking.
    def get_role(self, role_id):
        return None

    async def invites(self):
        if self.forbidden:
            raise discord.Forbidden(types.SimpleNamespace(status=403, reason=""), "nope")
        return list(self._invites)

    async def vanity_invite(self):
        return self._vanity

    def use(self, code, times=1):
        """Somebody joins through this invite."""
        for invite in self._invites:
            if invite.code == code:
                invite.uses += times
                return
        if self._vanity is not None and code == "vanity":
            self._vanity.uses += times


async def main():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    bot.MongoClient = object()
    await bot.load_extension("Cogs.Invites")
    cog = bot.get_cog("Invites")
    INV = sys.modules["Cogs.Invites"]

    marcus = user(1, "marcus")
    priya = user(2, "priya", global_name="Priya P")

    print("=== the counts get read up front ===")
    guild = FakeGuild(10, [FakeInvite("aaa", 5, marcus), FakeInvite("bbb", 0, priya)])
    assert await cog._snapshot(guild) is True
    assert cog.uses[10] == {"aaa": 5, "bbb": 0}, cog.uses[10]
    assert cog.tracked(10) is True
    print(f"  {cog.uses[10]} OK")

    print("\n=== one invite moves, so that's the answer ===")
    guild.use("aaa")
    code, inviter_id, inviter_name = await cog.resolve(guild)
    assert (code, inviter_id, inviter_name) == ("aaa", 1, "marcus"), (code, inviter_id, inviter_name)
    # And the cache has to have moved on, or the next join blames this one again.
    assert cog.uses[10]["aaa"] == 6, cog.uses[10]
    print("  aaa, by marcus, and the count advanced OK")

    print("\n=== the display name wins over the username ===")
    guild.use("bbb")
    code, _, inviter_name = await cog.resolve(guild)
    assert (code, inviter_name) == ("bbb", "Priya P"), (code, inviter_name)
    print("  Priya P rather than priya OK")

    print("\n=== an invite that was never used doesn't get the credit ===")
    # A brand new invite appearing at zero uses must not look like the one that moved.
    guild._invites.append(FakeInvite("ccc", 0, marcus))
    guild.use("aaa")
    code, _, _ = await cog.resolve(guild)
    assert code == "aaa", code
    print("  a fresh zero-use invite is ignored OK")

    print("\n=== two at once is unknowable, not a coin flip ===")
    guild.use("aaa")
    guild.use("bbb")
    code, inviter_id, inviter_name = await cog.resolve(guild)
    assert code is INV.UNKNOWN, code
    assert (inviter_id, inviter_name) == (None, None)
    # The counts still have to advance, or the ambiguity repeats on every later join.
    assert cog.uses[10]["aaa"] == 8 and cog.uses[10]["bbb"] == 2, cog.uses[10]
    print("  recorded as unknown, and the cache still caught up OK")

    print("\n=== nothing moved, so nothing is claimed ===")
    code, _, _ = await cog.resolve(guild)
    assert code is INV.UNKNOWN, code
    print("  a Discovery or widget join stays unattributed OK")

    print("\n=== without Manage Server it can't see anything ===")
    blind = FakeGuild(11, [FakeInvite("zzz", 3, marcus)], can_manage=False)
    assert await cog._snapshot(blind) is False
    assert cog.tracked(11) is False
    code, _, _ = await cog.resolve(blind)
    assert code is INV.UNKNOWN, code
    print("  no permission, no attribution, no crash OK")

    print("\n=== and a permission taken away mid-run doesn't poison the cache ===")
    guild.forbidden = True
    code, _, _ = await cog.resolve(guild)
    assert code is INV.UNKNOWN, code
    guild.forbidden = False
    print("  a refused fetch returns unknown OK")

    print("\n=== the vanity url counts too ===")
    vanity_guild = FakeGuild(12, [FakeInvite("aaa", 1, marcus)],
                             vanity=FakeInvite("discord.gg/retro", 40),
                             features=["VANITY_URL", "COMMUNITY"])
    await cog._snapshot(vanity_guild)
    assert cog.uses[12][INV.VANITY] == 40, cog.uses[12]
    vanity_guild.use("vanity")
    code, inviter_id, _ = await cog.resolve(vanity_guild)
    assert code == INV.VANITY, code
    assert inviter_id is None, "a vanity url has no author"
    print("  the vanity url is tracked as its own source OK")

    print("\n=== a server without one isn't asked ===")
    plain = FakeGuild(13, [FakeInvite("aaa", 0, marcus)])
    await cog._snapshot(plain)
    assert INV.VANITY not in cog.uses[13], cog.uses[13]
    print("  no VANITY_URL feature, no vanity row OK")

    print("\n=== invites created and deleted keep the cache honest ===")
    created = FakeInvite("ddd", 0, priya, guild=guild)
    await cog.on_invite_create(created)
    assert cog.uses[10]["ddd"] == 0
    guild._invites.append(created)
    guild.use("ddd")
    code, _, name = await cog.resolve(guild)
    assert (code, name) == ("ddd", "Priya P"), (code, name)
    print("  a new invite is seeded, so its first use is attributable OK")

    await cog.on_invite_delete(FakeInvite("ddd", 1, priya, guild=guild))
    assert "ddd" not in cog.uses[10]
    # The author survives deletion: a join attributed seconds earlier still has a name.
    assert cog.authors[10].get("ddd") is not None
    print("  a deleted invite leaves the cache but keeps its author OK")

    print("\n=== leaving a server forgets it ===")
    await cog.on_guild_remove(types.SimpleNamespace(id=10))
    assert 10 not in cog.uses and 10 not in cog.authors
    assert cog.tracked(10) is False
    print("  no counts kept for a server the bot isn't in OK")

    print("\n=== Members writes the code onto the spell ===")
    await bot.load_extension("Cogs.Members")
    members = bot.get_cog("Members")
    joined = FakeGuild(20, [FakeInvite("promo", 7, marcus)])
    await cog._snapshot(joined)
    joined.use("promo")

    # Nothing stubbed: the real on_member_join asks the real cog through the real guild.
    await members.on_member_join(types.SimpleNamespace(id=555, bot=False, guild=joined))

    spell = DB["memberships"].docs[-1]
    assert spell["invite_code"] == "promo", spell
    assert spell["inviter_name"] == "marcus", spell
    assert spell["left_at"] is None and spell["cohort"], spell
    print(f"  spell carries invite_code={spell['invite_code']}, "
          f"inviter_name={spell['inviter_name']} OK")

    print("\n=== the join is recorded even when Discord never answers ===")
    # The whole point of the two step write. A fetch that hangs used to hold the membership
    # record behind it, and a raid is when both the fetch is slowest and the record matters
    # most. The join has to land regardless.
    slow = FakeGuild(21, [FakeInvite("promo", 1, marcus)])
    await cog._snapshot(slow)

    async def never_answers():
        await asyncio.sleep(3600)

    slow.invites = never_answers
    before = len(DB["memberships"].docs)
    try:
        await asyncio.wait_for(
            members.on_member_join(types.SimpleNamespace(id=600, bot=False, guild=slow)),
            timeout=0.4)
    except asyncio.TimeoutError:
        pass                      # the handler is still stuck on the fetch, which is fine
    await asyncio.sleep(0)
    assert len(DB["memberships"].docs) == before + 1, "the join was held up by the lookup"
    assert DB["memberships"].docs[-1]["user_id"] == 600
    assert DB["memberships"].docs[-1]["invite_code"] is None, "nothing to attribute yet"
    print("  recorded while the invite fetch was still hanging OK")

    print("\n=== a burst costs one lookup, not one per joiner ===")
    # Ten people arriving at once used to mean ten invite fetches, each rate limited, each
    # holding up a record. They cannot be told apart anyway, so the ones arriving inside an
    # existing lookup are answered without a second call.
    rush = FakeGuild(22, [FakeInvite("promo", 0, marcus)])
    await cog._snapshot(rush)
    calls = {"n": 0}
    real_invites = rush.invites

    async def counted():
        calls["n"] += 1
        await asyncio.sleep(0.05)         # long enough for the others to pile up behind it
        return await real_invites()

    rush.invites = counted
    rush.use("promo", 10)
    before = len(DB["memberships"].docs)
    await asyncio.gather(*[
        members.on_member_join(types.SimpleNamespace(id=700 + n, bot=False, guild=rush))
        for n in range(10)])
    assert len(DB["memberships"].docs) == before + 10, "every join still recorded"
    assert calls["n"] == 1, f"{calls['n']} invite fetches for 10 simultaneous joins"
    assert not cog.busy, "the in flight marker has to clear"
    print(f"  10 joins, all recorded, {calls['n']} invite fetch OK")

    print("\n=== and still works with the Invites cog gone ===")
    await bot.unload_extension("Cogs.Invites")
    assert bot.get_cog("Invites") is None
    before = len(DB["memberships"].docs)
    joined.use("promo")          # an invite really was used, and still nothing can read it
    await members.on_member_join(types.SimpleNamespace(id=556, bot=False, guild=joined))
    assert len(DB["memberships"].docs) == before + 1, "the join still has to be recorded"
    assert DB["memberships"].docs[-1]["invite_code"] is None
    print("  the join is still recorded, just without a code OK")

    print("\nALL CHECKS PASSED")


asyncio.run(main())
