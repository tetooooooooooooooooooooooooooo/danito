"""The insights page: the trend chart, the survival bars and the by-invite table.

The maths is the part worth pinning down, because every figure here is a ratio and the
denominator is where these go wrong. Somebody who joined yesterday cannot tell you anything
about 30 day retention, and counting them as a survivor flatters every number on the page.

The chart has a second trap: a group too young to measure has no rate at all. Drawing that as
zero would show a collapse that never happened, so it has to be a gap.
"""
import pathlib as _pathlib
ROOT = _pathlib.Path(__file__).resolve().parents[1]
WEB_DIR = str(ROOT / "web")

import datetime, html, os, sys, types
sys.path.insert(0, WEB_DIR)

os.environ.update({
    "DISCORD_CLIENT_ID": "123", "DISCORD_CLIENT_SECRET": "shh",
    "DISCORD_REDIRECT_URI": "https://example.test/callback",
    "BOT_TOKEN": "bot-token", "DASHBOARD_SECRET_KEY": "test-key",
    "DASHBOARD_INSECURE_COOKIES": "1",
})

NOW = datetime.datetime.now(datetime.timezone.utc)
SPELLS = []


class FakeCursor(list):
    def sort(self, key, direction=-1):
        return FakeCursor(sorted(self, key=lambda d: d[key], reverse=direction < 0))
    def limit(self, n):
        return FakeCursor(self[:n])


class FakeColl:
    def __init__(self, name): self.name = name
    def find(self, q=None, *a, **k):
        if self.name == "memberships":
            gid = (q or {}).get("guild_id")
            return FakeCursor([s for s in SPELLS if s["guild_id"] == gid])
        return FakeCursor([])
    def find_one(self, q=None, *a, **k):
        return {"_id": "bot", "guild_ids": [111]} if self.name == "runtime" else None
    def update_one(self, *a, **k): return types.SimpleNamespace(matched_count=1)


class FakeDB:
    def __getitem__(self, n): return FakeColl(n)


import store
store.db = lambda: FakeDB()

import discord_api as api
GUILDS = [{"id": "111", "name": "Test Server", "icon": None}]
api.manageable_guilds = lambda t, u, force=False: GUILDS
api.guild_channels = lambda g: []
api.guild_roles = lambda g: []

import app as dashboard
dashboard.app.config["TESTING"] = True


def spell(days_ago, stayed_days=None, code="promo", inviter="marcus", guild_id=111):
    """A membership. stayed_days=None means they are still here."""
    joined = NOW - datetime.timedelta(days=days_ago)
    left = None if stayed_days is None else joined + datetime.timedelta(days=stayed_days)
    return {"guild_id": guild_id, "user_id": len(SPELLS) + 1, "joined_at": joined,
            "left_at": left, "cohort": str(joined.date()), "invite_code": code,
            "inviter_id": 1, "inviter_name": inviter}


def load(*spells):
    SPELLS.clear()
    SPELLS.extend(spells)


def login(client):
    with client.session_transaction() as s:
        s["user"] = {"id": "7", "username": "Admin", "avatar": ""}
        s["token"] = "user-token"


def main():
    c = dashboard.app.test_client()
    login(c)

    print("=== only people old enough to measure are counted ===")
    load(
        spell(40, None),        # here 40 days, still around: survived every window
        spell(40, 2),           # left after 2 days: survived 1, not 7/14/30
        spell(3, None),         # only 3 days old: can only speak to the 1 day window
    )
    survival = {row["days"]: row for row in store.insights(111)["survival"]}
    assert survival[1]["measurable"] == 3 and survival[1]["survived"] == 3, survival[1]
    assert survival[7]["measurable"] == 2 and survival[7]["survived"] == 1, survival[7]
    assert survival[30]["measurable"] == 2 and survival[30]["survived"] == 1, survival[30]
    for days, row in survival.items():
        print(f"  after {days:>2} days: {row['survived']}/{row['measurable']} = {row['rate']}%")
    # The three day old member must not be counted as having survived a month.
    assert survival[30]["measurable"] == 2, "a recent join can't speak to a 30 day window"
    print("  a member too new for a window is left out of it entirely OK")

    print("\n=== a window nobody is old enough for says so, rather than 0% ===")
    load(spell(2, None))
    survival = {row["days"]: row for row in store.insights(111)["survival"]}
    assert survival[1]["rate"] == 100, survival[1]
    assert survival[30]["rate"] is None, survival[30]
    assert survival[30]["measurable"] == 0
    print("  30 day rate is None, not zero OK")

    print("\n=== leaving on the boundary counts as having lasted it ===")
    load(spell(30, 7))          # joined 30 days ago, left exactly 7 days later
    survival = {row["days"]: row for row in store.insights(111)["survival"]}
    assert survival[7]["survived"] == 1, "7 days served is 7 day retention"
    assert survival[14]["survived"] == 0, survival[14]
    print("  exactly 7 days counts for 7, not for 14 OK")

    print("\n=== by invite, sorted by who stays and not by who arrives ===")
    load(
        # A big invite that loses nearly everybody.
        *[spell(20, 1, code="twitter") for _ in range(10)],
        *[spell(20, None, code="twitter") for _ in range(2)],
        # A small one that keeps them.
        *[spell(20, None, code="youtube") for _ in range(4)],
        spell(20, 1, code="youtube"),
    )
    invites = store.retention_by_invite(111)
    codes = [row["code"] for row in invites["invites"]]
    assert codes == ["youtube", "twitter"], codes
    youtube = invites["invites"][0]
    twitter = invites["invites"][1]
    assert youtube["joins"] == 5 and youtube["rate"] == 80, youtube
    assert twitter["joins"] == 12 and twitter["rate"] == 17, twitter
    print(f"  youtube {youtube['joins']} joins at {youtube['rate']}% beats "
          f"twitter {twitter['joins']} at {twitter['rate']}% OK")

    print("\n=== joins with no invite are their own row, not dropped ===")
    load(spell(20, None, code="promo"), spell(20, None, code=None, inviter=None))
    invites = store.retention_by_invite(111)
    assert [r["code"] for r in invites["invites"]] == ["promo"], invites["invites"]
    assert invites["unknown"] is not None and invites["unknown"]["joins"] == 1
    assert invites["total"] == 2, invites["total"]
    print("  1 attributed, 1 unknown, 2 total OK")

    print("\n=== an invite too new to judge sorts last and shows no rate ===")
    load(
        *[spell(20, None, code="old") for _ in range(2)],
        spell(1, None, code="brandnew"),
    )
    invites = store.retention_by_invite(111)
    assert [r["code"] for r in invites["invites"]] == ["old", "brandnew"], invites["invites"]
    assert invites["invites"][1]["rate"] is None, "nothing measurable yet"
    print("  no rate rather than 0%, and it sorts below the ones that have one OK")

    print("\n=== the trend buckets by period and leaves gaps ===")
    load(
        spell(30, None), spell(30, 1),          # five weeks back: 50%
        spell(1, None),                          # this week: too young to measure
    )
    trend = store.retention_trend(111, "weekly")
    assert trend["period"] == "weekly"
    assert len(trend["points"]) == 12, len(trend["points"])
    rated = [p for p in trend["points"] if p["rate"] is not None]
    assert len(rated) == 1 and rated[0]["rate"] == 50, rated
    assert trend["points"][-1]["joins"] == 1, "this week's join is still counted"
    assert trend["points"][-1]["rate"] is None, "but it has no rate yet"
    assert trend["joins"] == 3, trend["joins"]
    print(f"  12 buckets, 1 with a rate ({rated[0]['rate']}%), the newest a gap OK")

    print("\n=== and reports which way it moved ===")
    load(
        # Eight weeks ago: 1 of 4 stayed. Two weeks ago: 3 of 4.
        *[spell(56, 1) for _ in range(3)], spell(56, None),
        *[spell(14, None) for _ in range(3)], spell(14, 1),
    )
    trend = store.retention_trend(111, "weekly")
    rated = [p["rate"] for p in trend["points"] if p["rate"] is not None]
    assert rated[0] == 25 and rated[-1] == 75, rated
    assert trend["change"] == 50, trend["change"]
    assert trend["latest"] == 75, trend["latest"]
    print(f"  {rated[0]}% to {rated[-1]}%, reported as {trend['change']:+} points OK")

    print("\n=== one rated bucket has no direction to report ===")
    load(spell(14, None))
    trend = store.retention_trend(111, "weekly")
    assert trend["change"] is None, "a single point is not a trend"
    assert trend["latest"] == 100
    print("  change is None rather than 0 OK")

    print("\n=== every period works, and a bad one falls back ===")
    load(spell(20, None), spell(20, 1))
    for period, expected in (("daily", 30), ("weekly", 12), ("monthly", 6)):
        trend = store.retention_trend(111, period)
        assert len(trend["points"]) == expected, (period, len(trend["points"]))
        print(f"  {period}: {expected} buckets OK")
    assert store.retention_trend(111, "hourly")["period"] == store.DEFAULT_TREND
    assert store.retention_trend(111, "'; drop--")["period"] == store.DEFAULT_TREND
    print("  anything unrecognised falls back to weekly OK")

    print("\n=== the chart geometry stays inside its box ===")
    load(*[spell(d, None if d % 2 else 1) for d in range(8, 80, 3)])
    chart = dashboard.trend_chart(store.retention_trend(111, "weekly"))
    assert chart["dots"], "something to draw"
    top, bottom = chart["top"], chart["h"] - chart["bottom"]
    for dot in chart["dots"]:
        assert top <= dot["y"] <= bottom, dot
        assert chart["left"] <= dot["x"] <= chart["w"] - chart["right"], dot
    # A polyline of one point draws nothing, so segments only exist where there are two.
    assert all(len(run) > 1 for run in chart["segments"]), chart["segments"]
    # The count doesn't matter, the spacing does: "18 May" is about 38px wide, so anything
    # under roughly that would overlap the next one.
    for period in store.TREND_PERIODS:
        labels = dashboard.trend_chart(store.retention_trend(111, period))["labels"]
        gaps = [b["x"] - a["x"] for a, b in zip(labels, labels[1:])]
        assert labels, period
        assert not gaps or min(gaps) >= 45, (period, min(gaps), len(labels))
        print(f"  {period}: {len(labels)} labels, closest {min(gaps) if gaps else '-'}px apart")
    print(f"  {len(chart['dots'])} points, {len(chart['segments'])} line segments, "
          f"all within bounds OK")

    print("\n=== a gap really does break the line ===")
    # Two runs of rated weeks with silent weeks between them. The line must not be drawn
    # across the hole, which would invent a slope through weeks nobody joined in.
    load(*[s for days in (45, 38, 17, 10)
           for s in (spell(days, None), spell(days, 1))])
    chart = dashboard.trend_chart(store.retention_trend(111, "weekly"))
    assert len(chart["dots"]) == 4, chart["dots"]
    assert len(chart["segments"]) == 2, chart["segments"]
    assert all(len(run) == 2 for run in chart["segments"]), chart["segments"]
    # And the hole is real: the gap between the runs is wider than a single step.
    step = chart["segments"][0][1]["x"] - chart["segments"][0][0]["x"]
    hole = chart["segments"][1][0]["x"] - chart["segments"][0][-1]["x"]
    assert hole > step, (hole, step)
    print(f"  two segments of two, separated by {hole:.0f}px against a {step:.0f}px step OK")

    print("\n=== a lone rated bucket still shows up ===")
    # It cannot be a line, so it has to survive as a dot or it vanishes off the chart.
    load(spell(35, None), spell(35, 1))
    chart = dashboard.trend_chart(store.retention_trend(111, "weekly"))
    assert chart["segments"] == [], chart["segments"]
    assert len(chart["dots"]) == 1, chart["dots"]
    print("  no line, but the point is drawn OK")

    print("\n=== the page renders ===")
    load(*[spell(d, None if d % 3 else 2, code="promo" if d % 2 else "other")
           for d in range(2, 60)])
    r = c.get("/servers/111/insights")
    assert r.status_code == 200, r.status_code
    body = html.unescape(r.data.decode())
    assert "<svg" in body and 'class="trend"' in body
    assert "polyline" in body, "a line got drawn"
    assert "Which invite they came through" in body
    assert "promo" in body and "other" in body
    assert "7 day retention" in body
    print("  chart, invite table and headline figures all present OK")

    print("\n=== a server with no data still gets a chart ===")
    load()
    body = html.unescape(c.get("/servers/111/insights").data.decode())
    # The axes are drawn either way. A paragraph where a chart should be reads as broken, and
    # the page would change shape under somebody the moment their first member joined.
    assert "<svg" in body, "the chart is drawn empty, not skipped"
    assert 'class="trend bare"' in body, "and marked as empty so it can be styled back"
    assert "Nothing recorded yet" in body
    # Empty means empty: axes and labels, but nothing plotted on them.
    assert "polyline" not in body and "<circle" not in body, "nothing to plot"
    assert body.count("gridline") == 5, "the axis lines are still there"
    # And it must not nag about a permission on a server where nobody has joined at all.
    assert "Manage Server" not in body
    print("  empty axes, a label saying so, and no permission nag OK")

    print("\n=== joins too recent to measure get a chart too ===")
    load(spell(1, None), spell(2, None))
    body = html.unescape(c.get("/servers/111/insights").data.decode())
    assert 'class="trend bare"' in body
    assert "No group here is a week old yet" in body
    assert "2 joined over this period" in body, "the joins still get counted"
    print("  drawn empty, but it says the joins landed OK")

    print("\n=== and it says why nothing is attributed ===")
    load(spell(10, None, code=None, inviter=None))
    body = html.unescape(c.get("/servers/111/insights").data.decode())
    assert "Manage Server" in body, "it has to say which permission is missing"
    assert "add it again" in body
    # Once anything is attributed the notice goes, rather than nagging forever.
    load(spell(10, None, code="promo"))
    assert "Manage Server" not in html.unescape(c.get("/servers/111/insights").data.decode())
    print("  the permission notice appears only while it's needed OK")

    print("\n=== it's reachable from the settings sidebar ===")
    body = c.get("/servers/111").data.decode()
    assert '/servers/111/insights' in body, "no way in from the settings page"
    # It sits in the tab list but is not a tab. The script switches panes for anything with
    # data-tab, so marking this one would leave a sixth entry that swallows the click and
    # shows nothing. Below Ratings, which is the last real tab.
    assert 'data-tab="insights"' not in body, "it's a page, not a pane"
    assert body.index('data-tab="survey"') < body.index('/servers/111/insights'), \
        "it belongs under Ratings, not above the tabs"
    print("  in the sidebar under Ratings, and not registered as a tab OK")

    print("\n=== and it's behind the same check as the settings page ===")
    anon = dashboard.app.test_client()
    r = anon.get("/servers/111/insights")
    assert r.status_code == 302 and "/login" in r.headers["Location"], r.status_code
    # A server this user doesn't manage is a 404, the same as everywhere else, so the page
    # isn't a way to probe which servers exist.
    r = c.get("/servers/999/insights")
    assert r.status_code == 404, r.status_code
    print("  logged out redirects, somebody else's server is a 404 OK")

    print("\nALL CHECKS PASSED")


main()
