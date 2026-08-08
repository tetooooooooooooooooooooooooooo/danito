"""The /welcome and /goodbye commands themselves.

This is the gap that let a TypeError reach production: the listeners and the renderer were
covered thoroughly, but the command everybody runs first was never actually invoked. The
header and the rendered greeting were both being passed as `content`.
"""
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


class FakeChannel:
    def __init__(self, cid): self.id = cid; self.mention = f"<#{cid}>"
    def permissions_for(self, who):
        return types.SimpleNamespace(view_channel=True, send_messages=True, embed_links=True)
    async def send(self, **kw): pass


class BadChannel(FakeChannel):
    def permissions_for(self, who):
        return types.SimpleNamespace(view_channel=True, send_messages=False, embed_links=False)


GUILD_OBJ = types.SimpleNamespace(
    id=GUILD, name="Cool Server", member_count=42,
    get_channel=lambda i: FakeChannel(i) if i == CHAN else None,
    me=types.SimpleNamespace(guild_permissions=types.SimpleNamespace(manage_roles=True)))


class FakeMember:
    def __init__(self, uid=500, name="admin"):
        self.id = uid; self.bot = False; self.guild = GUILD_OBJ
        self.display_name = name; self.mention = f"<@{uid}>"
        self.display_avatar = types.SimpleNamespace(url="https://e.com/a.png")
        self._n = name
    def __str__(self): return self._n


class Resp:
    def __init__(self): self.calls = []
    def is_done(self): return bool(self.calls)
    async def send_message(self, *a, **kw): self.calls.append((a, kw))


class Followup:
    def __init__(self): self.calls = []
    async def send(self, *a, **kw): self.calls.append((a, kw))


def content_of(call):
    """send_message accepts content positionally or by keyword; both are valid."""
    args, kw = call
    return args[0] if args else kw.get('content')


def interaction():
    i = types.SimpleNamespace(guild=GUILD_OBJ, user=FakeMember(), response=Resp())
    i.followup = Followup()
    return i


def settings(**kw):
    DB["servers"].docs.clear()
    if kw:
        DB["servers"].docs.append({"guild_id": GUILD, **kw})
    GuildConfig._cache.clear()


async def main():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    bot._connection.user = types.SimpleNamespace(id=42, name="soundcord", avatar=None)
    bot.MongoClient = object()
    await bot.load_extension("Cogs.Greetings")
    cog = bot.get_cog("Greetings")

    print("=== /welcome set, both styles ===")
    for as_embed in (False, True):
        settings()
        i = interaction()
        await cog.welcome_set.callback(cog, i, message="Hi {user}, welcome to {server}!",
                                       channel=FakeChannel(CHAN), embed=as_embed)
        assert len(i.response.calls) == 1, i.response.calls
        args, kw = i.response.calls[0]
        assert not args, f"nothing should be passed positionally, got {args}"
        assert kw.get("ephemeral") is True
        header = kw["content"]
        if as_embed:
            assert kw.get("embed") is not None, "embed style should attach an embed"
            assert "Hi <@500>" in kw["embed"].description
            print(f"  embed=True  header={header[:52]!r} + embed")
        else:
            assert kw.get("embed") is None
            assert "Hi <@500>, welcome to Cool Server!" in header, header
            print(f"  embed=False header and preview combined into one message")
        cfg = await GuildConfig.get(bot, GUILD)
        assert cfg["welcome_enabled"] is True
        assert cfg["welcome_embed"] is as_embed
        assert cfg["welcome_channel"] == CHAN

    print("\n=== /welcome set with no channel means DM ===")
    settings()
    i = interaction()
    await cog.welcome_set.callback(cog, i, message="DM {username}", channel=None, embed=False)
    assert "direct message" in i.response.calls[0][1]["content"]
    cfg = await GuildConfig.get(bot, GUILD)
    assert cfg["welcome_channel"] is None
    print("  stored with no channel, reply says direct message OK")

    print("\n=== a channel the bot can't post in is refused ===")
    settings()
    i = interaction()
    await cog.welcome_set.callback(cog, i, message="hi", channel=BadChannel(CHAN), embed=False)
    reply = content_of(i.response.calls[0])
    assert "missing" in reply.lower(), reply
    cfg = await GuildConfig.get(bot, GUILD)
    assert not cfg.get("welcome_enabled"), "must not enable when it can't post"
    print(f"  {reply[:70]}")
    print("  refused and nothing saved OK")

    print("\n=== /goodbye set, both styles ===")
    for as_embed in (False, True):
        settings()
        i = interaction()
        await cog.goodbye_set.callback(cog, i, message="Bye {username}",
                                       channel=FakeChannel(CHAN), embed=as_embed)
        assert len(i.response.calls) == 1
        args, kw = i.response.calls[0]
        assert not args, f"nothing positional, got {args}"
        assert (kw.get("embed") is not None) is as_embed
        print(f"  embed={as_embed} OK")

    print("\n=== /welcome off and /goodbye off ===")
    settings(welcome_enabled=True, welcome_message="keep me", welcome_channel=CHAN)
    i = interaction()
    await cog.welcome_off.callback(cog, i)
    cfg = await GuildConfig.get(bot, GUILD)
    assert cfg["welcome_enabled"] is False and cfg["welcome_message"] == "keep me"
    print("  disabled, wording kept OK")

    print("\n=== show, configured and not ===")
    for kind, conf in (("welcome", dict(welcome_enabled=True, welcome_message="Hi {user}",
                                        welcome_channel=CHAN)),
                       ("goodbye", dict(goodbye_enabled=True, goodbye_message="Bye {user}",
                                        goodbye_channel=CHAN))):
        settings(**conf)
        i = interaction()
        await getattr(cog, f"{kind}_show").callback(cog, i)
        assert len(i.response.calls) == 1, i.response.calls
        assert i.response.calls[0][1].get("embed") is not None
        assert len(i.followup.calls) == 1, "a preview should follow the settings embed"
        assert not i.followup.calls[0][0], "nothing positional in the followup"
        print(f"  /{kind} show -> settings embed plus preview OK")

    settings()
    i = interaction()
    await cog.welcome_show.callback(cog, i)
    assert len(i.response.calls) == 1
    assert not i.followup.calls, "nothing configured means no preview to send"
    e = i.response.calls[0][1]["embed"]
    assert "Off" in e.description
    print("  /welcome show on a fresh server: says Off, no preview OK")

    print("\n=== every reply stays inside Discord's limits ===")
    long_msg = "Welcome {user}! " + ("x" * 1400)
    settings()
    i = interaction()
    await cog.welcome_set.callback(cog, i, message=long_msg,
                                   channel=FakeChannel(CHAN), embed=False)
    content = i.response.calls[0][1]["content"]
    assert len(content) <= 2000, f"content is {len(content)} chars"
    print(f"  a 1400-char greeting produces a {len(content)}-char reply OK")

    settings()
    i = interaction()
    await cog.welcome_set.callback(cog, i, message=long_msg,
                                   channel=FakeChannel(CHAN), embed=True)
    e = i.response.calls[0][1]["embed"]
    assert len(e) <= 6000, len(e)
    assert len(e.description) <= 4096
    print(f"  as an embed: {len(e)} chars total OK")

    print("\nALL CHECKS PASSED")

asyncio.run(main())
