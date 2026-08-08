"""The dashboard.

Runs as a separate process from the bot and shares only MongoDB. Everything security-relevant
is deliberately re-checked per request rather than trusted from the session:

- The OAuth callback verifies a `state` value it generated, so another site can't complete a
  login on somebody's behalf.
- Every guild page and every save re-asks Discord whether this user really can manage that
  guild. Being shown a server in the picker earlier is not permission to edit it later.
- Form posts carry a token tied to the session, so a link on another site can't make a logged
  in admin change their settings.
- Submitted channel ids are checked against that guild's real channels, so a crafted post
  can't aim the bot at a channel somewhere else.
"""

import os
import secrets
from functools import wraps

from dotenv import load_dotenv

# Must run before discord_api is imported: that module reads its credentials at import time.
# On Heroku the real environment already exists and load_dotenv leaves it alone, so this only
# matters when running locally from a .env file.
load_dotenv()

from flask import (Flask, abort, flash, redirect, render_template, request,  # noqa: E402
                   session, url_for)

import discord_api as api                                                    # noqa: E402
import docs                                                                  # noqa: E402
import store                                                                 # noqa: E402

app = Flask(__name__)
app.secret_key = os.environ.get("DASHBOARD_SECRET_KEY") or secrets.token_hex(32)

# The bot's own name inside Discord comes from the application itself, so it follows a rename
# in the Developer Portal without a deploy. Only the web pages need telling.
BRAND = os.environ.get("DASHBOARD_BRAND", "Newt")

# The permissions the invite asks for, in one place rather than repeated in each template.
INVITE_PERMISSIONS = "1374389534294"

# The terms and privacy policy live outside this app, on their own static site, so they stay
# up whether or not the dashboard is. Discord wants both reachable from anywhere the bot is
# offered, which is why the footer carrying them is on every page rather than only when
# somebody is signed in.
# `or` rather than a get() default: .env.example lists these empty, and an empty string would
# otherwise win over the default and produce a link that goes nowhere.
TERMS_URL = (os.environ.get("TERMS_URL") or
             "https://tetooooooooooooooooooooooooooo.github.io/soundcord-tos/").strip()
PRIVACY_URL = (os.environ.get("PRIVACY_URL") or
               "https://tetooooooooooooooooooooooooooo.github.io/soundcord-tos/privacy.html"
               ).strip()


def invite_url(guild_id=None) -> str:
    url = (f"https://discord.com/oauth2/authorize?client_id={api.CLIENT_ID}"
           f"&scope=bot+applications.commands&permissions={INVITE_PERMISSIONS}")
    return f"{url}&guild_id={guild_id}" if guild_id else url
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Heroku terminates TLS in front of the dyno, so cookies can be secure-only in production.
    SESSION_COOKIE_SECURE=os.environ.get("DASHBOARD_INSECURE_COOKIES") != "1",
    MAX_CONTENT_LENGTH=64 * 1024,
)

if not os.environ.get("DASHBOARD_SECRET_KEY"):
    print("[dashboard] DASHBOARD_SECRET_KEY is unset, so a random one is in use. "
          "Everyone gets logged out on every restart. Set it.")


# ── session helpers ──────────────────────────────────────────────────
def current_user():
    return session.get("user")


def csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


def check_csrf():
    sent = request.form.get("csrf", "")
    expected = session.get("csrf", "")
    # compare_digest so a wrong token can't be guessed a character at a time.
    if not expected or not secrets.compare_digest(sent, expected):
        abort(400, "This form expired. Go back, reload the page and try again.")


def login_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if not current_user() or not session.get("token"):
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)
    return wrapper


def require_guild(guild_id: int) -> dict:
    """Confirm, right now, that the logged in user can manage this guild and the bot is in it.

    Re-checked on every request on purpose. A session that listed a guild an hour ago says
    nothing about whether that person still administers it.
    """
    user = current_user()
    token = session.get("token")
    if not user or not token:
        abort(401)
    try:
        allowed = api.manageable_guilds(token, int(user["id"]))
    except api.DiscordError:
        session.clear()
        abort(401)

    match = next((g for g in allowed if int(g["id"]) == guild_id), None)
    if match is None:
        # Same response whether the guild doesn't exist or they simply can't touch it, so the
        # dashboard isn't a way to probe which servers exist.
        abort(404)
    if guild_id not in store.bot_guild_ids():
        abort(404)
    return match


@app.context_processor
def inject():
    return {"user": current_user(), "csrf_token": csrf_token, "api": api,
            "brand": BRAND, "invite_url": invite_url,
            "terms_url": TERMS_URL, "privacy_url": PRIVACY_URL}


# ── auth ─────────────────────────────────────────────────────────────
@app.route("/")
def index():
    problems = api.configured()
    if problems:
        return render_template("misconfigured.html", problems=problems), 503
    if current_user():
        return redirect(url_for("servers"))
    return render_template("landing.html")


@app.route("/docs")
def documentation():
    """Public on purpose: somebody deciding whether to add the bot should be able to read what
    it does without handing over an account first."""
    return render_template("docs.html", setup=docs.SETUP, sections=docs.SECTIONS,
                           troubleshooting=docs.TROUBLESHOOTING)


# ── status ───────────────────────────────────────────────────────────
# Public, and deliberately the one page that needs no login and no database write. Somebody
# whose bot has gone quiet wants an answer, not a sign-in form.
STATUS_WORDS = {
    "up": ("All good", "Newt is online and answering."),
    "wobbly": ("Having a moment", "Newt hasn't checked in for a few minutes. This is usually a "
                                  "restart or a deploy and clears itself."),
    "down": ("Offline", "Newt hasn't checked in for a while, so commands won't be working."),
    "unknown": ("Not sure", "There's nothing recent to go on."),
}


def _ago(seconds) -> str:
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} second{'' if seconds == 1 else 's'}"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'' if hours == 1 else 's'}"
    days = hours // 24
    return f"{days} day{'' if days == 1 else 's'}"


def status_payload() -> dict:
    status = store.bot_status()
    heading, detail = STATUS_WORDS[status["state"]]
    return {
        **status,
        "heading": heading,
        "detail": status.get("reason") or detail,
        "quiet_for": _ago(status.get("seconds_quiet")),
        "uptime": _ago(status.get("uptime_seconds")),
        # The page you are reading is served by the web process, so its being up is a given.
        "dashboard": "up",
    }


@app.route("/status")
def status():
    return render_template("status.html", status=status_payload(),
                           brand=BRAND)


@app.route("/status.json")
def status_json():
    """Polled by the page so it updates without a reload, and usable by anything else."""
    data = status_payload()
    for key in ("last_seen", "started_at"):
        if data.get(key) is not None:
            data[key] = data[key].isoformat()
    return data


@app.route("/login")
def login():
    if api.configured():
        return redirect(url_for("index"))
    state = secrets.token_urlsafe(24)
    session["state"] = state
    nxt = request.args.get("next", "")
    # Only ever store a path of our own, never an absolute url somebody supplied.
    session["next"] = nxt if nxt.startswith("/") and not nxt.startswith("//") else ""
    return redirect(api.authorize_url(state))


@app.route("/callback")
def callback():
    expected = session.pop("state", None)
    got = request.args.get("state")
    if not expected or not got or not secrets.compare_digest(expected, got):
        return render_template("error.html",
                               message="That login didn't come from here. Try again."), 400

    code = request.args.get("code")
    if not code:
        return redirect(url_for("index"))

    try:
        payload = api.exchange_code(code)
        token = payload["access_token"]
        user = api.get_user(token)
    except (api.DiscordError, KeyError):
        return render_template("error.html",
                               message="Discord wouldn't complete that login. Try again."), 502

    session["token"] = token
    session["user"] = {"id": user["id"], "username": user.get("global_name")
                       or user.get("username"), "avatar": api.avatar_url(user)}
    api.forget_user(int(user["id"]))          # start from a fresh guild list on each login
    destination = session.pop("next", "") or url_for("servers")
    return redirect(destination)


@app.route("/logout", methods=["POST"])
def logout():
    check_csrf()
    user = current_user()
    if user:
        api.forget_user(int(user["id"]))
    session.clear()
    return redirect(url_for("index"))


# ── servers ──────────────────────────────────────────────────────────
# Which tab each form's section lives in. Related settings share a tab, so a save has to know
# where to send you back to rather than assuming its own name is a pane.
SECTION_TABS = {
    "welcome": "greetings", "goodbye": "greetings",
    "autorole": "roles",
    "modlog": "logging", "medialog": "logging", "pinglog": "logging",
}


@app.route("/servers")
@login_required
def servers():
    user = current_user()
    try:
        allowed = api.manageable_guilds(session["token"], int(user["id"]))
    except api.DiscordError:
        session.clear()
        return redirect(url_for("index"))

    present = store.bot_guild_ids()
    joined, absent = [], []
    for g in sorted(allowed, key=lambda x: x["name"].lower()):
        (joined if int(g["id"]) in present else absent).append(g)
    return render_template("servers.html", joined=joined, absent=absent)


@app.route("/servers/<int:guild_id>")
@login_required
def guild_settings(guild_id: int):
    guild = require_guild(guild_id)
    roles = api.guild_roles(guild_id)
    channels = api.guild_channels(guild_id)

    panels = store.panels(guild_id)
    for panel in panels:
        # Keyed by role id so the template can fill each row's label and emoji boxes without
        # searching the list again for every role in the server.
        panel["by_role"] = {int(e["role_id"]): e for e in (panel.get("roles") or [])}

    return render_template(
        "settings.html",
        guild=guild,
        settings=store.settings(guild_id),
        channels=channels,
        roles=roles,
        panels=panels,
        role_names={r["id"]: r for r in roles},
        channel_names={c["id"]: c for c in channels},
        max_autoroles=store.MAX_AUTOROLES,
        max_panel_roles=store.MAX_PANEL_ROLES,
        log_events=store.LOG_EVENTS,
        automod_rules=store.AUTOMOD_RULES,
        automod_actions=store.AUTOMOD_ACTIONS,
        automod_defaults=store.AUTOMOD_DEFAULTS,
    )


@app.route("/servers/<int:guild_id>", methods=["POST"])
@login_required
def save_guild_settings(guild_id: int):
    check_csrf()
    require_guild(guild_id)

    valid_channels = {int(c["id"]) for c in api.guild_channels(guild_id)}
    valid_roles = assignable_role_ids(guild_id)
    section = request.form.get("section", "")

    # Automod, like logging, writes one nested document rather than a set of flat fields, so
    # it has its own builder instead of going through the generic allow-list.
    if section == "automod":
        # Exemptions can name any role, not only ones the bot could hand out, since being
        # exempt from a filter has nothing to do with whether the bot can assign it.
        every_role = {int(r["id"]) for r in api.guild_roles(guild_id)}
        existing = (store.settings(guild_id) or {}).get("automod") or {}
        store.save(guild_id, {"automod": store.clean_automod(
            request.form, valid_channels, every_role, existing)})
        flash("Saved. The bot picks this up within a few seconds.")
        return redirect(url_for("guild_settings", guild_id=guild_id) + "#automod")

    # Logging is handled apart from the table below: it writes one nested map of twelve events
    # rather than a set of flat fields, so it can't go through the generic allow-list.
    if section == "logging":
        events = store.clean_log_events(request.form, valid_channels)
        values = {
            "logging_enabled": "logging_enabled" in request.form,
            "log_channel": store.clean("log_channel", request.form.get("log_channel"),
                                       valid_channels),
            "log_events": events,
        }
        # Every event needs somewhere to go: its own channel, or the shared one. With neither,
        # nothing would be recorded and the page would still claim logging was on.
        if not values["log_channel"] and not any(e["channel"] for e in events.values() if e["on"]):
            values["logging_enabled"] = False
        store.save(guild_id, values)
        flash("Saved. The bot picks this up within a few seconds.")
        return redirect(url_for("guild_settings", guild_id=guild_id) + "#logging")

    # Only the fields belonging to the submitted section are touched, so saving one card
    # can't blank the others.
    sections = {
        "medialog": ["medialog_enabled", "medialog_channel"],
        "modlog": ["modlog_channel"],
        "pinglog": ["pinglog_enabled", "pinglog_channel"],
        "welcome": ["welcome_enabled", "welcome_channel", "welcome_message", "welcome_embed"],
        "goodbye": ["goodbye_enabled", "goodbye_channel", "goodbye_message", "goodbye_embed"],
        "autorole": ["autorole_enabled", "autorole_ids"],
    }
    fields = sections.get(section)
    if not fields:
        abort(400, "Unknown section.")

    values = {}
    for field in fields:
        kind = store.ALLOWED_FIELDS[field]
        if kind is bool:
            raw = field in request.form          # a checkbox is absent when unticked
        elif kind == "role_ids":
            raw = request.form.getlist(field)    # a set of ticked boxes, not one value
        else:
            raw = request.form.get(field)
        values[field] = store.clean(field, raw, valid_channels, valid_roles)

    # A feature that needs somewhere to post can't be on without one.
    if section in ("medialog", "pinglog") and not values.get(f"{section}_channel"):
        values[f"{section}_enabled"] = False
    if section == "goodbye" and not values.get("goodbye_channel"):
        values["goodbye_enabled"] = False
    if section in ("welcome", "goodbye") and not values.get(f"{section}_message"):
        values[f"{section}_enabled"] = False
    # Nothing to hand out means nothing to switch on.
    if section == "autorole" and not values.get("autorole_ids"):
        values["autorole_enabled"] = False

    store.save(guild_id, values)
    flash("Saved. The bot picks this up within a few seconds.")
    # Several sections share a tab, so they have no pane of their own to come back to.
    # Without this a save would land on whatever tab happens to be first.
    anchor = SECTION_TABS.get(section, section)
    return redirect(url_for("guild_settings", guild_id=guild_id) + f"#{anchor}")


# ── role panels ──────────────────────────────────────────────────────
def assignable_role_ids(guild_id: int) -> set:
    """Only roles the bot could really give out. Enforced on save so a panel can't be built
    from roles that would fail silently every time somebody clicked them."""
    return {int(r["id"]) for r in api.guild_roles(guild_id) if not r["problem"]}


def _panel_form(guild_id: int):
    """The parts of a panel form that create and edit have in common."""
    valid_channels = {int(c["id"]) for c in api.guild_channels(guild_id)}
    channel_id = request.form.get("channel_id")
    try:
        channel_id = int(channel_id)
    except (TypeError, ValueError):
        abort(400, "Pick a channel for this panel.")
    if channel_id not in valid_channels:
        abort(400, "That channel isn't in this server.")

    mode = request.form.get("mode", "toggle")
    title = request.form.get("title", "")
    description = request.form.get("description", "")
    return channel_id, title, description, mode


@app.route("/servers/<int:guild_id>/panels", methods=["POST"])
@login_required
def create_panel(guild_id: int):
    check_csrf()
    require_guild(guild_id)
    channel_id, title, description, mode = _panel_form(guild_id)

    if not store.create_panel(guild_id, channel_id, title, description, mode):
        flash(f"That's the limit of {store.MAX_PANELS} panels. Delete one first.")
    else:
        flash("Panel created. Add some roles to it and it gets posted.")
    return redirect(url_for("guild_settings", guild_id=guild_id) + "#roles")


@app.route("/servers/<int:guild_id>/panels/<panel_id>", methods=["POST"])
@login_required
def save_panel(guild_id: int, panel_id: str):
    check_csrf()
    require_guild(guild_id)

    if request.form.get("delete"):
        if store.delete_panel(guild_id, panel_id):
            flash("Panel deleted. The message disappears within a few seconds.")
        else:
            flash("That panel is already gone.")
        return redirect(url_for("guild_settings", guild_id=guild_id) + "#roles")

    channel_id, title, description, mode = _panel_form(guild_id)
    # Names as well as ids: a button left without a label should read as the role it grants,
    # not as a placeholder.
    usable = {int(r["id"]): r["name"] for r in api.guild_roles(guild_id) if not r["problem"]}

    # Each button is one ticked role plus its optional label and emoji, keyed by role id so a
    # missing tick drops the whole button rather than leaving an orphaned label behind.
    roles, seen = [], set()
    for raw in request.form.getlist("role_ids"):
        try:
            role_id = int(raw)
        except (TypeError, ValueError):
            continue
        if role_id not in usable or role_id in seen:
            continue
        seen.add(role_id)
        label = (request.form.get(f"label_{role_id}", "") or "").strip()
        roles.append({
            "role_id": role_id,
            "label": (label or usable[role_id])[:store.MAX_LABEL],
            "emoji": (request.form.get(f"emoji_{role_id}", "") or "").strip()[:64] or None,
        })

    if not store.save_panel(guild_id, panel_id, channel_id, title, description, mode, roles):
        abort(404)
    flash("Saved. The panel updates itself within a few seconds."
          if roles else "Saved. Tick at least one role and it gets posted.")
    return redirect(url_for("guild_settings", guild_id=guild_id) + "#roles")


@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(404)
def handle(err):
    messages = {
        400: getattr(err, "description", "That request didn't look right."),
        401: "You need to log in again.",
        404: "That server isn't available to you.",
    }
    return render_template("error.html", message=messages.get(err.code, "Something went wrong.")), err.code


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
