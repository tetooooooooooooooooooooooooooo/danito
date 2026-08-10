"""Self-serve roles: a message with a button per role, click to give yourself one.

Buttons rather than reactions, which is the older way of doing this. Reactions need Add
Reactions and Manage Messages, break whenever a custom emoji is deleted or comes from a server
the bot was removed from, and give no way to tell somebody why their click did nothing. A
button carries its own id, works with no extra permissions, and can answer privately.

Clicks are handled by on_interaction rather than by a live View object, because a View dies
with the process and these messages have to keep working across restarts and redeploys. The
role is recoverable because the id is ours: `rr:<panel>:<role>`.

Panels are published by the loop below rather than written to Discord directly. The dashboard
runs in a separate process with no gateway connection, so it records what the panel should look
like and flags it; the bot is what actually posts. That way the same code publishes a panel
whether it came from a slash command or from the web.
"""

import asyncio
import datetime
import re
from typing import Optional

import discord
from bson import ObjectId
from bson.errors import InvalidId
from discord import app_commands
from discord.ext import commands, tasks

import Database
import RoleTools
from Brand import MINT

MAX_PANELS = 10
PUBLISH_EVERY = 12          # seconds between checks for panels the dashboard has changed

COLOR_PANEL = MINT
COLOR_WARN = 0xE67E22

# Ours, and cheap to recognise. Every component click in every server reaches on_interaction,
# so this string test is what keeps role panels off the database for clicks that aren't theirs.
BUTTON_ID = re.compile(r"^rr:([0-9a-f]{24}):(\d{1,20})$")

MODES = {
    "toggle": "Click to get a role, click again to give it back.",
    "single": "Pick one. Choosing another swaps it for the one you had.",
}


class RoleButtons(commands.Cog, name="RoleButtons"):
    """Let people pick their own roles from a message."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    rolepanel = app_commands.Group(
        name="rolepanel", description="Messages people click to give themselves roles",
        guild_only=True, default_permissions=discord.Permissions(manage_roles=True))

    async def _run(self, fn, *args, **kwargs):
        # pymongo is synchronous, so keep it off the event loop.
        return await asyncio.to_thread(lambda: fn(*args, **kwargs))

    @property
    def panels(self):
        return Database.get_bot_database(self.bot.MongoClient)["role_panels"]

    async def cog_load(self):
        try:
            await self._run(self._ensure_indexes)
        except Exception as e:
            print(f"[RoleButtons] index setup failed: {e}")
        self.publish_pending.start()

    async def cog_unload(self):
        self.publish_pending.cancel()

    def _ensure_indexes(self):
        self.panels.create_index([("guild_id", 1)], name="guild")
        # The publish loop asks only this question, and asks it every few seconds.
        self.panels.create_index([("needs_publish", 1)], name="pending")

    # ── what a panel looks like ──────────────────────────────────────
    @staticmethod
    def _embed(panel: dict) -> discord.Embed:
        embed = discord.Embed(
            title=(panel.get("title") or "Pick your roles")[:256],
            description=(panel.get("description") or None),
            color=panel.get("color") or COLOR_PANEL,
        )
        embed.set_footer(text=MODES.get(panel.get("mode"), MODES["toggle"]))
        return embed

    @staticmethod
    def _view(panel: dict) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        panel_id = str(panel["_id"])
        for entry in (panel.get("roles") or [])[:RoleTools.MAX_BUTTONS]:
            label = (entry.get("label") or "").strip()[:RoleTools.MAX_LABEL]
            emoji = RoleTools.parse_emoji(entry.get("emoji"))
            # Discord rejects a button with neither a label nor an emoji.
            if not label and emoji is None:
                label = "Role"
            view.add_item(discord.ui.Button(
                label=label or None,
                emoji=emoji,
                style=discord.ButtonStyle.secondary,
                custom_id=f"rr:{panel_id}:{entry['role_id']}",
            ))
        return view

    # ── publishing ───────────────────────────────────────────────────
    @tasks.loop(seconds=PUBLISH_EVERY)
    async def publish_pending(self):
        """Carry out what the dashboard asked for.

        The dashboard runs in its own process with no gateway connection, so it can only write
        down what it wants. Posting, editing and deleting the actual message happens here.
        """
        try:
            due = await self._run(lambda: list(self.panels.find(
                {"$or": [{"needs_publish": True}, {"pending_delete": True}]}).limit(20)))
        except Exception as e:
            print(f"[RoleButtons] couldn't look for pending panels: {e}")
            return

        for panel in due:
            try:
                if panel.get("pending_delete"):
                    await self._destroy(panel)
                else:
                    await self._publish(panel)
            except Exception as e:
                print(f"[RoleButtons] publish crashed for {panel.get('_id')}: {e}")
                await self._mark(panel["_id"], error=f"Something went wrong: {e}")

    async def _destroy(self, panel: dict):
        """Take the message down, then drop the record.

        In this order on purpose. If deleting the message fails the record survives, so the
        panel still appears in /rolepanel list and can be dealt with, rather than leaving a
        message with live buttons and nothing behind them.
        """
        guild = self.bot.get_guild(panel.get("guild_id", 0))
        channel = guild.get_channel(panel.get("channel_id", 0)) if guild else None
        if channel and panel.get("message_id"):
            try:
                message = await channel.fetch_message(int(panel["message_id"]))
                await message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                # Already gone, or we can't reach it. Either way the record shouldn't linger.
                pass
        await self._run(self.panels.delete_one, {"_id": panel["_id"]})

    @publish_pending.before_loop
    async def before_publish(self):
        await self.bot.wait_until_ready()

    async def _mark(self, panel_id, error: Optional[str] = None, message_id: Optional[int] = None):
        """Record the outcome and clear the flag.

        Cleared even on failure, on purpose. Leaving it set would retry a panel pointed at a
        deleted channel every few seconds forever; the error is stored instead so the person
        who set it up can see what went wrong and try again.
        """
        values = {"needs_publish": False, "publish_error": error,
                  "published_at": datetime.datetime.now(datetime.timezone.utc)}
        if message_id is not None:
            values["message_id"] = message_id
        await self._run(self.panels.update_one, {"_id": panel_id}, {"$set": values})

    async def _publish(self, panel: dict):
        guild = self.bot.get_guild(panel.get("guild_id", 0))
        if guild is None:
            await self._mark(panel["_id"], error="I'm not in that server any more.")
            return

        channel = guild.get_channel(panel.get("channel_id", 0))
        if channel is None:
            await self._mark(panel["_id"], error="That channel is gone. Pick another one.")
            return

        perms = channel.permissions_for(guild.me)
        missing = [n for n, ok in (("View Channel", perms.view_channel),
                                   ("Send Messages", perms.send_messages),
                                   ("Embed Links", perms.embed_links)) if not ok]
        if missing:
            await self._mark(panel["_id"],
                             error=f"I'm missing {', '.join(missing)} in #{channel.name}.")
            return

        if not (panel.get("roles") or []):
            await self._mark(panel["_id"],
                             error="There are no roles on this panel yet, so there was nothing "
                                   "to post.")
            return

        embed, view = self._embed(panel), self._view(panel)

        message_id = panel.get("message_id")
        if message_id:
            try:
                message = await channel.fetch_message(int(message_id))
                await message.edit(embed=embed, view=view)
                await self._mark(panel["_id"], error=None, message_id=message.id)
                return
            except discord.NotFound:
                pass          # somebody deleted it, so post a fresh one below
            except discord.Forbidden:
                await self._mark(panel["_id"],
                                 error=f"I can't edit my message in #{channel.name}.")
                return
            except discord.HTTPException as e:
                await self._mark(panel["_id"], error=f"Discord refused the edit: {e}")
                return

        try:
            sent = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await self._mark(panel["_id"], error=f"I can't post in #{channel.name}.")
            return
        except discord.HTTPException as e:
            await self._mark(panel["_id"], error=f"Discord refused the message: {e}")
            return
        await self._mark(panel["_id"], error=None, message_id=sent.id)

    async def _queue(self, panel_id):
        await self._run(self.panels.update_one, {"_id": panel_id},
                        {"$set": {"needs_publish": True}})

    # ── someone clicks ───────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")
        match = BUTTON_ID.match(custom_id)
        if not match or interaction.guild is None:
            return

        panel_id, role_id = match.group(1), int(match.group(2))
        await interaction.response.defer(ephemeral=True)

        try:
            panel = await self._run(self.panels.find_one, {"_id": ObjectId(panel_id)})
        except Exception as e:
            print(f"[RoleButtons] panel lookup failed: {e}")
            await interaction.followup.send(
                "I couldn't reach my settings just now. Try again in a moment.", ephemeral=True)
            return

        if panel is None or panel.get("guild_id") != interaction.guild.id:
            await interaction.followup.send(
                "This panel has been deleted, so the buttons don't do anything any more.",
                ephemeral=True)
            return

        # Trusting the id in the button alone would let anybody who can read a custom_id hand
        # themselves any role in the server by editing it into a crafted interaction.
        panel_roles = [int(e["role_id"]) for e in (panel.get("roles") or [])]
        if role_id not in panel_roles:
            await interaction.followup.send(
                "That button is out of date. Somebody with Manage Roles can refresh the panel.",
                ephemeral=True)
            return

        role = interaction.guild.get_role(role_id)
        if role is None:
            await interaction.followup.send(
                "That role has been deleted. Somebody with Manage Roles needs to update this "
                "panel.", ephemeral=True)
            return

        problem = RoleTools.why_not(interaction.guild, role)
        if problem:
            await interaction.followup.send(
                f"I can't give you **{role.name}** because {problem}", ephemeral=True)
            return

        member = interaction.user
        single = panel.get("mode") == "single"

        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Role panel")
                await interaction.followup.send(
                    f"Taken **{role.name}** back off you.", ephemeral=True)
                return

            if single:
                # Everything else on this panel comes off, so the choice stays a choice.
                others = [r for r in member.roles
                          if r.id in panel_roles and r.id != role.id
                          and RoleTools.assignable(interaction.guild, r)]
                if others:
                    await member.remove_roles(*others, reason="Role panel, single choice")

            await member.add_roles(role, reason="Role panel")
        except discord.Forbidden:
            await interaction.followup.send(
                "Discord wouldn't let me change your roles. My own role probably sits below "
                "that one.", ephemeral=True)
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"That didn't work: {e}", ephemeral=True)
            return

        await interaction.followup.send(f"You now have **{role.name}**.", ephemeral=True)

    # ── commands ─────────────────────────────────────────────────────
    async def _panel_options(self, interaction: discord.Interaction, current: str):
        try:
            found = await self._run(lambda: list(
                self.panels.find({"guild_id": interaction.guild.id}).limit(25)))
        except Exception:
            return []
        out = []
        for panel in found:
            name = (panel.get("title") or "Untitled panel")[:100]
            if current.lower() in name.lower():
                out.append(app_commands.Choice(name=name, value=str(panel["_id"])))
        return out[:25]

    async def _get_panel(self, interaction: discord.Interaction, raw: str) -> Optional[dict]:
        """Load a panel the user named, or answer them and return None."""
        try:
            panel = await self._run(self.panels.find_one, {"_id": ObjectId(raw)})
        except (InvalidId, TypeError):
            panel = None
        except Exception as e:
            print(f"[RoleButtons] panel lookup failed: {e}")
            panel = None

        if panel is None or panel.get("guild_id") != interaction.guild.id:
            await interaction.response.send_message(
                "I couldn't find that panel. Pick one from the list the command offers, or see "
                "them all with `/rolepanel list`.", ephemeral=True)
            return None
        return panel

    @rolepanel.command(name="create", description="Post a new message people click for roles")
    @app_commands.describe(
        channel="Where to post it.",
        title="The heading on the message.",
        description="Optional text under the heading.",
        mode="Whether people can hold several of these roles at once.")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Any number of roles", value="toggle"),
        app_commands.Choice(name="One role only", value="single"),
    ])
    @app_commands.checks.has_permissions(manage_roles=True)
    async def create(self, interaction: discord.Interaction, channel: discord.TextChannel,
                     title: app_commands.Range[str, 1, 200],
                     description: Optional[app_commands.Range[str, 1, 1500]] = None,
                     mode: Optional[app_commands.Choice[str]] = None):
        count = await self._run(self.panels.count_documents,
                                {"guild_id": interaction.guild.id})
        if count >= MAX_PANELS:
            await interaction.response.send_message(
                f"That's the limit of {MAX_PANELS} panels. Delete one with "
                f"`/rolepanel delete` first.", ephemeral=True)
            return

        result = await self._run(self.panels.insert_one, {
            "guild_id": interaction.guild.id,
            "channel_id": channel.id,
            "message_id": None,
            "title": title,
            "description": description,
            "color": COLOR_PANEL,
            "mode": mode.value if mode else "toggle",
            "roles": [],
            "needs_publish": False,          # nothing to post until it has a role
            "publish_error": None,
            "created_at": datetime.datetime.now(datetime.timezone.utc),
        })
        await interaction.response.send_message(
            f"Panel created for {channel.mention}. Add roles to it with `/rolepanel addrole` "
            f"and it gets posted as soon as the first one is on there.\n"
            f"Panel id `{result.inserted_id}`.", ephemeral=True)

    @rolepanel.command(name="addrole", description="Put a role button on a panel")
    @app_commands.describe(panel="Which panel.", role="The role the button gives out.",
                           label="What the button says. Defaults to the role name.",
                           emoji="An emoji for the button.")
    @app_commands.autocomplete(panel=_panel_options)
    @app_commands.checks.has_permissions(manage_roles=True)
    async def addrole(self, interaction: discord.Interaction, panel: str, role: discord.Role,
                      label: Optional[app_commands.Range[str, 1, RoleTools.MAX_LABEL]] = None,
                      emoji: Optional[str] = None):
        found = await self._get_panel(interaction, panel)
        if found is None:
            return

        problem = RoleTools.why_not(interaction.guild, role)
        if problem:
            await interaction.response.send_message(
                f"I can't give out **{role.name}** because {problem}", ephemeral=True)
            return

        roles = found.get("roles") or []
        if any(int(e["role_id"]) == role.id for e in roles):
            await interaction.response.send_message(
                f"**{role.name}** is already on that panel.", ephemeral=True)
            return
        if len(roles) >= RoleTools.MAX_BUTTONS:
            await interaction.response.send_message(
                f"A message can only hold {RoleTools.MAX_BUTTONS} buttons. Make a second panel "
                f"for the rest.", ephemeral=True)
            return

        roles.append({"role_id": role.id,
                      "label": RoleTools.label_for(role, label),
                      "emoji": (emoji or "").strip() or None})
        await self._run(self.panels.update_one, {"_id": found["_id"]},
                        {"$set": {"roles": roles, "needs_publish": True}})
        await interaction.response.send_message(
            f"**{role.name}** added. The panel updates itself within a few seconds.",
            ephemeral=True)

    @rolepanel.command(name="removerole", description="Take a role button off a panel")
    @app_commands.describe(panel="Which panel.", role="The role to take off it.")
    @app_commands.autocomplete(panel=_panel_options)
    @app_commands.checks.has_permissions(manage_roles=True)
    async def removerole(self, interaction: discord.Interaction, panel: str, role: discord.Role):
        found = await self._get_panel(interaction, panel)
        if found is None:
            return

        roles = found.get("roles") or []
        kept = [e for e in roles if int(e["role_id"]) != role.id]
        if len(kept) == len(roles):
            await interaction.response.send_message(
                f"**{role.name}** wasn't on that panel.", ephemeral=True)
            return

        await self._run(self.panels.update_one, {"_id": found["_id"]},
                        {"$set": {"roles": kept, "needs_publish": bool(kept)}})
        extra = ("" if kept else
                 " That was the last button, so the message stays as it is until you add "
                 "another role.")
        await interaction.response.send_message(
            f"**{role.name}** taken off the panel.{extra}", ephemeral=True)

    @rolepanel.command(name="list", description="See the role panels in this server")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def show(self, interaction: discord.Interaction):
        found = await self._run(lambda: list(
            self.panels.find({"guild_id": interaction.guild.id}).limit(MAX_PANELS)))

        embed = discord.Embed(title="Role panels", color=MINT)
        if not found:
            embed.description = ("There aren't any yet. Make one with `/rolepanel create`, or "
                                 "build it on the dashboard.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        for panel in found:
            channel = interaction.guild.get_channel(panel.get("channel_id", 0))
            names = []
            for entry in (panel.get("roles") or []):
                role = interaction.guild.get_role(int(entry["role_id"]))
                names.append(role.mention if role else "*(deleted role)*")

            lines = [f"In {channel.mention if channel else '*(channel gone)*'}, "
                     f"{MODES.get(panel.get('mode'), MODES['toggle']).lower()}"]
            lines.append("Roles: " + (", ".join(names) if names else "*none yet*"))
            if panel.get("publish_error"):
                lines.append(f"⚠️ {panel['publish_error']}")
                embed.color = COLOR_WARN
            elif panel.get("needs_publish"):
                lines.append("Updating in a moment.")
            lines.append(f"`{panel['_id']}`")

            embed.add_field(name=(panel.get("title") or "Untitled panel")[:256],
                            value="\n".join(lines)[:1024], inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @rolepanel.command(name="delete", description="Remove a role panel and its message")
    @app_commands.describe(panel="Which panel to delete.")
    @app_commands.autocomplete(panel=_panel_options)
    @app_commands.checks.has_permissions(manage_roles=True)
    async def delete(self, interaction: discord.Interaction, panel: str):
        found = await self._get_panel(interaction, panel)
        if found is None:
            return

        # The message goes too. Leaving it behind would give people buttons that answer
        # "this panel has been deleted" forever.
        channel = interaction.guild.get_channel(found.get("channel_id", 0))
        if channel and found.get("message_id"):
            try:
                message = await channel.fetch_message(int(found["message_id"]))
                await message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        await self._run(self.panels.delete_one, {"_id": found["_id"]})
        await interaction.response.send_message(
            f"**{found.get('title') or 'That panel'}** is gone.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(RoleButtons(bot))
    print("RoleButtons cog loaded ✓")
