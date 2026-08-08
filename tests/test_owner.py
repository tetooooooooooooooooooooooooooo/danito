"""Verify owner commands are guild-scoped (invisible elsewhere) and owner-gated."""
import pathlib as _pathlib
# Resolved from this file so the suite runs from a clone, on any machine, from any cwd.
ROOT = _pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / "src")
WEB_DIR = str(ROOT / "web")
import asyncio, os, sys, types, importlib

sys.path.insert(0, SRC_DIR)
stub = types.ModuleType("Database"); stub.get_bot_database = lambda c: None
sys.modules["Database"] = stub
for n in ("pymongo", "certifi", "dotenv"):
    m = types.ModuleType(n)
    if n == "pymongo": m.MongoClient = object
    if n == "certifi": m.where = lambda: ""
    if n == "dotenv": m.load_dotenv = lambda *a, **k: None
    sys.modules[n] = m

import discord
from discord.ext import commands

FAKE_GUILD = 123456789012345678
COGS = ["Cogs.Ratings", "Cogs.Members", "Cogs.help", "Cogs.stats",
        "Cogs.utility", "Cogs.ImageSpamFilter", "Cogs.MediaLog", "Cogs.owner"]


async def build():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    bot._connection.user = types.SimpleNamespace(
        name="soundcord", avatar=types.SimpleNamespace(url="https://e.com/a.png"))
    for c in COGS:
        await bot.load_extension(c)
    return bot


async def main():
    # ---- scenario 1: OWNER_GUILD_ID set ----
    os.environ["OWNER_GUILD_ID"] = str(FAKE_GUILD)
    for mod in [m for m in sys.modules if m.startswith("Cogs.")]:
        del sys.modules[mod]
    bot = await build()

    glob = [c.name for c in bot.tree.get_commands()]
    scoped = [c.name for c in bot.tree.get_commands(guild=discord.Object(id=FAKE_GUILD))]
    print("=== OWNER_GUILD_ID set ===")
    print(f"  global commands ({len(glob)}): {sorted(glob)}")
    print(f"  guild-only  ({len(scoped)}): {sorted(scoped)}")
    assert "admin" not in glob, "/admin leaked into the global scope!"
    assert "admin" in scoped, "/admin missing from the owner guild"
    print("  -> /admin is invisible to every other server OK")

    help_cog = bot.get_cog("Help")
    cats = help_cog._commands_by_category()
    assert "Owner" not in cats, f"/help exposes Owner: {list(cats)}"
    print(f"  -> /help categories: {sorted(cats)} (no Owner) OK")

    # the group's own gate
    owner_cog = bot.get_cog("Owner")
    assert owner_cog is not None, f"cog not found; have {list(bot.cogs)}"
    subs = sorted(c.qualified_name for c in
                  bot.tree.get_commands(guild=discord.Object(id=FAKE_GUILD))[0].walk_commands())
    print(f"  -> subcommands: {subs}")

    class FakeResp:
        def __init__(self): self.sent = None
        async def send_message(self, content=None, **kw): self.sent = content
    class FakeUser:
        id = 999
    inter = types.SimpleNamespace(user=FakeUser(), response=FakeResp())

    bot.owner_id = 111  # someone else
    allowed = await owner_cog.interaction_check(inter)
    assert allowed is False, "non-owner passed the check!"
    assert inter.response.sent == "Unknown command.", inter.response.sent
    print(f"  -> non-owner rejected with {inter.response.sent!r} (no hint it exists) OK")

    bot.owner_id = 999  # now it's them
    inter2 = types.SimpleNamespace(user=FakeUser(), response=FakeResp())
    assert await owner_cog.interaction_check(inter2) is True, "owner was rejected!"
    print("  -> owner allowed OK")

    # ---- scenario 2: env var unset ----
    del os.environ["OWNER_GUILD_ID"]
    for mod in [m for m in sys.modules if m.startswith("Cogs.")]:
        del sys.modules[mod]
    bot2 = await build()
    glob2 = [c.name for c in bot2.tree.get_commands()]
    print("\n=== OWNER_GUILD_ID unset (fallback) ===")
    print(f"  global commands ({len(glob2)}): {sorted(glob2)}")
    assert "admin" in glob2, "expected global fallback registration"
    cats2 = bot2.get_cog("Help")._commands_by_category()
    assert "Owner" not in cats2, "/help must still hide Owner in fallback mode"
    print("  -> registers globally but /help still hides it, is_owner still gates it OK")

    print("\nALL CHECKS PASSED")

asyncio.run(main())
