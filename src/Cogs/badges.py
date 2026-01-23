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

premium_types = {
    1: "Nitro Classic",
    2: "Nitro",
    3: "Nitro Basic",
}

class Badges(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def has_badge(self, member: discord.Member, badge: str):
        if not member.public_flags:
            return False
        return getattr(member.public_flags, badge, False)

    def has_premium(self, member: discord.Member, premium_level: int):
        """Check if member has specific premium type"""
        return member.premium_type and member.premium_type.value == premium_level

    # /cbc badge
    @app_commands.command(name="cbc", description="Count members with a badge or premium")
    @app_commands.describe(badge="Badge name, premium type (nitro/nitro_classic/nitro_basic), or 'all'")
    async def cbc(self, interaction: discord.Interaction, badge: str):
        await interaction.response.defer()
        
        guild = interaction.guild
        if not guild:
            return

        members = guild.members

        if badge == "all":
            result = {}
            
            # Count badges
            for b in badge_attrs:
                count = sum(self.has_badge(m, b) for m in members)
                if count > 0:
                    result[badge_attrs[b]] = count
            
            # Count premium types
            for level, name in premium_types.items():
                count = sum(self.has_premium(m, level) for m in members)
                if count > 0:
                    result[name] = count
            
            text = "\n".join(
                f"{name}: {count}"
                for name, count in result.items()
            ) or "No detectable badges or premium."
            
            await interaction.followup.send(f"**Badge & Premium counts:**\n{text}")
            return

        # Check if it's a premium type
        if badge in ["nitro_classic", "nitro", "nitro_basic"]:
            premium_map = {
                "nitro_classic": 1,
                "nitro": 2,
                "nitro_basic": 3
            }
            level = premium_map[badge]
            count = sum(self.has_premium(m, level) for m in members)
            await interaction.followup.send(
                f"**{premium_types[level]}:** {count} members"
            )
            return

        # Check if it's a badge
        if badge not in badge_attrs:
            await interaction.followup.send("Unknown badge or premium type.")
            return

        count = sum(self.has_badge(m, badge) for m in members)
        await interaction.followup.send(
            f"**{badge_attrs[badge]}:** {count} members"
        )

    # /cbu badge
    @app_commands.command(name="cbu", description="List users with a badge or premium")
    @app_commands.describe(badge="Badge name or premium type (nitro/nitro_classic/nitro_basic)")
    async def cbu(self, interaction: discord.Interaction, badge: str):
        await interaction.response.defer()
        
        guild = interaction.guild
        
        # Check if it's a premium type
        if badge in ["nitro_classic", "nitro", "nitro_basic"]:
            premium_map = {
                "nitro_classic": 1,
                "nitro": 2,
                "nitro_basic": 3
            }
            level = premium_map[badge]
            members = [
                m for m in guild.members
                if self.has_premium(m, level)
            ]
            
            if not members:
                await interaction.followup.send("No users found.")
                return

            names = "\n".join(m.mention for m in members[:50])
            await interaction.followup.send(
                f"**Users with {premium_types[level]}:**\n{names}"
            )
            return
        
        # Check if it's a badge
        if badge not in badge_attrs:
            await interaction.followup.send("Unknown badge or premium type.")
            return

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
