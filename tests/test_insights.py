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

    print("\n=== joins and leaves are counted in their own buckets ===")
    # Somebody who joined five weeks ago and left last week is one join in one bucket and one
    # leave in another, not both in the week they arrived. Counting a leave against the
    # joining week would answer the question the survival bars already answer.
    load(spell(35, 28))
    activity = store.activity_trend(111, "weekly")
    joins_at = [i for i, p in enumerate(activity["points"]) if p["joins"]]
    leaves_at = [i for i, p in enumerate(activity["points"]) if p["leaves"]]
    assert joins_at and leaves_at and joins_at != leaves_at, (joins_at, leaves_at)
    assert activity["joins"] == 1 and activity["leaves"] == 1, activity
    print(f"  join in bucket {joins_at[0]}, leave in bucket {leaves_at[0]} OK")

    print("\n=== somebody still here is never counted as a leave ===")
    load(spell(10, None), spell(10, None))
    activity = store.activity_trend(111, "weekly")
    assert activity["joins"] == 2 and activity["leaves"] == 0, activity
    print("  2 joins, 0 leaves OK")

    print("\n=== the axis scales to the numbers, in whole people ===")
    for peak, expected_top in ((0, 4), (1, 4), (4, 4), (5, 5), (8, 8), (9, 10), (12, 15),
                               (37, 40), (140, 150), (413, 500), (2100, 2500)):
        top, ticks = dashboard.count_axis(peak)
        assert top == expected_top, (peak, top, expected_top)
        assert ticks[0] == 0 and ticks[-1] == top, ticks
        assert len(ticks) <= 6, ticks
        assert all(isinstance(t, int) for t in ticks), "half a person is not a tick"
        assert top >= peak, "the tallest point has to fit under the ceiling"
        # Evenly spaced, or the gridlines would lie about the scale.
        gaps = {b - a for a, b in zip(ticks, ticks[1:])}
        assert len(gaps) == 1, (peak, ticks)
        print(f"  peak {peak:>4} -> 0 to {top} in {len(ticks) - 1} steps of {gaps.pop()}")

    print("\n=== zero is a point on the floor, not a gap ===")
    # Unlike retention, where a bucket can have no answer at all, a day nobody joined is a
    # real zero. The line has to stay unbroken or it would imply missing data.
    load(spell(35, None), spell(7, None))
    chart = dashboard.activity_chart(store.activity_trend(111, "weekly"), "joins")
    line = chart["lines"][0]
    assert len(line["points"]) == 12, "every bucket gets a point"
    assert sum(1 for p in line["points"] if p["value"] == 0) == 10, line["points"]
    assert all(p["y"] == chart["baseline"] for p in line["points"] if p["value"] == 0)
    print("  12 points, 10 of them sitting on the baseline OK")

    print("\n=== the geometry stays inside its box, on every view ===")
    load(*[spell(d, None if d % 2 else 3) for d in range(2, 80)])
    for period in store.TREND_PERIODS:
        for series in store.SERIES:
            chart = dashboard.activity_chart(store.activity_trend(111, period), series)
            expected_lines = 2 if series == "both" else 1
            assert len(chart["lines"]) == expected_lines, (period, series, chart["lines"])
            for line in chart["lines"]:
                for point in line["points"]:
                    assert chart["top"] <= point["y"] <= chart["h"] - chart["bottom"], point
                    assert chart["left"] <= point["x"] <= chart["w"] - chart["right"], point
        # The count of labels doesn't matter, the spacing does: "18 May" is about 38px wide,
        # so anything tighter than that would collide with the next one.
        labels = chart["labels"]
        gaps = [b["x"] - a["x"] for a, b in zip(labels, labels[1:])]
        assert labels and (not gaps or min(gaps) >= 45), (period, gaps)
        print(f"  {period}: {len(labels)} labels, closest {min(gaps) if gaps else '-'}px apart")

    print("\n=== both series share one axis ===")
    # Drawn against separate scales they would be uncomparable, which is the entire point of
    # putting them on the same chart.
    load(*[spell(20, None) for _ in range(30)], spell(20, 1))
    activity = store.activity_trend(111, "weekly")
    # 31 joins in one week against a single leave: the peak follows the taller series.
    assert activity["peak"] == 31, activity["peak"]
    assert activity["joins"] == 31 and activity["leaves"] == 1, activity
    both = dashboard.activity_chart(activity, "both")
    joins_only = dashboard.activity_chart(activity, "joins")
    assert both["gridlines"] == joins_only["gridlines"], "the scale must not move"
    print(f"  peak {activity['peak']}, same gridlines whichever lines are shown OK")

    print("\n=== the page renders, and the toggles pick the lines ===")
    load(*[spell(d, None if d % 3 else 2, code="promo" if d % 2 else "other")
           for d in range(2, 60)])
    for series, expect, forbid in (("joins", "line joins", "line leaves"),
                                   ("leaves", "line leaves", "line joins"),
                                   ("both", "line leaves", None)):
        r = c.get(f"/servers/111/insights?series={series}")
        assert r.status_code == 200, (series, r.status_code)
        body = html.unescape(r.data.decode())
        assert expect in body, (series, expect)
        if forbid:
            assert forbid not in body, (series, forbid)
        assert f'class="trend {series}"' in body, series
        print(f"  {series}: {expect} drawn{', ' + forbid + ' not' if forbid else ''} OK")

    body = html.unescape(c.get("/servers/111/insights?series=both").data.decode())
    assert "line joins" in body and "line leaves" in body, "both means both"
    assert "Net" in body, "and the combined view does the subtraction"
    print("  both: two lines and a net figure OK")

    print("\n=== a bogus series falls back rather than erroring ===")
    for bad in ("nonsense", "", "'; drop--"):
        r = c.get(f"/servers/111/insights?series={bad}")
        assert r.status_code == 200, (bad, r.status_code)
        assert f'class="trend {store.DEFAULT_SERIES}"' in r.data.decode(), bad
    print(f"  falls back to {store.DEFAULT_SERIES} OK")

    print("\n=== the rest of the page is still there ===")
    body = html.unescape(c.get("/servers/111/insights").data.decode())
    assert "Which invite they came through" in body
    assert "promo" in body and "other" in body
    # Retention did not disappear when it stopped being the chart.
    assert "7 day retention" in body and "How long people last" in body
    print("  invite table, survival bars and the retention figure all present OK")

    print("\n=== a server with no data still gets a chart ===")
    load()
    body = html.unescape(c.get("/servers/111/insights").data.decode())
    # The axes are drawn either way. A paragraph where a chart should be reads as broken, and
    # the page would change shape under somebody the moment their first member joined.
    assert "<svg" in body, "the chart is drawn empty, not skipped"
    assert "trend joins bare" in body, "and marked as empty so it can be styled back"
    assert "Nobody joined or left in this period" in body
    # Empty means empty: axes and labels, but nothing plotted on them.
    assert "polyline" not in body and "<circle" not in body, "nothing to plot"
    assert body.count("gridline") == 5, "the axis lines are still there"
    # Whole numbers on the axis even with nothing to scale to, rather than 0.25 of a person.
    chart = dashboard.activity_chart(store.activity_trend(111), "joins")
    assert [g["value"] for g in chart["gridlines"]] == [0, 1, 2, 3, 4], chart["gridlines"]
    # And it must not nag about a permission on a server where nobody has joined at all.
    assert "Manage Server" not in body
    print("  empty axes labelled 0 to 4, a note saying so, and no permission nag OK")

    print("\n=== but a single join is enough to draw ===")
    # The old chart stayed blank until a group was a week old. This one has something to say
    # the moment anybody arrives, which is the point of counting joins rather than rates.
    load(spell(1, None), spell(2, None))
    body = html.unescape(c.get("/servers/111/insights").data.decode())
    assert "bare" not in body, "two joins is a chart"
    assert "line joins" in body
    assert "Nobody joined or left" not in body
    print("  two joins on day one and the line is already there OK")

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
