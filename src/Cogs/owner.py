"""Owner-only tooling, grouped under /admin.

Two independent layers keep these private:

1. Visibility — when OWNER_GUILD_ID is set, these commands are registered to that one guild
   only. Discord never sends them to any other server, so they don't appear in anyone else's
   command picker. Global commands can only be *greyed out* for others, never hidden.
2. Execution — every callback still checks is_owner() first. That's the real gate: it's the
   application owner (or team), not a guild permission, so a server admin can't grant it to
   themselves. Layer 1 is cosmetic; this one is load-bearing.
"""

import datetime
import os
import time

import discord
from discord import app_commands
from discord.ext import commands

OWNER_GUILD_ID = os.environ.get("OWNER_GUILD_ID")


def _fmt_delta(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def _chunk(lines: list, limit: int = 1024) -> list:
    chunks, current = [], ""
    for line in lines:
        line = line[:limit]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current += line + "\n"
    return chunks or [""]


class Owner(commands.GroupCog, name="Owner", group_name="admin",
            group_description="Bot owner tools"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Runs before every subcommand in this group."""
        if await self.bot.is_owner(interaction.user):
            return True
        # Deliberately vague: don't advertise that a hidden command exists.
        await interaction.response.send_message("Unknown command.", ephemeral=True)
        return False

    # ── /admin servers ───────────────────────────────────────────────
    @app_commands.command(name="servers", description="List every server the bot is in")
    async def servers(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        guilds = sorted(self.bot.guilds, key=lambda g: g.member_count or 0, reverse=True)
        total_members = sum(g.member_count or 0 for g in guilds)

        embed = discord.Embed(
            title=f"🌐 In {len(guilds)} server(s)",
            description=f"Total reach: **{total_members:,}** members"
                        if guilds else "Not in any servers.",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        lines = [
            f"**{g.name}** (`{g.id}`) — {g.member_count or '?'} members"
            + (f" · owner {g.owner}" if g.owner else "")
            for g in guilds
        ]
        chunks = _chunk(lines)
        for i, chunk in enumerate(chunks[:25]):
            label = "Servers" if len(chunks) == 1 else f"Servers ({i + 1}/{len(chunks)})"
            embed.add_field(name=label, value=chunk, inline=False)
        if len(chunks) > 25:
            embed.set_footer(text=f"List truncated — {len(guilds)} servers total")

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /admin info ──────────────────────────────────────────────────
    @app_commands.command(name="info", description="Bot health: uptime, latency, cache, cogs")
    async def info(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        started = getattr(self.bot, "start_time", None)
        uptime = _fmt_delta((discord.utils.utcnow() - started).total_seconds()) if started else "?"

        embed = discord.Embed(
            title="🩺 Bot Health",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Uptime", value=uptime, inline=True)
        embed.add_field(name="Latency", value=f"{self.bot.latency * 1000:.0f} ms", inline=True)
        embed.add_field(name="Servers", value=f"{len(self.bot.guilds):,}", inline=True)
        embed.add_field(
            name="Members",
            value=f"{sum(g.member_count or 0 for g in self.bot.guilds):,}", inline=True)
        embed.add_field(name="Cached messages", value=f"{len(self.bot.cached_messages):,}", inline=True)
        embed.add_field(name="Cogs", value=f"{len(self.bot.cogs)}", inline=True)

        medialog = self.bot.get_cog("MediaLog")
        if medialog is not None:
            s = medialog.stats
            embed.add_field(
                name="Media log",
                value=f"held {len(medialog._cache)} msg • "
                      f"{medialog._bytes / (1024 * 1024):.1f} MB in memory\n"
                      f"cached {s['cached']} • logged {s['logged']} • "
                      f"too big {s['too_big']} • failed {s['failed']}",
                inline=False)

        embed.add_field(
            name="Loaded cogs", value=", ".join(sorted(self.bot.cogs)) or "none", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /admin reload ────────────────────────────────────────────────
    @app_commands.command(name="reload", description="Hot-reload a cog without redeploying")
    @app_commands.describe(cog="Extension name, e.g. Cogs.MediaLog — omit to reload everything")
    async def reload(self, interaction: discord.Interaction, cog: str = None):
        await interaction.response.defer(ephemeral=True)

        targets = [cog] if cog else list(self.bot.extensions.keys())
        results = []
        for name in targets:
            try:
                await self.bot.reload_extension(name)
                results.append(f"✅ `{name}`")
            except Exception as e:
                results.append(f"❌ `{name}` — {type(e).__name__}: {str(e)[:150]}")

        embed = discord.Embed(
            title="🔄 Reload",
            description="\n".join(results)[:4000],
            color=discord.Color.green() if all(r.startswith("✅") for r in results)
            else discord.Color.red(),
        )
        embed.set_footer(text="Slash command definitions still need /sync to change.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @reload.autocomplete("cog")
    async def _reload_autocomplete(self, interaction: discord.Interaction, current: str):
        names = [n for n in self.bot.extensions if current.lower() in n.lower()]
        return [app_commands.Choice(name=n, value=n) for n in names[:25]]

    # ── /admin leave ─────────────────────────────────────────────────
    @app_commands.command(name="leave", description="Make the bot leave a server")
    @app_commands.describe(
        server_id="ID of the server to leave (see /admin servers)",
        confirm="Must be True — the bot loses access immediately",
    )
    async def leave(self, interaction: discord.Interaction, server_id: str, confirm: bool):
        await interaction.response.defer(ephemeral=True)

        if not confirm:
            await interaction.followup.send(
                "Nothing happened — re-run with `confirm: True`.", ephemeral=True)
            return
        try:
            gid = int(server_id.strip())
        except ValueError:
            await interaction.followup.send("❌ That isn't a valid server ID.", ephemeral=True)
            return

        guild = self.bot.get_guild(gid)
        if guild is None:
            await interaction.followup.send("❌ I'm not in a server with that ID.", ephemeral=True)
            return

        name = guild.name
        try:
            await guild.leave()
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Failed to leave: {e}", ephemeral=True)
            return
        await interaction.followup.send(f"👋 Left **{name}** (`{gid}`).", ephemeral=True)


async def setup(bot: commands.Bot):
    cog = Owner(bot)
    if OWNER_GUILD_ID:
        # Registered to one guild only, so these never reach any other server's picker.
        await bot.add_cog(cog, guild=discord.Object(id=int(OWNER_GUILD_ID)))
        print(f"Owner cog loaded ✓ (private to guild {OWNER_GUILD_ID})")
    else:
        await bot.add_cog(cog)
        print("Owner cog loaded ✓ (WARNING: OWNER_GUILD_ID unset — /admin is globally "
              "visible, though still owner-only to run)")
