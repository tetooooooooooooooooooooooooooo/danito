import discord
from discord import app_commands
from discord.ext import commands
from collections import Counter

class TagInfo(commands.Cog):
    """
    Shows summary of primary guild/server tags used by members in this server.
    Optimized for large servers (defaults to online members only).
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="guildtags",
        description="Show summary of primary guild/server tags in this server (online members by default)"
    )
    @app_commands.describe(
        online_only="Only scan online members? (recommended for large servers)",
        debug="Show debug stats?"
    )
    @app_commands.choices(
        online_only=[
            app_commands.Choice(name="Yes (fast)", value="yes"),
            app_commands.Choice(name="No - full server (slow!)", value="no")
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
            await interaction.response.send_message("❌ This command only works in a server.", ephemeral=True)
            return

        await interaction.response.defer()

        if online_only == "yes":
            members = [m for m in guild.members if m.status != discord.Status.offline]
            scan_note = " (online members only – faster for large servers)"
        else:
            members = guild.members
            scan_note = " (full member list – may take minutes or timeout on 80k+ servers)"

        processed = len(members)
        tag_counts = Counter()
        total_with_tag = 0
        fetch_attempts = 0

        for member in members:
            primary = member.primary_guild

            if primary is None:
                fetch_attempts += 1
                try:
                    user = await self.bot.fetch_user(member.id)
                    primary = user.primary_guild
                except:
                    continue

            if primary and primary.tag:
                tag_counts[primary.tag] += 1
                total_with_tag += 1

        embed = discord.Embed(
            title=f"Primary Guild Tags in {guild.name}{scan_note}",
            description=f"**{total_with_tag}** members have a visible primary tag.\nProcessed **{processed}** / **{guild.member_count}** members.",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=guild.icon.url if guild.icon else None)

        if tag_counts:
            top_tags = tag_counts.most_common(15)
            top_text = "\n".join(f"**{tag}**: {count}" for tag, count in top_tags)
            embed.add_field(name=f"Top Tags ({len(tag_counts)} unique)", value=top_text, inline=False)
        else:
            embed.description += "\n\nNo primary guild tags detected in the scanned members."

        if debug == "yes":
            debug_info = (
                f"Processed members: {processed}/{guild.member_count}\n"
                f"Online-only mode: {online_only == 'yes'}\n"
                f"Fetch attempts: {fetch_attempts}\n"
                f"Unique tags: {len(tag_counts)}"
            )
            embed.add_field(name="Debug Info", value=f"```py\n{debug_info}```", inline=False)

        await interaction.followup.send(embed=embed)

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TagInfo(bot))
    print("GuildTags cog loaded ✓")
