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
    # The oversized one is named, with the reason on its own line rather than a vague footer.
    assert "over 8 MB, not kept" in str([f.value for f in e.fields]), [f.value for f in e.fields]
    assert e.footer.text and "1 of 2 files kept" in e.footer.text, e.footer.text
    assert str(entry.author_id) in e.footer.text, "the id stays reachable, just out of the way"
    print("  inline attachment reference matches OK")

    # The author is named once, in the header, rather than again in a field underneath.
    assert e.author.name.startswith("someone#0001")
    assert not any(f.name == "Uploaded by" for f in e.fields), "that field was a duplicate"
    assert not any(f.name == "Posted" for f in e.fields), "the time is in the description now"
    assert "posted <t:" in e.description and "deleted by Mod#1" in e.description
    assert e.description.startswith("🖼️"), e.description
    # A caption is shown when there is one, and this entry has one.
    assert [f.value for f in e.fields if f.name == "Caption"] == ["check this out"]
    print(f"  {len(e.fields)} fields, down from five OK")

    print("\n=== one picture that survived needs no list under it ===")
    sent.clear()
    solo = ML.CachedMessage(
        guild_id=1, channel_id=222, author_id=333, author_tag="someone#0001",
        author_avatar="https://e.com/u.png", author_bot=False,
        content="", created_at=discord.utils.utcnow(),
        files=[F("one.png", png, "image/png", len(png), False)],
        nbytes=len(png), cached_at=time.monotonic())
    await cog._send(None, {"medialog_channel": 999}, solo, None, discord.utils.utcnow())
    e3 = sent["embed"]
    assert e3.image.url, "it is still shown"
    assert e3.fields == [], f"nothing to say twice: {[f.name for f in e3.fields]}"
    assert "probably the author" in e3.description, "an unnamed deleter is the ordinary case"
    assert "couldn't be kept" not in (e3.footer.text or "")
    print("  a lone retained image renders as the image and nothing else OK")

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
    assert e2.author.name.endswith("· bot"), f"bot uploader should be marked: {e2.author.name}"
    assert "probably the author" in e2.description
    # No caption on this one, so no field pretending there is one.
    assert not any(f.name == "Caption" for f in e2.fields), "an empty caption is not a field"
    # The spoiler was not inlined, so the list has to name it or nothing does.
    assert [f.name for f in e2.fields] == ["Files"], [f.name for f in e2.fields]
    print("  spoiler not inlined, bot flagged in the header, no empty caption row OK")

    print("\n=== a file we couldn't hold says which reason ===")
    assert "over 8 MB" in ML._not_kept(F("big.png", None, "image/png", 9 * 1024 * 1024, False))
    assert "before a restart" in ML._not_kept(F("old.png", None, "image/png", 500, False))
    print("  too large and posted-before-a-restart read differently OK")

    print("\n=== a bulk delete logs every message, not one per person ===")
    # The bug this covers: entries recovered from our own byte cache were deduped against
    # discord.py's cache by AUTHOR, so a purge of one person's images logged whichever one we
    # happened to hold and silently dropped the rest.
    posts = []
    class BulkChannel:
        id = 999
        async def send(self, **kw):
            posts.append(kw)
            return types.SimpleNamespace(id=1)
    cog._log_channel = lambda g, c: BulkChannel()
    async def _cfg(*a, **k): return {"medialog_channel": 999, "medialog_enabled": True}
    async def _nobody(*a, **k): return None
    cog._get_config = _cfg
    cog._who_deleted = _nobody
    ML.AUDIT_DELAY = 0

    # One message we hold the bytes for, two more from the same person that only discord.py
    # remembers. All three were deleted in the same sweep.
    cog._cache.clear(); cog._bytes = 0
    held = ML.CachedMessage(
        guild_id=1, channel_id=222, author_id=333, author_tag="spammer#1",
        author_avatar=None, author_bot=False, content="", created_at=discord.utils.utcnow(),
        files=[F("a.png", png, "image/png", len(png), False)],
        nbytes=len(png), cached_at=time.monotonic())
    cog._cache[1001] = held
    cog._bytes = len(png)

    def cached_msg(mid, name):
        return types.SimpleNamespace(
            id=mid, guild=types.SimpleNamespace(id=1),
            channel=types.SimpleNamespace(id=222),
            author=types.SimpleNamespace(id=333, bot=False,
                                         display_avatar=types.SimpleNamespace(url=None),
                                         __str__=lambda s: "spammer#1"),
            content="", created_at=discord.utils.utcnow(),
            attachments=[types.SimpleNamespace(
                filename=name, content_type="image/png", size=1234,
                is_spoiler=lambda: False)])

    payload = types.SimpleNamespace(
        guild_id=1, channel_id=222,
        message_ids={1001, 1002, 1003},
        # discord.py hands back everything it remembers, including the one we already hold.
        cached_messages=[cached_msg(1001, "a.png"), cached_msg(1002, "b.png"),
                         cached_msg(1003, "c.png")])
    posts.clear()
    await cog.on_raw_bulk_message_delete(payload)

    # One summary, then one entry per message. Three messages went, so three get logged.
    assert len(posts) == 4, f"summary plus three entries, got {len(posts)}: " \
                            f"{[p['embed'].title for p in posts]}"
    assert posts[0]["embed"].title == "Bulk delete"
    named = [p["embed"] for p in posts[1:]]
    listed = " ".join(str([f.value for f in e.fields]) + (e.image.url or "") for e in named)
    for expected in ("a.png", "b.png", "c.png"):
        assert expected in listed, f"{expected} missing from the log: {listed}"
    # And the one we held bytes for is the only one with a file attached.
    attached = [p for p in posts[1:] if p.get("files")]
    assert len(attached) == 1, "only the cached message had bytes behind it"
    print(f"  3 deleted, {len(named)} logged, 1 with the file itself OK")

    print("\n=== and never logs the same message twice ===")
    # 1001 is in both caches. It must not produce two entries.
    titles = [p["embed"].title for p in posts[1:]]
    assert len(titles) == 3, titles
    print("  the one held in both caches appears once OK")

    print("\n=== sizes read like sizes ===")
    for n, want in ((0, "0 B"), (900, "900 B"), (1024, "1 KB"), (1536, "1.5 KB"),
                    (1024 * 1024, "1 MB"), (3 * 1024 * 1024, "3 MB"),
                    (int(3.25 * 1024 * 1024), "3.2 MB")):
        got = ML._fmt_size(n)
        assert got == want, (n, got, want)
    print("  1 MB rather than 1.0 MB OK")

    print("\nALL CHECKS PASSED")

asyncio.run(main())
