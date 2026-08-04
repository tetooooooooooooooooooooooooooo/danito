import discord
from discord import app_commands
from discord.ext import commands


class Owner(commands.Cog):
    """Diagnostics restricted to the bot owner. Gated on discord.py's own is_owner() check
    (the application owner / team) rather than the global manage_guild check, since that check
    is guild-permission based and has nothing to do with who owns the bot."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="servers", description="Owner only: list every server the bot is in")
    async def servers(self, interaction: discord.Interaction):
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("This command is owner-only.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guilds = sorted(self.bot.guilds, key=lambda g: g.member_count or 0, reverse=True)
        total_members = sum(g.member_count or 0 for g in guilds)

        embed = discord.Embed(
            title=f"🌐 In {len(guilds)} server(s)",
            description=f"Total reach: **{total_members}** members"
                        if guilds else "Not in any servers.",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )

        lines = [
            f"**{g.name}** (`{g.id}`) — {g.member_count or '?'} members"
            + (f" · owner {g.owner}" if g.owner else "")
            for g in guilds
        ]

        # Chunk into 1024-char fields, capped at Discord's 25-field-per-embed limit.
        chunks, current = [], ""
        for line in lines:
            line = line[:300]
            if len(current) + len(line) + 1 > 1024:
                chunks.append(current)
                current = ""
            current += line + "\n"
        if current:
            chunks.append(current)

        for i, chunk in enumerate(chunks[:25]):
            label = "Servers" if len(chunks) == 1 else f"Servers ({i + 1}/{len(chunks)})"
            embed.add_field(name=label, value=chunk, inline=False)
        if len(chunks) > 25:
            embed.set_footer(text=f"List truncated — {len(guilds)} servers total")

        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Owner(bot))
    print("Owner cog loaded ✓")
