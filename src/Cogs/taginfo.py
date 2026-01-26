import discord
from discord import app_commands
from discord.ext import commands
from collections import Counter

class TagInfo(commands.Cog):
    """Shows primary guild tags used in the server"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="guildtags",
        description="Show primary guild tags used by members (online only by default)"
    )
    @app_commands.describe(
        online_only="Scan only online members?",
        debug="Show debug info?"
    )
    @app_commands.choices(
        online_only=[
            app_commands.Choice(name="Online only", value="yes"),
            app_commands.Choice(name="All members", value="no")
        ],
        debug=[
            app_commands.Choice(name="Yes", value="yes"),
            app_commands.Choice(name="No", value="no")
        ]
    )
    async def guildtags(
        self,
        interaction: discord.Interaction,
        online_only: str = "yes",
        debug: str | None = None
    ):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("❌ Server only", ephemeral=True)
            return

        await interaction.response.defer()

        is_online_only = online_only == "yes"

        if is_online_only:
            members = [m for m in guild.members if m.status != discord.Status.offline]
            subtitle = "Online members only"
        else:
            members = guild.members
            subtitle = "All members"

        processed = len(members)
        tag_counts = Counter()
        total_tagged = 0

        for member in members:
            primary = member.primary_guild
            if primary is None:
                try:
                    user = await self.bot.fetch_user(member.id)
                    primary = user.primary_guild
                except:
                    continue

            if primary and primary.tag:
                tag_counts[primary.tag] += 1
                total_tagged += 1

        embed = discord.Embed(
            title=f"Guild Tags • {guild.name}",
            description=f"**{total_tagged}** tagged • {processed:,} / {guild.member_count:,} members",
            color=discord.Color(0x5865F2),
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=guild.icon.url if guild.icon else None)

        if tag_counts:
            top = tag_counts.most_common(12)
            embed.add_field(
                name=f"Top tags ({len(tag_counts)} unique)",
                value="\n".join(f"`{tag}` × {count}" for tag, count in top),
                inline=False
            )
        else:
            embed.description += "\n\nNo tags found in scanned members."

        embed.set_footer(text=f"Mode: {subtitle} • Debug: {'on' if debug == 'yes' else 'off'}")

        if debug == "yes":
            embed.add_field(
                name="Stats",
                value=(
                    f"Processed: {processed:,}\n"
                    f"Total members: {guild.member_count:,}\n"
                    f"Unique tags: {len(tag_counts)}\n"
                    f"Online only: {'Yes' if is_online_only else 'No'}"
                ),
                inline=False
            )

        await interaction.followup.send(embed=embed)

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TagInfo(bot))
