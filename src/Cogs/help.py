import discord
from discord import app_commands
from discord.ext import commands

# Display metadata per cog, keyed on the cog class name. Keeping this explicit rather than
# deriving it from class names means categories can have proper names and a blurb explaining
# what they're for — a bare cog name tells a server admin nothing.
CATEGORIES = {
    "Moderation": ("🔨", "Moderation",
                   "Ban, kick, timeout and warn, with every action saved as a numbered case."),
    "MediaLog": ("🗄️", "Media Logging",
                 "Logs deleted images, videos and voice memos with the file attached."),
    "Ping Tracking": ("🔔", "Nudge Tracking",
                      "Records each survey nudge and how many members it reached."),
    "Server Ratings": ("⭐", "Server Ratings",
                       "Ask members to rate your server, and nudge newcomers to answer."),
    "Stats": ("📊", "Server Stats",
              "Roles, activity, badges, tags and what people are playing."),
    "ImageSpamFilter": ("🛡️", "Spam Filter",
                        "Removes batches of images posted with spam-looking filenames."),
    "Greetings": ("👋", "Greetings",
                  "Welcome and goodbye messages, written by you."),
    "Members": ("📈", "Members", "How well the server holds on to the people who join."),
    "Utility": ("🔧", "Utility", "Small tools for server admins."),
    "Help": ("❓", "Help", "This menu."),
}
FALLBACK = ("▫️", None, "")

# Never advertised. Guild-scoped commands are already absent from the global walk, but this
# also covers the case where OWNER_GUILD_ID is unset and they register globally instead.
HIDDEN_COGS = {"Owner", "admin"}


def signature(cmd: app_commands.Command) -> str:
    """/ban <member> [reason] — required args in angle brackets, optional in square."""
    parts = [f"/{cmd.qualified_name}"]
    for p in cmd.parameters:
        parts.append(f"<{p.name}>" if p.required else f"[{p.name}]")
    return " ".join(parts)


class HelpView(discord.ui.View):
    def __init__(self, cog: "Help", by_cat: dict, requester_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.by_cat = by_cat
        self.requester_id = requester_id
        self.message: discord.Message | None = None

        options = [discord.SelectOption(
            label="Overview", emoji="📚", value="__home__",
            description="All categories at a glance", default=True)]
        for key in cog.sorted_categories(by_cat):
            emoji, label, blurb = cog.meta(key)
            options.append(discord.SelectOption(
                label=label, emoji=emoji, value=key,
                description=(blurb[:100] or None)))

        self.select = discord.ui.Select(placeholder="Jump to a category…", options=options[:25])
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Otherwise anyone passing by could drive someone else's menu.
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "Run `/help` yourself to browse the menu.", ephemeral=True)
        return False

    async def _on_select(self, interaction: discord.Interaction):
        value = self.select.values[0]
        for opt in self.select.options:
            opt.default = opt.value == value
        embed = (self.cog.overview_embed(self.by_cat, interaction.user) if value == "__home__"
                 else self.cog.category_embed(value, self.by_cat[value], interaction.user))
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Overview", emoji="🏠", style=discord.ButtonStyle.secondary, row=1)
    async def home(self, interaction: discord.Interaction, _button: discord.ui.Button):
        for opt in self.select.options:
            opt.default = opt.value == "__home__"
        await interaction.response.edit_message(
            embed=self.cog.overview_embed(self.by_cat, interaction.user), view=self)

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

    def meta(self, key: str):
        emoji, label, blurb = CATEGORIES.get(key, FALLBACK)
        return emoji, label or key, blurb

    def sorted_categories(self, by_cat: dict) -> list:
        """Declaration order in CATEGORIES first (most useful first), then anything unknown."""
        known = [k for k in CATEGORIES if k in by_cat]
        return known + sorted(k for k in by_cat if k not in CATEGORIES)

    def _commands_by_category(self) -> dict:
        by_cat = {}
        for cmd in self.bot.tree.walk_commands():
            # walk_commands() yields Group containers as well as their subcommands. Groups have
            # no .binding, and listing them would duplicate their children, so only leaf
            # Commands are collected.
            if not isinstance(cmd, app_commands.Command):
                continue
            binding = getattr(cmd, "binding", None)
            if binding is None:
                continue
            # qualified_name honours a cog's `name=` kwarg, so "Discovery Helper" shows up
            # instead of the class name.
            key = getattr(binding, "qualified_name", None) or binding.__class__.__name__
            if key in HIDDEN_COGS or binding.__class__.__name__ in HIDDEN_COGS:
                continue
            by_cat.setdefault(key, []).append(cmd)
        return by_cat

    def _stamp(self, embed: discord.Embed, requester):
        embed.set_footer(text=f"Requested by {requester.display_name}",
                         icon_url=requester.display_avatar.url)
        if self.bot.user.avatar:
            embed.set_thumbnail(url=self.bot.user.avatar.url)

    def overview_embed(self, by_cat: dict, requester) -> discord.Embed:
        total = sum(len(v) for v in by_cat.values())
        embed = discord.Embed(
            title=f"{self.bot.user.name} commands",
            description=f"**{total}** commands in **{len(by_cat)}** categories.\n"
                        f"Use the menu below to see a category in detail.",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        for key in self.sorted_categories(by_cat):
            emoji, label, blurb = self.meta(key)
            cmds = sorted(by_cat[key], key=lambda c: c.qualified_name)
            listed = " ".join(f"`/{c.qualified_name}`" for c in cmds[:6])
            if len(cmds) > 6:
                listed += f" *+{len(cmds) - 6}*"
            value = (f"{blurb}\n{listed}" if blurb else listed)
            embed.add_field(name=f"{emoji}  {label} · {len(cmds)}", value=value, inline=False)

        self._stamp(embed, requester)
        return embed

    def category_embed(self, key: str, cmds: list, requester) -> discord.Embed:
        emoji, label, blurb = self.meta(key)
        embed = discord.Embed(
            title=f"{emoji}  {label}",
            description=blurb or None,
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        # One field per command would blow the 25-field cap on a big category, so they're
        # packed into a single description block instead.
        lines = []
        for cmd in sorted(cmds, key=lambda c: c.qualified_name):
            lines.append(f"**`{signature(cmd)}`**\n{cmd.description or 'No description'}")

        chunks, current = [], ""
        for line in lines:
            if len(current) + len(line) + 2 > 1024:
                chunks.append(current)
                current = ""
            current += line + "\n\n"
        if current:
            chunks.append(current)

        for i, chunk in enumerate(chunks[:24]):
            embed.add_field(
                name=f"{len(cmds)} command{'s' if len(cmds) != 1 else ''}" if i == 0 else "​",
                value=chunk, inline=False)

        self._stamp(embed, requester)
        return embed

    @app_commands.command(name="help", description="Show everything this bot can do")
    @app_commands.checks.cooldown(2, 10.0)
    async def help(self, interaction: discord.Interaction):
        by_cat = self._commands_by_category()
        view = HelpView(self, by_cat, interaction.user.id)
        await interaction.response.send_message(
            embed=self.overview_embed(by_cat, interaction.user), view=view)
        view.message = await interaction.original_response()


async def setup(bot):
    await bot.add_cog(Help(bot))
