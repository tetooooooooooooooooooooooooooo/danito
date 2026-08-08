"""Load every cog main.py loads, and sanity-check the resulting command tree."""
import pathlib as _pathlib
# Resolved from this file so the suite runs from a clone, on any machine, from any cwd.
ROOT = _pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / "src")
WEB_DIR = str(ROOT / "web")
import asyncio
import sys
import types

sys.path.insert(0, SRC_DIR)

stub = types.ModuleType("Database")
stub.get_bot_database = lambda client: None
sys.modules["Database"] = stub
for name in ("pymongo", "certifi", "dotenv"):
    m = types.ModuleType(name)
    if name == "pymongo":
        m.MongoClient = object
    if name == "certifi":
        m.where = lambda: ""
    if name == "dotenv":
        m.load_dotenv = lambda *a, **k: None
    sys.modules[name] = m

import discord
from discord.ext import commands

COGS = [
    "Cogs.Ratings", "Cogs.Members", "Cogs.Greetings", "Cogs.help", "Cogs.stats",
    "Cogs.utility", "Cogs.ImageSpamFilter", "Cogs.MediaLog", "Cogs.PingLog", "Cogs.Moderation", "Cogs.owner",
]


async def main():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    bot._connection.user = types.SimpleNamespace(
        name="soundcord", avatar=types.SimpleNamespace(url="https://example.com/a.png")
    )

    failed = []
    for ext in COGS:
        try:
            await bot.load_extension(ext)
        except Exception as e:
            failed.append((ext, repr(e)))

    print("=== cog load ===")
    for ext in COGS:
        status = "FAILED" if any(f[0] == ext for f in failed) else "ok"
        print(f"  {ext}: {status}")
    for ext, err in failed:
        print(f"\n  !! {ext}\n     {err}")

    print("\n=== full command tree ===")
    names = []
    for cmd in bot.tree.walk_commands():
        kind = "GROUP" if isinstance(cmd, discord.app_commands.Group) else "cmd"
        print(f"  [{kind:5}] /{cmd.qualified_name} — {cmd.description[:60]}")
        if not isinstance(cmd, discord.app_commands.Group):
            names.append(cmd.qualified_name)

    # Discord rejects duplicate top-level names and >100 top-level commands.
    top = [c.name for c in bot.tree.get_commands()]
    print(f"\ntop-level entries: {len(top)} -> {top}")
    assert len(top) == len(set(top)), f"duplicate top-level names: {top}"
    assert len(top) <= 100, "too many top-level commands"

    # Each group is capped at 25 subcommands.
    for c in bot.tree.get_commands():
        if isinstance(c, discord.app_commands.Group):
            subs = list(c.walk_commands())
            assert len(subs) <= 25, f"/{c.name} has {len(subs)} subcommands (max 25)"
            print(f"/{c.name}: {len(subs)} subcommands OK")

    # Every command needs a 1..100 char description.
    for cmd in bot.tree.walk_commands():
        d = cmd.description or ""
        assert 1 <= len(d) <= 100, f"/{cmd.qualified_name} description len {len(d)}: {d!r}"
    print("all descriptions within 1..100 chars OK")

    print(f"\ntotal invokable commands: {len(names)}")
    print("FAILED COGS:" if failed else "\nALL COGS LOADED CLEANLY")


asyncio.run(main())
