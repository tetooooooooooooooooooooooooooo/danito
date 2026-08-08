"""Link previews: the tags Discord reads when somebody shares the site.

This bot is shared in Discord, which unfurls every url. Getting these wrong is invisible from
the site itself and only shows up as a bare grey box in somebody else's channel, which is
exactly the kind of thing that rots without a test.

The image is checked by reading the PNG header directly, so the suite needs no Pillow. Pillow
is only used by tools/make_og_image.py, which is run by hand when the branding changes.
"""
import pathlib as _pathlib
# Resolved from this file so the suite runs from a clone, on any machine, from any cwd.
ROOT = _pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / "src")
WEB_DIR = str(ROOT / "web")

import html, os, re, struct, sys, types
sys.path.insert(0, WEB_DIR)

os.environ.update({
    "DISCORD_CLIENT_ID": "123", "DISCORD_CLIENT_SECRET": "shh",
    "DISCORD_REDIRECT_URI": "https://example.test/callback",
    "BOT_TOKEN": "bot-token", "DASHBOARD_SECRET_KEY": "test-key",
    "DASHBOARD_INSECURE_COOKIES": "1",
})


class FakeColl:
    def __init__(self, name): self.name = name
    def find_one(self, q, *a, **k):
        return {"_id": "bot", "guild_ids": []} if self.name == "runtime" else None
    def find(self, q=None, *a, **k): return []
    def update_one(self, *a, **k): return types.SimpleNamespace(matched_count=1)


class FakeDB:
    def __getitem__(self, n): return FakeColl(n)


import store
store.db = lambda: FakeDB()

import discord_api as api
api.manageable_guilds = lambda t, u, force=False: []
api.guild_channels = lambda g: []
api.guild_roles = lambda g: []

import app as dashboard
dashboard.app.config["TESTING"] = True

PUBLIC = ("/", "/docs", "/status")


def meta(body: str) -> dict:
    """Every meta tag on the page, by name or property."""
    found = {}
    for tag in re.findall(r"<meta[^>]*>", body):
        key = re.search(r'(?:name|property)="([^"]+)"', tag)
        value = re.search(r'content="([^"]*)"', tag)
        if key and value:
            found[key.group(1)] = html.unescape(value.group(1))
    return found


def main():
    c = dashboard.app.test_client()

    print("=== the image exists and is the shape every unfurler wants ===")
    png = ROOT / "web" / "static" / "og.png"
    assert png.exists(), "run tools/make_og_image.py"
    raw = png.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    # Width and height live in the IHDR chunk, at a fixed offset.
    width, height = struct.unpack(">II", raw[16:24])
    assert (width, height) == (1200, 630), (width, height)
    size = len(raw) / 1024
    assert size < 900, f"{size:.0f} KB is too heavy for a preview"
    print(f"  {width}x{height}, {size:.0f} KB OK")

    print("\n=== every public page carries a full set ===")
    needed = ("description", "og:title", "og:description", "og:url", "og:image",
              "og:type", "og:site_name", "twitter:card", "twitter:image")
    for path in PUBLIC:
        tags = meta(c.get(path).data.decode())
        missing = [n for n in needed if not tags.get(n)]
        assert not missing, (path, missing)
        assert tags["twitter:card"] == "summary_large_image", tags["twitter:card"]
        print(f"  {path:<8} {len(needed)} tags, title {tags['og:title']!r}")

    print("\n=== the urls are absolute, since a relative one is never followed ===")
    for path in PUBLIC:
        tags = meta(c.get(path).data.decode())
        for key in ("og:url", "og:image", "twitter:image"):
            assert tags[key].startswith("http"), (path, key, tags[key])
        assert tags["og:image"].endswith("/static/og.png"), tags["og:image"]
        assert tags["og:url"].rstrip("/").endswith(path.rstrip("/")), (path, tags["og:url"])
    print("  absolute on every page, and og:url follows the page OK")

    print("\n=== https is forced off localhost ===")
    # Heroku terminates TLS in front of the dyno and hands the app plain http, which would
    # otherwise put http:// into every shared link.
    tags = meta(c.get("/", base_url="http://newt.example").data.decode())
    assert tags["og:url"].startswith("https://newt.example"), tags["og:url"]
    assert tags["og:image"].startswith("https://newt.example"), tags["og:image"]
    local = meta(c.get("/", base_url="http://127.0.0.1:5055").data.decode())
    assert local["og:url"].startswith("http://127.0.0.1"), local["og:url"]
    print(f"  {tags['og:url']}, and localhost left alone OK")

    print("\n=== SITE_URL wins when it is set ===")
    os.environ["SITE_URL"] = "https://newt.gg/"
    tags = meta(c.get("/docs", base_url="http://newt.example").data.decode())
    assert tags["og:url"] == "https://newt.gg/docs", tags["og:url"]
    assert tags["og:image"] == "https://newt.gg/static/og.png", tags["og:image"]
    os.environ.pop("SITE_URL")
    print("  overrides the request host OK")

    print("\n=== each page says something different ===")
    seen = {}
    for path in PUBLIC:
        tags = meta(c.get(path).data.decode())
        text = tags["description"]
        assert text not in seen, f"{path} repeats the description from {seen.get(text)}"
        seen[text] = path
        # Discord shows roughly the first 300 characters before cutting it off.
        assert 60 <= len(text) <= 300, (path, len(text))
    print(f"  {len(seen)} distinct descriptions, all inside the length Discord shows OK")

    print("\n=== the description does not leak into the page ===")
    # It is defined as a block inside the meta tag; defining it anywhere else prints it as
    # loose text between </head> and <body>.
    body = c.get("/").data.decode()
    after_head = body.split("</head>", 1)[1]
    assert "Newt asks new members what they think" not in after_head, \
        "the description block is rendering into the document"
    assert body.count('name="description"') == 1, "one description tag, not several"
    print("  only in the tag OK")

    print("\n=== the embed stripe is the brand colour ===")
    # Discord colours the left edge of the embed with theme-color, so a near-black one reads
    # as no colour at all.
    tags = meta(c.get("/").data.decode())
    assert tags["theme-color"].lower() == "#3ddc97", tags["theme-color"]
    print(f"  {tags['theme-color']} OK")

    print("\nALL CHECKS PASSED")


main()
