import discord
from discord import app_commands
from discord.ext import commands
from collections import defaultdict

class PlayingStatus(commands.Cog):
    """
    Shows what games members are currently playing.
    Groups by game name and shows player counts + examples.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="playing",
        description="Show what games people are playing right now (grouped by game)"
    )
    @app_commands.describe(
        online_only="Only check online members? (much faster)",
        show_examples="Show a few player names per game?"
    )
    @app_commands.choices(
        online_only=[
            app_commands.Choice(name="Online only", value="yes"),
            app_commands.Choice(name="All members", value="no")
        ],
        show_examples=[
            app_commands.Choice(name="Yes (a few names)", value="yes"),
            app_commands.Choice(name="No (just counts)", value="no")
        ]
    )
    async def playing(
        self,
        interaction: discord.Interaction,
        online_only: str = "yes",
        show_examples: str = "yes"
    ):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("❌ This command only works in servers.", ephemeral=True)
            return

        await interaction.response.defer()

        # Filter members
        if online_only == "yes":
            members = [m for m in guild.members if m.status != discord.Status.offline]
            subtitle = "Online members only"
        else:
            members = guild.members
            subtitle = "All members (slow on large servers)"

        # Group games → list of players
        game_players = defaultdict(list)

        for member in members:
            if not member.activity:
                continue

            act = member.activity

            # Only care about "Playing" type (Game)
            if isinstance(act, discord.Game) or (
                isinstance(act, discord.Activity) and act.type == discord.ActivityType.playing
            ):
                game_name = act.name.strip()
                if game_name:
                    game_players[game_name].append(member.display_name)

        if not game_players:
            embed = discord.Embed(
                title="Currently Playing",
                description="No one is playing a detectable game right now (or cache empty).",
                color=discord.Color.greyple()
            )
            embed.set_footer(text=f"Mode: {subtitle}")
            await interaction.followup.send(embed=embed)
            return

        # Sort by player count descending
        sorted_games = sorted(
            game_players.items(),
            key=lambda x: len(x[1]),
            reverse=True
        )

        embed = discord.Embed(
            title="Currently Playing",
            description=f"**{len(game_players)}** different games • {len(members):,} members scanned",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=guild.icon.url if guild.icon else None)

        # Build top games list
        lines = []
        for game, players in sorted_games[:12]:  # top 12 games
            count = len(players)
            line = f"**{game}** × {count}"
            if show_examples == "yes" and players:
                examples = ", ".join(players[:3])
                if len(players) > 3:
                    examples += f" +{len(players)-3} more"
                line += f"  • {examples}"
            lines.append(line)

        embed.add_field(
            name=f"Top Games ({len(game_players)} total)",
            value="\n".join(lines) or "None",
            inline=False
        )

        embed.set_footer(text=f"Mode: {subtitle} • Showing top {min(12, len(game_players))}")

        await interaction.followup.send(embed=embed)

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PlayingStatus(bot))
    print("PlayingStatus cog loaded ✓")
