"""Shared rules for handing a role to somebody.

Autorole and the role buttons need the same answer to one question: can the bot actually give
this role out. Getting it wrong produces the most common complaint a role bot gets, which is
silence. Discord replies 403 with nothing useful in it, so the reason has to be worked out here
and said in words the person setting it up can act on.
"""

from typing import Optional

import discord

# Discord allows five action rows of five buttons on one message.
MAX_BUTTONS = 25
# A button label is capped at 80 characters, and a role name can be longer.
MAX_LABEL = 80


def why_not(guild: discord.Guild, role: discord.Role) -> Optional[str]:
    """Why this role can't be handed out, phrased to finish "I can't give that out because...".

    Returns None when it can. Every branch here is a rule Discord enforces silently.
    """
    me = guild.me
    if me is None:
        return "I can't see myself in this server."
    if not me.guild_permissions.manage_roles:
        return "I don't have the Manage Roles permission."
    if role.is_default():
        return "that's @everyone, which everybody has already."
    if role.managed:
        return ("it's managed by an integration, so it belongs to a bot, a booster perk or a "
                "subscription. Discord doesn't let anybody assign those by hand.")
    if role >= me.top_role:
        return (f"it sits above my own highest role ({me.top_role.name}). Drag me above it in "
                f"Server Settings, then Roles.")
    return None


def assignable(guild: discord.Guild, role: discord.Role) -> bool:
    return why_not(guild, role) is None


def label_for(role: discord.Role, custom: Optional[str] = None) -> str:
    """A button label that Discord will accept."""
    return (custom or role.name)[:MAX_LABEL]


def parse_emoji(raw: Optional[str]):
    """Turn whatever somebody typed into an emoji, or None.

    Never raises. A button with a wrong emoji is refused by Discord outright, which would take
    the whole panel down; dropping just the emoji keeps the role working.
    """
    if not raw:
        return None
    try:
        return discord.PartialEmoji.from_str(raw.strip())
    except Exception:
        return None
