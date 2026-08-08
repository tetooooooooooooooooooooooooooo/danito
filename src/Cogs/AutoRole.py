"""Roles handed out the moment somebody joins.

The whole feature is four lines of logic and one piece of Discord trivia that decides whether
it works at all: membership screening. When a server makes people accept rules before they can
talk, Discord still fires the join event straight away, but the member arrives *pending*. Roles
given to a pending member are discarded when they finish accepting, so an autorole that only
listens to the join event appears to work in testing and does nothing on any server with a
rules screen. The second listener below is what covers that.
"""

import discord
from discord import app_commands
from discord.ext import commands

import GuildConfig
import RoleTools

MAX_ROLES = 10           # more than anyone needs, and keeps the join handler to one API call

COLOR_INFO = 0x5865F2
COLOR_GOOD = 0x2ECC71
COLOR_WARN = 0xE67E22


class AutoRole(commands.Cog, name="AutoRole"):
    """Give people roles automatically when they arrive."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    autorole = app_commands.Group(
        name="autorole", description="Roles given out automatically when somebody joins",
        guild_only=True, default_permissions=discord.Permissions(manage_roles=True))

    # ── handing them out ─────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        if member.pending:
            # They still have to accept the rules. on_member_update picks them up afterwards.
            return
        await self._apply(member)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        # This event is noisy (every nickname, role and status change), so leave immediately
        # unless it is the one transition that matters: screening finished.
        if not (before.pending and not after.pending):
            return
        if after.bot:
            return
        await self._apply(after)

    async def _apply(self, member: discord.Member):
        cfg = await GuildConfig.get(self.bot, member.guild.id)
        if not cfg.get("autorole_enabled"):
            return
        wanted = cfg.get("autorole_ids") or []
        if not wanted:
            return

        guild = member.guild
        give, dead = [], []
        for role_id in wanted[:MAX_ROLES]:
            role = guild.get_role(int(role_id))
            if role is None:
                dead.append(int(role_id))
                continue
            if RoleTools.assignable(guild, role) and role not in member.roles:
                give.append(role)

        # A deleted role would otherwise sit in the settings forever, failing quietly on every
        # single join. Drop it once and the problem is gone.
        if dead:
            await GuildConfig.update(self.bot, guild.id, pull={"autorole_ids": {"$in": dead}})
            print(f"[AutoRole] dropped {len(dead)} deleted role(s) in {guild.id}")

        if not give:
            return
        try:
            # One call for all of them rather than one per role, which on a raid is the
            # difference between a rate limit and none.
            await member.add_roles(*give, reason="Autorole")
        except discord.Forbidden:
            print(f"[AutoRole] refused in {guild.id}, my role may be too low")
        except discord.HTTPException as e:
            print(f"[AutoRole] add_roles failed in {guild.id}: {e}")

    # ── commands ─────────────────────────────────────────────────────
    @autorole.command(name="add", description="Give this role to everybody who joins")
    @app_commands.describe(role="The role to hand out on join.")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def add(self, interaction: discord.Interaction, role: discord.Role):
        problem = RoleTools.why_not(interaction.guild, role)
        if problem:
            await interaction.response.send_message(
                f"I can't give out **{role.name}** because {problem}", ephemeral=True)
            return

        cfg = await GuildConfig.get(self.bot, interaction.guild.id)
        current = [int(r) for r in (cfg.get("autorole_ids") or [])]
        if role.id in current:
            await interaction.response.send_message(
                f"**{role.name}** is already on the list.", ephemeral=True)
            return
        if len(current) >= MAX_ROLES:
            await interaction.response.send_message(
                f"That's the limit of {MAX_ROLES} roles. Remove one first with "
                f"`/autorole remove`.", ephemeral=True)
            return

        await GuildConfig.update(self.bot, interaction.guild.id,
                                 values={"autorole_enabled": True},
                                 add_to_set={"autorole_ids": role.id})
        await interaction.response.send_message(
            f"Everybody who joins now gets **{role.name}**. "
            f"See the full list with `/autorole list`.", ephemeral=True)

    @autorole.command(name="remove", description="Stop giving this role to new members")
    @app_commands.describe(role="The role to stop handing out.")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def remove(self, interaction: discord.Interaction, role: discord.Role):
        cfg = await GuildConfig.get(self.bot, interaction.guild.id)
        current = [int(r) for r in (cfg.get("autorole_ids") or [])]
        if role.id not in current:
            await interaction.response.send_message(
                f"**{role.name}** wasn't on the list.", ephemeral=True)
            return

        await GuildConfig.update(self.bot, interaction.guild.id,
                                 pull={"autorole_ids": role.id})
        left = len(current) - 1
        note = "" if left else " That was the last one, so nothing is handed out now."
        await interaction.response.send_message(
            f"**{role.name}** won't be given out any more.{note}", ephemeral=True)

    @autorole.command(name="list", description="See which roles new members get")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def show(self, interaction: discord.Interaction):
        cfg = await GuildConfig.get(self.bot, interaction.guild.id)
        ids = [int(r) for r in (cfg.get("autorole_ids") or [])]
        enabled = bool(cfg.get("autorole_enabled"))

        embed = discord.Embed(title="Autorole", color=COLOR_GOOD if enabled and ids else COLOR_INFO)
        if not ids:
            embed.description = ("Nothing is handed out yet. Add a role with "
                                 "`/autorole add` and everybody who joins gets it.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        lines, broken = [], False
        for role_id in ids:
            role = interaction.guild.get_role(role_id)
            if role is None:
                lines.append(f"`{role_id}` this role has been deleted")
                broken = True
                continue
            problem = RoleTools.why_not(interaction.guild, role)
            if problem:
                lines.append(f"{role.mention} not working, because {problem}")
                broken = True
            else:
                lines.append(f"{role.mention}")

        embed.description = ("**On.** New members get:" if enabled
                             else "**Off.** These are saved but nothing is being handed out:")
        embed.add_field(name="Roles", value="\n".join(lines), inline=False)
        if broken:
            embed.color = COLOR_WARN
            embed.set_footer(text="Some roles need attention. Deleted ones are removed by "
                                  "themselves the next time somebody joins.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @autorole.command(name="off", description="Stop handing out roles on join")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def off(self, interaction: discord.Interaction):
        await GuildConfig.update(self.bot, interaction.guild.id, {"autorole_enabled": False})
        await interaction.response.send_message(
            "Autorole is off. Your list is kept, so `/autorole on` switches it straight back.",
            ephemeral=True)

    @autorole.command(name="on", description="Start handing out roles on join again")
    @app_commands.checks.has_permissions(manage_roles=True)
    async def on(self, interaction: discord.Interaction):
        cfg = await GuildConfig.get(self.bot, interaction.guild.id)
        if not (cfg.get("autorole_ids") or []):
            await interaction.response.send_message(
                "There are no roles to hand out yet. Add one with `/autorole add`.",
                ephemeral=True)
            return
        await GuildConfig.update(self.bot, interaction.guild.id, {"autorole_enabled": True})
        await interaction.response.send_message(
            "Autorole is on. Check what goes out with `/autorole list`.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoRole(bot))
    print("AutoRole cog loaded ✓")
