"""Render /help for real: category naming, signatures, embed limits, owner hiding."""
import pathlib as _pathlib
# Resolved from this file so the suite runs from a clone, on any machine, from any cwd.
ROOT = _pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / "src")
WEB_DIR = str(ROOT / "web")
import asyncio, os, sys, types

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

os.environ["OWNER_GUILD_ID"] = "123456789012345678"
COGS = ["Cogs.Ratings", "Cogs.Members", "Cogs.Greetings", "Cogs.help", "Cogs.stats", "Cogs.utility",
        "Cogs.ImageSpamFilter", "Cogs.MediaLog", "Cogs.PingLog", "Cogs.Moderation", "Cogs.owner"]


async def main():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    bot._connection.user = types.SimpleNamespace(
        id=42, name="soundcord", avatar=types.SimpleNamespace(url="https://e.com/a.png"))
    for c in COGS:
        await bot.load_extension(c)

    cog = bot.get_cog("Help")
    requester = types.SimpleNamespace(
        id=7, display_name="tet", display_avatar=types.SimpleNamespace(url="https://e.com/u.png"))

    by_cat = cog._commands_by_category()
    print("=== categories ===")
    for key in cog.sorted_categories(by_cat):
        emoji, label, blurb = cog.meta(key)
        print(f"  {emoji} {label:18} ({len(by_cat[key])}) key={key!r}")

    assert "Owner" not in by_cat and "admin" not in by_cat, f"owner leaked: {list(by_cat)}"
    assert "Server Ratings" in by_cat, f"expected Server Ratings cog; got {list(by_cat)}"
    assert "commandcog" not in by_cat, "old cog name still present"
    assert "DiscoveryHelper" not in by_cat, "should use the pretty name, not the class name"
    print("  -> renamed to Server Ratings, owner hidden OK")

    for key in by_cat:
        emoji, label, blurb = cog.meta(key)
        assert emoji != "\u25ab\ufe0f", f"{key} has no CATEGORIES entry"
        assert blurb, f"{key} has no blurb"
    print("  -> all categories have an emoji + blurb OK")

    # ---- overview embed ----
    print("\n=== overview embed ===")
    ov = cog.overview_embed(by_cat, requester)
    print(f"  title: {ov.title}")
    print(f"  desc:  {ov.description}")
    for f in ov.fields:
        print(f"  [{f.name}]\n      {f.value}")
    assert len(ov) <= 6000, f"overview too long: {len(ov)}"
    assert len(ov.fields) <= 25, len(ov.fields)
    for f in ov.fields:
        assert len(f.value) <= 1024, f"field {f.name} is {len(f.value)} chars"
        assert len(f.name) <= 256
    print(f"  total chars: {len(ov)} / 6000, fields: {len(ov.fields)} / 25 OK")

    # ---- every category embed ----
    print("\n=== category embeds ===")
    for key, cmds in by_cat.items():
        e = cog.category_embed(key, cmds, requester)
        assert len(e) <= 6000, f"{key} embed is {len(e)} chars"
        assert len(e.fields) <= 25, f"{key} has {len(e.fields)} fields"
        for f in e.fields:
            assert len(f.value) <= 1024, f"{key} field {len(f.value)} chars"
            assert len(f.name) <= 256, f"{key} field name too long"
        print(f"  {cog.meta(key)[1]:18} {len(e):5} chars, {len(e.fields)} field(s) OK")

    # ---- signatures ----
    print("\n=== signatures ===")
    import Cogs.help as H
    for name in ("ban", "timeout", "purge", "warn"):
        print(f"  {H.signature(bot.tree.get_command(name))}")
    sig = H.signature(bot.tree.get_command("ban"))
    assert "<member>" in sig, sig
    assert "[reason]" in sig, sig
    print("  -> required <> vs optional [] correct OK")

    # ---- the view ----
    print("\n=== view ===")
    view = H.HelpView(cog, by_cat, requester_id=7)
    opts = view.select.options
    print(f"  select options: {[o.label for o in opts]}")
    assert len(opts) <= 25, len(opts)
    assert opts[0].value == "__home__"
    assert sum(o.default for o in opts) == 1, "exactly one default expected"
    for o in opts:
        assert o.description is None or len(o.description) <= 100, o.description
    buttons = [c for c in view.children if isinstance(c, discord.ui.Button)]
    assert buttons, "expected a Home button"
    print(f"  buttons: {[b.label for b in buttons]}")
    print("  -> option count, default flag, description limits OK")

    class Resp:
        def __init__(self): self.sent = None
        async def send_message(self, content=None, **kw): self.sent = content
    stranger = types.SimpleNamespace(user=types.SimpleNamespace(id=999), response=Resp())
    assert await view.interaction_check(stranger) is False, "stranger passed the check!"
    assert "Run `/help` yourself" in stranger.response.sent
    owner_i = types.SimpleNamespace(user=types.SimpleNamespace(id=7), response=Resp())
    assert await view.interaction_check(owner_i) is True, "requester was rejected!"
    print("  -> only the requester can use the menu OK")

    dh = bot.tree.get_command("discoveryhelp")
    assert dh is not None, "/discoveryhelp missing"
    print(f"\n=== /discoveryhelp ===\n  {H.signature(dh)} - {dh.description}")
    assert dh.default_permissions is None, "should be usable by everyone"
    print("  -> public (no permission gate) OK")

    print("\nALL CHECKS PASSED")

asyncio.run(main())
