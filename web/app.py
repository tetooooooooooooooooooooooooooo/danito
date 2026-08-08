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
import store                                                                 # noqa: E402

app = Flask(__name__)
app.secret_key = os.environ.get("DASHBOARD_SECRET_KEY") or secrets.token_hex(32)

# The bot's own name inside Discord comes from the application itself, so it follows a rename
# in the Developer Portal without a deploy. Only the web pages need telling.
BRAND = os.environ.get("DASHBOARD_BRAND", "Newt")
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
    return {"user": current_user(), "csrf_token": csrf_token, "api": api, "brand": BRAND}


# ── auth ─────────────────────────────────────────────────────────────
@app.route("/")
def index():
    problems = api.configured()
    if problems:
        return render_template("misconfigured.html", problems=problems), 503
    if current_user():
        return redirect(url_for("servers"))
    return render_template("login.html")


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
    return render_template("servers.html", joined=joined, absent=absent,
                           client_id=api.CLIENT_ID)


@app.route("/servers/<int:guild_id>")
@login_required
def guild_settings(guild_id: int):
    guild = require_guild(guild_id)
    return render_template(
        "settings.html",
        guild=guild,
        settings=store.settings(guild_id),
        channels=api.guild_channels(guild_id),
    )


@app.route("/servers/<int:guild_id>", methods=["POST"])
@login_required
def save_guild_settings(guild_id: int):
    check_csrf()
    require_guild(guild_id)

    valid_channels = {int(c["id"]) for c in api.guild_channels(guild_id)}
    section = request.form.get("section", "")

    # Only the fields belonging to the submitted section are touched, so saving one card
    # can't blank the others.
    sections = {
        "medialog": ["medialog_enabled", "medialog_channel"],
        "modlog": ["modlog_channel"],
        "pinglog": ["pinglog_enabled", "pinglog_channel"],
        "welcome": ["welcome_enabled", "welcome_channel", "welcome_message", "welcome_embed"],
        "goodbye": ["goodbye_enabled", "goodbye_channel", "goodbye_message", "goodbye_embed"],
    }
    fields = sections.get(section)
    if not fields:
        abort(400, "Unknown section.")

    values = {}
    for field in fields:
        raw = request.form.get(field)
        if store.ALLOWED_FIELDS[field] is bool:
            raw = field in request.form          # a checkbox is absent when unticked
        values[field] = store.clean(field, raw, valid_channels)

    # A feature that needs somewhere to post can't be on without one.
    if section in ("medialog", "pinglog") and not values.get(f"{section}_channel"):
        values[f"{section}_enabled"] = False
    if section == "goodbye" and not values.get("goodbye_channel"):
        values["goodbye_enabled"] = False
    if section in ("welcome", "goodbye") and not values.get(f"{section}_message"):
        values[f"{section}_enabled"] = False

    store.save(guild_id, values)
    flash("Saved. The bot picks this up within a few seconds.")
    return redirect(url_for("guild_settings", guild_id=guild_id) + f"#{section}")


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
