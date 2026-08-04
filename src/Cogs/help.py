import discord
from discord import app_commands
from discord.ext import commands


class HelpView(discord.ui.View):
    """Overview embed + a category dropdown, so /help isn't a single wall of text."""

    def __init__(self, cog: "Help", by_category: dict, overview: discord.Embed):
        super().__init__(timeout=180)
        self.cog = cog
        self.by_category = by_category
        self.overview = overview
        self.message: discord.Message | None = None

        options = [discord.SelectOption(label="Overview", emoji="📚", value="__overview__", default=True)]
        for name in sorted(by_category):
            options.append(discord.SelectOption(
                label=name, emoji=cog.COG_EMOJIS.get(name, "▫️"), value=name,
            ))
        self.select = discord.ui.Select(placeholder="Browse a category…", options=options[:25])
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        value = self.select.values[0]
        for opt in self.select.options:
            opt.default = opt.value == value

        if value == "__overview__":
            embed = self.overview
        else:
            embed = self.cog.build_category_embed(value, self.by_category[value])
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    COG_EMOJIS = {
        "Stats": "📊",
        "Help": "❓",
        "Commandcog": "⚙️",
        "Eventcog": "📅",
        "MediaLog": "🗄️",
        "ImageSpamFilter": "🛡️",
        "Owner": "👑",
        "Utility": "🔧",
    }

    # Never advertised in /help. Guild-scoped commands are already absent from the global
    # walk, but this also covers the case where OWNER_GUILD_ID is unset and they fall back
    # to registering globally.
    HIDDEN_COGS = {"Owner"}

    def _commands_by_category(self) -> dict:
        by_cat = {}
        for cmd in self.bot.tree.walk_commands():
            # walk_commands() yields Group containers as well as their subcommands. Groups
            # have no .binding, and listing them would duplicate what their children already
            # show, so only leaf Commands are collected.
            if not isinstance(cmd, app_commands.Command):
                continue
            binding = getattr(cmd, "binding", None)
            cog_name = binding.__class__.__name__ if binding else "Other"
            if cog_name in self.HIDDEN_COGS:
                continue
            by_cat.setdefault(cog_name, []).append(cmd)
        return by_cat

    def build_overview_embed(self, by_cat: dict, requester: discord.abc.User) -> discord.Embed:
        total = sum(len(v) for v in by_cat.values())
        embed = discord.Embed(
            title=f"📚 {self.bot.user.name} — Commands",
            description=f"**{total}** commands across **{len(by_cat)}** categories.\n"
                        f"Pick a category from the dropdown below to see details.",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        if self.bot.user.avatar:
            embed.set_thumbnail(url=self.bot.user.avatar.url)

        for name in sorted(by_cat):
            emoji = self.COG_EMOJIS.get(name, "▫️")
            cmds = sorted(by_cat[name], key=lambda c: c.qualified_name)
            preview = ", ".join(f"`/{c.qualified_name}`" for c in cmds[:4])
            if len(cmds) > 4:
                preview += f" *+{len(cmds) - 4} more*"
            embed.add_field(name=f"{emoji} {name} ({len(cmds)})", value=preview, inline=False)

        self._footer(embed, requester)
        return embed

    def build_category_embed(self, name: str, cmds: list) -> discord.Embed:
        emoji = self.COG_EMOJIS.get(name, "▫️")
        embed = discord.Embed(
            title=f"{emoji} {name}",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        if self.bot.user.avatar:
            embed.set_thumbnail(url=self.bot.user.avatar.url)

        for cmd in sorted(cmds, key=lambda c: c.qualified_name):
            embed.add_field(
                name=f"/{cmd.qualified_name}",
                value=cmd.description or "No description",
                inline=False,
            )
        return embed

    def _footer(self, embed: discord.Embed, requester: discord.abc.User):
        embed.set_footer(
            text=f"Requested by {requester.name}",
            icon_url=requester.display_avatar.url,
        )

    @app_commands.command(name="help", description="Show all bot commands")
    async def help(self, interaction: discord.Interaction):
        by_cat = self._commands_by_category()
        overview = self.build_overview_embed(by_cat, interaction.user)
        view = HelpView(self, by_cat, overview)

        await interaction.response.send_message(embed=overview, view=view)
        view.message = await interaction.original_response()


async def setup(bot):
    await bot.add_cog(Help(bot))
