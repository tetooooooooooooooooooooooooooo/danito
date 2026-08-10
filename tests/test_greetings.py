"""Greetings: placeholders, mass-ping safety, channel vs DM, enable/disable, the old ad is gone."""
import pathlib as _pathlib
# Resolved from this file so the suite runs from a clone, on any machine, from any cwd.
ROOT = _pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / "src")
WEB_DIR = str(ROOT / "web")
import asyncio, sys, types
sys.path.insert(0, SRC_DIR)


class FakeColl:
    def __init__(self, name): self.name = name; self.docs = []
    def create_index(self, *a, **k): pass
    def _ref(self, q): return next(iter(self.find(q)), None)
    def find_one(self, q, *a, **k):
        h = self._ref(q); return dict(h) if h else None
    def find(self, q=None, *a, **k):
        q = q or {}
        return [d for d in self.docs
                if all(d.get(k2) == v for k2, v in q.items() if not isinstance(v, dict))]
    def update_one(self, q, ops, upsert=False):
        h = self._ref(q)
        if h is None:
            if not upsert: return types.SimpleNamespace(matched_count=0)
            h = dict(q); self.docs.append(h)
        h.update(ops.get("$set", {}))
        for k in ops.get("$unset", {}): h.pop(k, None)
        return types.SimpleNamespace(matched_count=1)


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
import GuildConfig

GUILD, CHAN = 1, 2
SENT = []
DMS = []


class FakeChannel:
    def __init__(self, cid): self.id = cid; self.mention = f"<#{cid}>"
    def permissions_for(self, who):
        return types.SimpleNamespace(view_channel=True, send_messages=True, embed_links=True)
    async def send(self, **kw): SENT.append(kw)


GUILD_OBJ = types.SimpleNamespace(
    id=GUILD, name="Cool Server", member_count=42,
    get_channel=lambda i: FakeChannel(i) if i == CHAN else None,
    me=types.SimpleNamespace(guild_permissions=types.SimpleNamespace(manage_roles=True)))


class FakeMember:
    """A class, not a SimpleNamespace: dunders are looked up on the type, so str() only
    behaves like a real Member if __str__ is defined here."""
    def __init__(self, uid=500, name="newbie", bot=False, pending=False):
        self.id = uid
        self.bot = bot
        self.pending = pending
        self.guild = GUILD_OBJ
        self.display_name = name
        self.mention = f"<@{uid}>"
        self.display_avatar = types.SimpleNamespace(url="https://e.com/a.png")
        self._name = name
    def __str__(self): return self._name
    async def send(self, **kw): DMS.append(kw)


def member(uid=500, name="newbie", bot=False, pending=False):
    return FakeMember(uid, name, bot, pending)


def settings(**kw):
    DB["servers"].docs.clear()
    DB["servers"].docs.append({"guild_id": GUILD, **kw})
    GuildConfig._cache.clear()


async def main():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    bot._connection.user = types.SimpleNamespace(id=42, name="soundcord", avatar=None)
    bot.MongoClient = object()
    await bot.load_extension("Cogs.Greetings")
    cog = bot.get_cog("Greetings")
    G = sys.modules["Cogs.Greetings"]

    print("=== commands ===")
    for c in bot.tree.walk_commands():
        if hasattr(c, "parameters"):
            print(f"  /{c.qualified_name} {[p.name for p in c.parameters]}")
    names = {c.qualified_name for c in bot.tree.walk_commands() if hasattr(c, "parameters")}
    assert names == {"welcome set", "welcome off", "welcome show",
                     "goodbye set", "goodbye off", "goodbye show"}, names

    print("\n=== placeholders ===")
    m = member()
    out = G.render("Hi {user} aka {username} ({tag}), welcome to {server}. "
                   "You're member {count}, the {ordinal}!", m, GUILD_OBJ)
    print(f"  {out}")
    assert "<@500>" in out and "Cool Server" in out
    assert "aka newbie" in out, 'display_name'
    assert "(newbie)" in out, '{tag} should render str(member), not a repr'
    assert "namespace" not in out and "object at" not in out, out
    assert "42" in out and "42nd" in out
    print("  every placeholder substituted OK")

    print("\n=== ordinals ===")
    for n, want in [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (11, "11th"),
                    (12, "12th"), (13, "13th"), (21, "21st"), (102, "102nd"), (111, "111th")]:
        got = G._ordinal(n)
        assert got == want, f"{n} -> {got}, want {want}"
    print("  1st 2nd 3rd 4th 11th 12th 13th 21st 102nd 111th OK")

    print("\n=== a mass ping cannot be smuggled in ===")
    nasty = G.render("hey @everyone and @here and @EveryOne", m, GUILD_OBJ)
    print(f"  {nasty!r}")
    assert "@everyone" not in nasty and "@here" not in nasty
    assert "@EveryOne" not in nasty, "the neutraliser should be case-insensitive"
    assert G.SAFE_MENTIONS.everyone is False and G.SAFE_MENTIONS.roles is False
    print("  text neutralised and AllowedMentions blocks it as well OK")

    print("\n=== nothing is sent unless it's set up ===")
    settings()                                   # no greeting config at all
    SENT.clear(); DMS.clear()
    await cog.greet(member())
    await cog.on_member_remove(member())
    assert not SENT and not DMS, "an unconfigured server must stay silent"
    print("  unconfigured server sends nothing OK")

    settings(welcome_enabled=True)               # enabled but no wording
    SENT.clear(); DMS.clear()
    await cog.greet(member())
    assert not SENT and not DMS, "enabled with no message should still send nothing"
    print("  enabled but empty sends nothing OK")

    print("\n=== welcome to a channel ===")
    settings(welcome_enabled=True, welcome_message="Welcome {user} to {server}!",
             welcome_channel=CHAN, welcome_embed=False)
    SENT.clear(); DMS.clear()
    await cog.greet(member())
    assert len(SENT) == 1 and not DMS, (SENT, DMS)
    print(f"  content: {SENT[0]['content']}")
    assert SENT[0]["content"] == "Welcome <@500> to Cool Server!"
    assert SENT[0]["embed"] is None
    assert SENT[0]["allowed_mentions"].everyone is False
    print("  posted to the channel with mass pings disabled OK")

    print("\n=== welcome as an embed ===")
    settings(welcome_enabled=True, welcome_message="Welcome {user}!",
             welcome_channel=CHAN, welcome_embed=True)
    SENT.clear()
    await cog.greet(member())
    e = SENT[0]["embed"]
    assert SENT[0]["content"] is None and e is not None
    assert e.description == "Welcome <@500>!"
    assert len(e) <= 6000
    print(f"  embed description: {e.description}")
    print("  embed mode OK")

    print("\n=== welcome by DM when no channel is set ===")
    settings(welcome_enabled=True, welcome_message="Hi {username}", welcome_channel=None)
    SENT.clear(); DMS.clear()
    await cog.greet(member())
    assert not SENT and len(DMS) == 1, (SENT, DMS)
    print(f"  dm: {DMS[0]['content']}")
    print("  falls back to a direct message OK")

    print("\n=== a deleted channel doesn't crash anything ===")
    settings(welcome_enabled=True, welcome_message="hi", welcome_channel=99999)
    SENT.clear(); DMS.clear()
    await cog.greet(member())
    assert not SENT and not DMS
    print("  missing channel handled quietly OK")

    print("\n=== goodbye ===")
    settings(goodbye_enabled=True, goodbye_message="{username} left. {count} remain.",
             goodbye_channel=CHAN)
    SENT.clear()
    await cog.on_member_remove(member())
    assert len(SENT) == 1, SENT
    print(f"  {SENT[0]['content']}")
    assert SENT[0]["content"] == "newbie left. 42 remain."
    # a goodbye must never be attempted as a DM
    settings(goodbye_enabled=True, goodbye_message="bye", goodbye_channel=None)
    SENT.clear(); DMS.clear()
    await cog.on_member_remove(member())
    assert not SENT and not DMS, "goodbye with no channel should do nothing, not DM"
    print("  channel only, never DMs someone who left OK")

    print("\n=== bots are ignored ===")
    settings(welcome_enabled=True, welcome_message="hi", welcome_channel=CHAN,
             goodbye_enabled=True, goodbye_message="bye", goodbye_channel=CHAN)
    SENT.clear(); DMS.clear()
    await cog.greet(member(bot=True))
    await cog.on_member_remove(member(bot=True))
    assert not SENT and not DMS
    print("  no greeting for bots OK")

    print("\n=== somebody still on the rules screen is not welcomed yet ===")
    settings(welcome_enabled=True, welcome_message="Welcome {user}!", welcome_channel=CHAN)
    SENT.clear(); DMS.clear()
    await cog.greet(member(pending=True))
    assert not SENT and not DMS, "a pending member cannot even see the channel yet"
    print("  nothing sent while they are pending OK")

    print("\n=== and is welcomed the moment they accept ===")
    SENT.clear()
    before, after = member(pending=True), member(pending=False)
    await cog.on_member_update(before, after)
    assert len(SENT) == 1, SENT
    assert SENT[0]["content"] == "Welcome <@500>!"
    print(f"  {SENT[0]['content']} on the pending to accepted transition OK")

    print("\n=== and only on that transition ===")
    SENT.clear()
    # The noisy cases: a nickname change, a role change, a fresh join. None of them is
    # somebody finishing the rules screen, and a welcome on any of them would be a second one.
    await cog.on_member_update(member(pending=False), member(pending=False))
    await cog.on_member_update(member(pending=True), member(pending=True))
    await cog.on_member_update(member(pending=False), member(pending=True))
    assert not SENT, SENT
    await cog.on_member_update(member(pending=True), member(bot=True))
    assert not SENT, "not for bots either"
    print("  every other member update ignored OK")

    print("\n=== no goodbye for somebody who never accepted ===")
    settings(goodbye_enabled=True, goodbye_message="bye", goodbye_channel=CHAN)
    SENT.clear()
    await cog.on_member_remove(member(pending=True))
    assert not SENT, "they were never welcomed, so a goodbye is the only trace they left"
    await cog.on_member_remove(member(pending=False))
    assert len(SENT) == 1, "somebody who did accept still gets one"
    print("  pending leaver silent, accepted leaver announced OK")

    print("\n=== no goodbye for an account the age gate turned away ===")
    SENT.clear()
    cog.suppress_goodbye(777)
    await cog.on_member_remove(member(uid=777))
    assert not SENT, "the gate kicks them, and that kick must not read as a departure"
    # The note is spent once, so a real departure later still gets announced.
    await cog.on_member_remove(member(uid=777))
    assert len(SENT) == 1, "the suppression covers one leave, not that member forever"
    print("  gated account silent, and only for the leave it caused OK")

    print("\n=== the note expires rather than piling up ===")
    import time as _time
    cog.suppress_goodbye(888)
    cog._turned_away[888] = _time.monotonic() - (cog.SUPPRESS_SECONDS + 1)
    SENT.clear()
    await cog.on_member_remove(member(uid=888))
    assert len(SENT) == 1, "an old note must not silence a genuine departure"
    cog.suppress_goodbye(999)
    assert 888 not in cog._turned_away, "and stale ids are dropped on the next note"
    cog._turned_away.clear()
    print("  stale notes ignored and pruned OK")

    print("\n=== there is no join listener to race the age gate ===")
    # Members calls greet() after the gate has run. A listener here would fire alongside that
    # handler instead, which is what used to welcome raid accounts on their way out.
    assert not hasattr(cog, "on_member_join"), \
        "Greetings must not listen for joins; Members calls greet()"
    import Cogs.Members as MembersMod
    src = open(MembersMod.__file__, encoding="utf-8").read()
    assert "cog.greet(member)" in src, "Members has to be the thing that calls it"
    assert "suppress_goodbye" in src, "and has to say when the gate turned somebody away"
    print("  no listener here, and Members does the calling OK")

    print("\n=== off keeps the wording ===")
    settings(welcome_enabled=True, welcome_message="keep me", welcome_channel=CHAN)

    class Resp:
        def __init__(self): self.sent = None; self.kw = {}
        async def send_message(self, content=None, **kw): self.sent = content; self.kw = kw
    inter = types.SimpleNamespace(guild=GUILD_OBJ, response=Resp(), user=member())
    await cog.welcome_off.callback(cog, inter)
    cfg = await GuildConfig.get(bot, GUILD)
    assert cfg["welcome_enabled"] is False
    assert cfg["welcome_message"] == "keep me", "disabling must not wipe the wording"
    SENT.clear()
    await cog.greet(member())
    assert not SENT, "disabled means silent"
    print("  disabled, wording preserved, nothing sent OK")

    print("\n=== the hardcoded advert is gone ===")
    import Cogs.Members as MembersMod
    src = open(MembersMod.__file__, encoding="utf-8").read()
    for bad in ("Meown", "meown.net", "VPjxQgTgBh", "WELCOME_MESSAGE"):
        assert bad not in src, f"{bad} still present in Members.py"
    assert not hasattr(MembersMod.Members, "_welcome")
    print("  no advert text, no _welcome method OK")

    print("\nALL CHECKS PASSED")

asyncio.run(main())
