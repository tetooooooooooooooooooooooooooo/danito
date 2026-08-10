"""Site plumbing: what crawlers are told, and what happens when something breaks.

The sitemap is built from endpoint names rather than paths, so the risk it carries is a
renamed route leaving a dead entry behind. That is checked here by fetching every url it
advertises rather than by comparing it to a list written twice.

The 500 handler matters more than it looks. Without one, a bug shows Flask's own page, which
carries a traceback and looks nothing like the rest of the site. On a site asking people for
money that is the worst possible moment to look broken.
"""
import pathlib as _pathlib
ROOT = _pathlib.Path(__file__).resolve().parents[1]
WEB_DIR = str(ROOT / "web")

import html, logging, os, sys, types
import xml.etree.ElementTree as ET
sys.path.insert(0, WEB_DIR)

os.environ.update({
    "DISCORD_CLIENT_ID": "123", "DISCORD_CLIENT_SECRET": "shh",
    "DISCORD_REDIRECT_URI": "https://example.test/callback",
    "BOT_TOKEN": "bot-token", "DASHBOARD_SECRET_KEY": "test-key",
    "DASHBOARD_INSECURE_COOKIES": "1",
})


# What the bot's heartbeat has written, if anything. The front page reads this.
RUNTIME = {}


class FakeColl:
    def __init__(self, name): self.name = name
    def find_one(self, *a, **k):
        return dict(RUNTIME) if self.name == "runtime" and RUNTIME else None
    def find(self, *a, **k): return []
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
# Otherwise Flask re-raises rather than letting the handler under test answer.
dashboard.app.config["PROPAGATE_EXCEPTIONS"] = False

SENTINEL = "a very distinctive string that must never reach a browser"


@dashboard.app.route("/_test_crash")
def _crash():
    raise RuntimeError(SENTINEL)


def main():
    c = dashboard.app.test_client()

    print("=== robots.txt ===")
    r = c.get("/robots.txt")
    assert r.status_code == 200, r.status_code
    assert r.mimetype == "text/plain", r.mimetype
    body = r.data.decode()
    assert "User-agent: *" in body
    for path in dashboard.PRIVATE_PATHS:
        assert f"Disallow: {path}" in body, path
    assert "Allow: /" in body
    assert "Sitemap: " in body
    print(f"  served, with {len(dashboard.PRIVATE_PATHS)} paths kept out and the sitemap named OK")

    print("\n=== the sitemap is valid xml, not a string that looks like it ===")
    r = c.get("/sitemap.xml")
    assert r.status_code == 200, r.status_code
    assert "xml" in r.mimetype, r.mimetype
    root = ET.fromstring(r.data)          # raises if it isn't well formed
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    assert root.tag == ns + "urlset", root.tag
    locs = [el.text for el in root.iter(ns + "loc")]
    assert len(locs) == len(dashboard.PUBLIC_PAGES), (locs, dashboard.PUBLIC_PAGES)
    print(f"  {len(locs)} urls, parsed OK")

    print("\n=== every url in it actually exists ===")
    # The whole point of building this from endpoint names: a renamed route should break the
    # suite here rather than send a crawler at a 404 for a month.
    for loc in locs:
        path = "/" + loc.split("/", 3)[3] if loc.count("/") > 2 else "/"
        got = c.get(path)
        assert got.status_code == 200, (loc, path, got.status_code)
        print(f"  {path or '/'} -> 200 OK")

    print("\n=== and nothing private is advertised ===")
    for path in dashboard.PRIVATE_PATHS:
        assert not any(loc.rstrip("/").endswith(path) for loc in locs), path
    print("  no logged in pages in the sitemap OK")

    print("\n=== the urls are absolute and force https off localhost ===")
    os.environ["SITE_URL"] = "http://newt.example"
    try:
        locs = [el.text for el in ET.fromstring(c.get("/sitemap.xml").data).iter(ns + "loc")]
        assert all(loc.startswith("https://newt.example") for loc in locs), locs
        sitemap_line = [ln for ln in c.get("/robots.txt").data.decode().splitlines()
                        if ln.startswith("Sitemap:")][0]
        assert "https://newt.example/sitemap.xml" in sitemap_line, sitemap_line
        print(f"  {locs[0]} OK")
    finally:
        del os.environ["SITE_URL"]

    print("\n=== a crash gets the site's own page, not Flask's ===")
    # Caught rather than left to print, both to keep this suite's output readable and because
    # the traceback reaching the log is half the point of the handler: a page that says sorry
    # and tells nobody is how a fault stays unfixed.
    caught = []

    class Capture(logging.Handler):
        def emit(self, record):
            caught.append(record)

    dashboard.app.logger.addHandler(Capture())
    dashboard.app.logger.propagate = False

    r = c.get("/_test_crash")
    assert r.status_code == 500, r.status_code
    assert caught, "the crash was swallowed without being logged"
    logged = caught[0]
    assert logged.exc_info and logged.exc_info[1].args[0] == SENTINEL, logged.exc_info
    assert "/_test_crash" in logged.getMessage(), logged.getMessage()
    print(f"  logged as: {logged.getMessage()}, with the traceback attached OK")
    body = html.unescape(r.data.decode())
    assert "Something broke at our end" in body
    # The two things Flask's default page would show and this one must not.
    assert SENTINEL not in body, "the exception message leaked to the browser"
    assert "Traceback" not in body, "the traceback leaked to the browser"
    # It is a real page, so it carries the header and footer like everything else.
    assert "Terms of Service" in body and "/support" in body
    print("  500, styled, and nothing leaked OK")

    print("\n=== while the other handlers keep their own wording ===")
    # Registering on Exception is broad enough to swallow every 404 if it isn't careful.
    r = c.get("/definitely-not-a-page")
    assert r.status_code == 404, r.status_code
    assert "Something broke at our end" not in html.unescape(r.data.decode())
    print("  404 still reads as a 404 OK")

    print("\n=== premium is reachable from the pitch, not just the nav ===")
    body = html.unescape(c.get("/").data.decode())
    plans = dashboard.premium_plans()
    assert "What it costs" in body
    for plan in plans:
        assert plan["price"] in body, plan["price"]
    assert "/premium" in body
    print(f"  the landing page names {plans[0]['price']} and {plans[1]['price']} OK")

    print("\n=== the front page claims nothing until the bot has checked in ===")
    global RUNTIME
    assert not RUNTIME, "this suite has run so far with no heartbeat at all"
    assert store.headline_numbers() == {}, store.headline_numbers()
    body = html.unescape(c.get("/").data.decode())
    assert 'class="tallies"' not in body, "a fresh install must not advertise zeroes"
    print("  no heartbeat, no numbers, no band OK")

    print("\n=== and shows them once it has ===")
    import datetime
    RUNTIME.update({"_id": "bot", "guild_count": 7, "member_count": 104382,
                    "last_seen": datetime.datetime.now(datetime.timezone.utc)})
    numbers = store.headline_numbers()
    # Two figures, both already in the heartbeat, so the page costs no extra query.
    assert numbers == {"servers": 7, "members": 104382}, numbers
    body = html.unescape(c.get("/").data.decode())
    assert 'class="tallies"' in body
    for expected in ("Servers", "7", "Members watched", "104k"):
        assert expected in body, expected
    assert "Joins recorded" not in body, "the third tile was dropped"
    print("  7 servers and 104k members, compact, and nothing else OK")

    print("\n=== a number it doesn't have is left out rather than shown as nought ===")
    RUNTIME["member_count"] = 0
    assert store.headline_numbers() == {"servers": 7}, store.headline_numbers()
    body = html.unescape(c.get("/").data.decode())
    assert "Servers" in body and 'class="tallies"' in body
    assert "Members watched" not in body, "0 members is worse than no claim"
    print("  only the tile that has a real number survives OK")
    RUNTIME.clear()

    print("\n=== the numbers are written the way people read them ===")
    for value, expected in ((0, "0"), (7, "7"), (999, "999"), (1000, "1k"), (1234, "1.2k"),
                            (12345, "12k"), (996214, "996k"), (1234567, "1.2M"),
                            (2_500_000_000, "2.5B")):
        assert store.compact(value) == expected, (value, store.compact(value), expected)
    # Anything that isn't a number at all becomes nothing, not the word None.
    for junk in (None, "", "lots", [1]):
        assert store.compact(junk) == "", junk
    print("  996214 reads as 996k, and junk reads as nothing OK")

    print("\n=== and the docs say which parts are free ===")
    import docs
    section = next((s for s in docs.SECTIONS if s["id"] == "premium"), None)
    assert section, "the docs have a premium section"
    assert section["commands"] == [], "it has no commands, and the count depends on that key"
    body = html.unescape(c.get("/docs").data.decode())
    assert 'id="premium"' in body
    assert "/premium" in body, "and links the pricing page"
    print("  in the contents and the body OK")

    print("\nALL CHECKS PASSED")


main()
