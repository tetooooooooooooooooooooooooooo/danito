import discord
from discord import app_commands
from discord.ext import commands

badge_attrs = {
    "staff": "Discord Staff",
    "partner": "Discord Partner",
    "hypesquad": "HypeSquad Events",
    "bug_hunter": "Bug Hunter L1",
    "bug_hunter_level_2": "Bug Hunter L2",
    "hypesquad_bravery": "House Bravery",
    "hypesquad_brilliance": "House Brilliance",
    "hypesquad_balance": "House Balance",
    "early_supporter": "Early Supporter",
    "team_user": "Team User",
    "verified_bot": "Verified Bot",
    "verified_bot_developer": "Early Verified Bot Developer",
    "discord_certified_moderator": "Moderator Programs Alumni",
    "bot_http_interactions": "HTTP Interactions Bot",
    "active_developer": "Active Developer",
}

class Badges(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def has_badge(self, member: discord.Member, badge: str):
        if not member.public_flags:
            return False
        return getattr(member.public_flags, badge, False)

    async def badge_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        # Add "all" option
        choices = [app_commands.Choice(name="All Badges", value="all")]
        
        # Add all badge options
        for key, name in badge_attrs.items():
            choices.append(app_commands.Choice(name=name, value=key))
        
        # Filter based on what user is typing
        if current:
            filtered = [
                choice for choice in choices
                if current.lower() in choice.name.lower()
            ]
            return filtered[:25]  # Discord limits to 25 choices
        
        return choices[:25]

    # /cbc badge
    @app_commands.command(name="cbc", description="Count members with a badge")
    @app_commands.describe(badge="Badge name or 'all'")
    @app_commands.autocomplete(badge=badge_autocomplete)
    async def cbc(self, interaction: discord.Interaction, badge: str):
        await interaction.response.defer()
        
        guild = interaction.guild
        if not guild:
            return

        members = guild.members

        if badge == "all":
            result = {}
            for b in badge_attrs:
                count = sum(self.has_badge(m, b) for m in members)
                if count > 0:
                    result[b] = count
            
            text = "\n".join(
                f"{badge_attrs[b]}: {c}"
                for b, c in result.items()
            ) or "No detectable badges."
            
            await interaction.followup.send(f"**Badge counts:**\n{text}")
            return

        if badge not in badge_attrs:
            await interaction.followup.send("Unknown badge.")
            return

        count = sum(self.has_badge(m, badge) for m in members)
        await interaction.followup.send(
            f"**{badge_attrs[badge]}:** {count} members"
        )

    # /cbu badge
    @app_commands.command(name="cbu", description="List users with a badge")
    @app_commands.describe(badge="Badge name")
    @app_commands.autocomplete(badge=badge_autocomplete)
    async def cbu(self, interaction: discord.Interaction, badge: str):
        await interaction.response.defer()
        
        if badge not in badge_attrs:
            await interaction.followup.send("Unknown badge.")
            return

        guild = interaction.guild
        members = [
            m for m in guild.members
            if self.has_badge(m, badge)
        ]

        if not members:
            await interaction.followup.send("No users found.")
            return

        names = "\n".join(m.mention for m in members[:50])
        await interaction.followup.send(
            f"**Users with {badge_attrs[badge]}:**\n{names}"
        )

async def setup(bot):
    await bot.add_cog(Badges(bot))
