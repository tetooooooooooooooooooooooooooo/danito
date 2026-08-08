"""Moderation cog: duration parsing, hierarchy guards, permission decorators, embed limits."""
import pathlib as _pathlib
# Resolved from this file so the suite runs from a clone, on any machine, from any cwd.
ROOT = _pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / "src")
WEB_DIR = str(ROOT / "web")
import asyncio, sys, types, datetime

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


async def main():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    bot._connection.user = types.SimpleNamespace(
        id=42, name="soundcord", avatar=types.SimpleNamespace(url="https://e.com/a.png"))
    await bot.load_extension("Cogs.Moderation")
    cog = bot.get_cog("Moderation")
    M = sys.modules["Cogs.Moderation"]

    # ---- duration parsing ----
    print("=== parse_duration ===")
    cases = [("10m", 600), ("2h", 7200), ("1d", 86400), ("1h30m", 5400),
             ("45s", 45), ("1w", 604800), ("2d12h", 216000),
             ("", None), ("abc", None), ("0m", None), ("10", None), ("m", None)]
    for text, want in cases:
        got = M.parse_duration(text)
        assert got == want, f"{text!r} -> {got}, want {want}"
        print(f"  {text!r:10} -> {got}")

    print("\n=== fmt_duration ===")
    for secs, want in [(600, "10m"), (7200, "2h"), (86400, "1d"),
                       (5400, "1h 30m"), (45, "45s"), (0, "0s"), (90061, "1d 1h 1m")]:
        got = M.fmt_duration(secs)
        assert got == want, f"{secs} -> {got!r}, want {want!r}"
        print(f"  {secs:7} -> {got}")

    # round-trip: every parseable duration should format back sensibly
    for text in ["10m", "2h", "1d", "1h30m", "1w"]:
        secs = M.parse_duration(text)
        assert M.parse_duration(M.fmt_duration(secs).replace(" ", "")) == secs, text
    print("  round-trip parse->fmt->parse stable OK")

    # ---- hierarchy guard ----
    print("\n=== hierarchy guard ===")

    def role(pos):
        r = types.SimpleNamespace(position=pos)
        r.__ge__ = lambda self, o: self.position >= o.position
        return r

    class Role:
        def __init__(self, pos): self.position = pos
        def __ge__(self, o): return self.position >= o.position
        def __lt__(self, o): return self.position < o.position

    def member(uid, pos):
        m = types.SimpleNamespace(id=uid, top_role=Role(pos))
        m.__str__ = lambda: f"user{uid}"
        return m

    OWNER, ACTOR, TARGET, BOT = 1, 2, 3, 42
    guild = types.SimpleNamespace(owner_id=OWNER, me=member(BOT, 50))

    def check(actor_pos, target_pos, actor_id=ACTOR, target_id=TARGET, bot_pos=50):
        guild.me = member(BOT, bot_pos)
        inter = types.SimpleNamespace(guild=guild, user=member(actor_id, actor_pos))
        cog._check_hierarchy(inter, member(target_id, target_pos))

    # self-action
    try:
        check(10, 5, actor_id=ACTOR, target_id=ACTOR); assert False, "self-action allowed!"
    except M.HierarchyError as e: print(f"  self       -> blocked: {e}")
    # the bot itself
    try:
        check(10, 5, target_id=BOT); assert False, "bot self-action allowed!"
    except M.HierarchyError as e: print(f"  bot        -> blocked: {e}")
    # server owner
    try:
        check(10, 5, target_id=OWNER); assert False, "owner action allowed!"
    except M.HierarchyError as e: print(f"  owner      -> blocked: {e}")
    # equal roles
    try:
        check(10, 10); assert False, "equal-role action allowed!"
    except M.HierarchyError as e: print(f"  equal role -> blocked: {e}")
    # target above actor
    try:
        check(5, 10); assert False, "higher-role action allowed!"
    except M.HierarchyError as e: print(f"  higher     -> blocked: {e}")
    # target above the bot
    try:
        check(90, 80, bot_pos=70); assert False, "above-bot action allowed!"
    except M.HierarchyError as e: print(f"  above bot  -> blocked: {e}")
    # the legitimate case
    check(20, 10)
    print("  actor > target, bot > target -> allowed OK")
    # guild owner may action someone with an equal role
    check(10, 10, actor_id=OWNER)
    print("  guild owner bypasses the role comparison OK")

    # ---- bot-permission guard ----
    print("\n=== _need ===")
    class Perms:
        def __init__(self, **kw): self.__dict__.update(kw)
        def __getattr__(self, n): return False
    g = types.SimpleNamespace(me=types.SimpleNamespace(
        guild_permissions=Perms(ban_members=True, kick_members=False)))
    cog._need(g, ban_members=True)
    print("  present permission passes OK")
    try:
        cog._need(g, kick_members=True); assert False, "missing perm passed!"
    except M.HierarchyError as e:
        assert "Kick Members" in str(e), e
        print(f"  missing -> {e}")

    # ---- command surface ----
    print("\n=== commands & permissions ===")
    expected = {
        "ban": "ban_members", "unban": "ban_members", "kick": "kick_members",
        "timeout": "moderate_members", "untimeout": "moderate_members",
        "purge": "manage_messages", "slowmode": "manage_channels",
        "lock": "manage_channels", "unlock": "manage_channels",
        "warn": "moderate_members", "warnings": "moderate_members",
        "delwarn": "manage_guild", "modlogs": "moderate_members",
    }
    found = {}
    for c in bot.tree.walk_commands():
        dp = c.default_permissions
        perms = [p for p, v in dp if v] if dp else []
        found[c.name] = perms
        assert dp is not None, f"/{c.name} has no default_permissions"
        assert c.guild_only, f"/{c.name} is not guild_only"
        print(f"  /{c.name:15} default_permissions={perms}")

    assert set(found) == set(expected), \
        f"missing {set(expected) - set(found)}, extra {set(found) - set(expected)}"
    for name, want in expected.items():
        assert want in found[name], f"/{name} should require {want}, got {found[name]}"
    print(f"  all {len(expected)} commands carry the right permission OK")

    # every command must also enforce at runtime, not just hide in the UI
    for c in bot.tree.walk_commands():
        assert c.checks, f"/{c.name} has no runtime check (default_permissions is only a UI hint)"
    print("  all commands have runtime checks too OK")

    print("\nALL CHECKS PASSED")

asyncio.run(main())
