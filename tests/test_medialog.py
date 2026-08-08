"""Exercise MediaLog's real logic: caching, eviction, embed building, file attachment."""
import pathlib as _pathlib
# Resolved from this file so the suite runs from a clone, on any machine, from any cwd.
ROOT = _pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / "src")
WEB_DIR = str(ROOT / "web")
import asyncio, io, sys, types, datetime, time

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
        name="soundcord", avatar=types.SimpleNamespace(url="https://e.com/a.png"))
    await bot.load_extension("Cogs.MediaLog")
    cog = bot.get_cog("MediaLog")
    ML = sys.modules["Cogs.MediaLog"]

    print("=== commands ===")
    for c in bot.tree.walk_commands():
        print(f"  /{c.qualified_name} — {c.description}")

    # ---- media detection ----
    print("\n=== _is_media ===")
    cases = [
        ("photo.png", "image/png", True), ("clip.mp4", "video/mp4", True),
        ("song.mp3", "audio/mpeg", True), ("memo.ogg", None, True),
        ("movie.MOV", None, True), ("doc.pdf", "application/pdf", False),
        ("archive.zip", None, False), ("script.py", "text/x-python", False),
        ("weird", None, False), ("noext.jpeg", None, True),
    ]
    for name, ctype, expected in cases:
        att = types.SimpleNamespace(filename=name, content_type=ctype)
        got = ML._is_media(att)
        assert got == expected, f"{name} ({ctype}) -> {got}, expected {expected}"
        print(f"  {name:16} {str(ctype):20} -> {got}")

    # ---- summarise ----
    print("\n=== _summarise ===")
    F = ML.CachedFile
    for files, want in [
        ([F("a.png", b"", "image/png", 1, False)], "1 image"),
        ([F("a.png", b"", "image/png", 1, False), F("b.png", b"", "image/png", 1, False)], "2 images"),
        ([F("a.png", b"", "image/png", 1, False), F("v.mp4", b"", "video/mp4", 1, False)], "1 image, 1 video"),
        ([F("s.mp3", b"", "audio/mpeg", 1, False)], "1 audio file"),
    ]:
        got = ML._summarise(files)
        assert got == want, f"{got!r} != {want!r}"
        print(f"  {want}")

    # ---- cache + eviction ----
    print("\n=== cache eviction ===")
    def mk(mid, nbytes):
        e = ML.CachedMessage(
            guild_id=1, channel_id=2, author_id=3, author_tag="u#1", author_avatar=None,
            author_bot=False, content="hi", created_at=discord.utils.utcnow(),
            files=[F("x.png", b"0" * nbytes, "image/png", nbytes, False)],
            nbytes=nbytes, cached_at=time.monotonic())
        cog._cache[mid] = e
        cog._bytes += nbytes
        cog._evict()

    for i in range(ML.MAX_CACHE_ENTRIES + 25):
        mk(i, 1024)
    assert len(cog._cache) <= ML.MAX_CACHE_ENTRIES, len(cog._cache)
    print(f"  entry cap honoured: {len(cog._cache)} <= {ML.MAX_CACHE_ENTRIES}")
    assert cog._bytes == sum(e.nbytes for e in cog._cache.values()), "byte counter drifted"
    print(f"  byte counter consistent: {cog._bytes}")

    cog._cache.clear(); cog._bytes = 0
    big = ML.MAX_CACHE_BYTES // 4
    for i in range(10):
        mk(1000 + i, big)
    assert cog._bytes <= ML.MAX_CACHE_BYTES, cog._bytes
    print(f"  byte cap honoured: {ML._fmt_size(cog._bytes)} <= {ML._fmt_size(ML.MAX_CACHE_BYTES)}")
    assert cog._bytes == sum(e.nbytes for e in cog._cache.values()), "byte counter drifted"

    # oldest evicted first
    cog._cache.clear(); cog._bytes = 0
    for i in range(ML.MAX_CACHE_ENTRIES + 5):
        mk(i, 10)
    assert 0 not in cog._cache and 4 not in cog._cache, "oldest should have gone first"
    assert (ML.MAX_CACHE_ENTRIES + 4) in cog._cache, "newest should survive"
    print("  oldest-first order confirmed")

    # _drop keeps the counter straight
    cog._cache.clear(); cog._bytes = 0
    mk(555, 4096)
    before = cog._bytes
    cog._drop(555)
    assert cog._bytes == before - 4096 == 0, cog._bytes
    assert cog._drop(999) is None, "dropping an absent id should be safe"
    print("  _drop adjusts bytes and tolerates misses")

    # ---- embed building, with a real PNG so set_image is exercised ----
    print("\n=== embed ===")
    png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    entry = ML.CachedMessage(
        guild_id=1, channel_id=222, author_id=333, author_tag="someone#0001",
        author_avatar="https://e.com/u.png", author_bot=False,
        content="check this out", created_at=discord.utils.utcnow(),
        files=[
            F("holiday photo.png", png, "image/png", len(png), False),
            F("huge.mp4", None, "video/mp4", 50 * 1024 * 1024, False),
        ],
        nbytes=len(png), cached_at=time.monotonic())

    sent = {}
    class FakeChannel:
        id = 999
        async def send(self, **kw):
            sent.update(kw)
            return types.SimpleNamespace(id=1)
    cog._log_channel = lambda g, c: FakeChannel()

    await cog._send(None, {"medialog_channel": 999}, entry, "Mod#1 (`77`)", discord.utils.utcnow())

    e = sent["embed"]
    print(f"  title: {e.title}")
    print(f"  desc:  {e.description}")
    for f in e.fields:
        print(f"  [{f.name}] {f.value!r}")
    print(f"  image: {e.image.url}")
    print(f"  footer: {e.footer.text}")
    print(f"  files attached: {[f.filename for f in sent['files']]}")

    assert len(e) <= 6000, f"embed too long: {len(e)}"
    assert len(e.fields) <= 25
    assert len(sent["files"]) == 1, "only the retained file should be attached"
    # The inline image must reference the exact attachment filename, sanitised.
    assert e.image.url == f"attachment://{sent['files'][0].filename}", \
        f"{e.image.url} != attachment://{sent['files'][0].filename}"
    assert " " not in sent["files"][0].filename, "spaces break attachment:// refs"
    assert "not retained" in str([f.value for f in e.fields]), "oversized file should be marked"
    assert e.footer.text and "1 file" in e.footer.text
    print("  inline attachment reference matches OK")

    # spoilered image must NOT be inlined
    sent.clear()
    entry2 = ML.CachedMessage(
        guild_id=1, channel_id=222, author_id=333, author_tag="u#1", author_avatar=None,
        author_bot=True, content="", created_at=discord.utils.utcnow(),
        files=[F("nsfw.png", png, "image/png", len(png), True)],
        nbytes=len(png), cached_at=time.monotonic())
    await cog._send(None, {"medialog_channel": 999}, entry2, None, discord.utils.utcnow())
    e2 = sent["embed"]
    assert e2.image.url is None, "spoilers must stay blurred, not inlined"
    assert sent["files"][0].filename.startswith("SPOILER_")
    assert "(bot)" in [f.value for f in e2.fields][0], "bot uploader should be marked"
    assert "Unknown" in [f.value for f in e2.fields][1]
    assert "no text" in [f.value for f in e2.fields][3].lower(), "empty content placeholder"
    print("  spoiler not inlined, bot flagged, empty-content placeholder OK")

    print("\nALL CHECKS PASSED")

asyncio.run(main())
