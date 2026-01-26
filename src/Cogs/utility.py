import discord
from discord import app_commands
from discord.ext import commands
import aiohttp

# ────────────────────────────────────────────────
#  Configurable constants
# ────────────────────────────────────────────────

SYNC_ROLE_ID = 1123406231210565743

LANGUAGES = {
    "English":           "en",
    "Arabic":            "ar",
    "French":            "fr",
    "German":            "de",
    "Spanish":           "es",
    "Italian":           "it",
    "Portuguese":        "pt",
    "Russian":           "ru",
    "Turkish":           "tr",
    "Japanese":          "ja",
    "Korean":            "ko",
    "Chinese (Simplified)": "zh-CN",
    "Hindi":             "hi",
    "Dutch":             "nl",
    "Swedish":           "sv",
    "Polish":            "pl",
    "Greek":             "el",
    "Persian":           "fa",
    "Urdu":              "ur",
    # feel free to add more
}

# ────────────────────────────────────────────────
class Utility(commands.Cog):
    """Utility commands: translation, say, sync"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None

    async def cog_load(self):
        self.session = aiohttp.ClientSession()

    async def cog_unload(self):
        if self.session is not None:
            await self.session.close()

    # ── Sync slash commands ────────────────────────────────────────
    @app_commands.command(name="sync", description="Sync slash commands (guild only)")
    async def sync(self, interaction: discord.Interaction):
        """Only users with the specified role can sync commands for this guild."""
        role = interaction.guild.get_role(SYNC_ROLE_ID)

        if not role or role not in interaction.user.roles:
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            synced = await self.bot.tree.sync(
                guild=discord.Object(id=interaction.guild.id)
            )
            await interaction.followup.send(
                f"✅ Synced **{len(synced)}** command{'s' if len(synced) != 1 else ''} "
                f"to this guild.",
                ephemeral=True
            )
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"❌ Failed to sync commands: {exc}",
                ephemeral=True
            )

    # ── Translate ──────────────────────────────────────────────────
    @app_commands.command(name="translate", description="Translate text to another language")
    @app_commands.describe(
        text="Text you want to translate (max ~500 chars recommended)",
        target="Target language"
    )
    @app_commands.choices(
        target=[app_commands.Choice(name=name, value=code) for name, code in LANGUAGES.items()]
    )
    async def translate(
        self,
        interaction: discord.Interaction,
        text: app_commands.Range[str, 1, 1000],
        target: app_commands.Choice[str]
    ):
        if self.session is None:
            await interaction.response.send_message("❌ Translator not ready.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": "auto",
            "tl": target.value,
            "dt": "t",
            "q": text
        }

        try:
            async with self.session.get(url, params=params, timeout=6) as r:
                if r.status != 200:
                    await interaction.followup.send("❌ Translation service returned an error.", ephemeral=True)
                    return

                data = await r.json()
                if not data or not data[0]:
                    await interaction.followup.send("❌ Could not translate.", ephemeral=True)
                    return

                translated = "".join(segment[0] for segment in data[0])
                detected_code = data[2]

                detected_name = next(
                    (name for name, code in LANGUAGES.items() if code == detected_code),
                    f"Unknown ({detected_code})"
                )

                embed = discord.Embed(color=discord.Color.blue())
                embed.set_author(name="Translation", icon_url="https://img.icons8.com/fluency/48/google-translate.png")
                embed.add_field(name="From", value=detected_name, inline=True)
                embed.add_field(name="To",   value=target.name,   inline=True)
                embed.add_field(name="Original",   value=text[:1018]     or "[empty]", inline=False)
                embed.add_field(name="Translated", value=translated[:1018] or "[empty]", inline=False)
                embed.set_footer(text="Powered by Google Translate • unofficial")

                await interaction.followup.send(embed=embed)

        except aiohttp.ClientError:
            await interaction.followup.send("❌ Could not connect to translation service.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ Unexpected error: {str(exc)[:200]}", ephemeral=True)

    # ── Say ────────────────────────────────────────────────────────
    @app_commands.command(name="say", description="Make the bot send a message")
    @app_commands.describe(message="The message to send")
    async def say(self, interaction: discord.Interaction, message: str):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message(
                "❌ You need **Manage Messages** permission to use this command.",
                ephemeral=True
            )
            return

        # You can also add: if message is too long / contains @everyone etc.
        # but keeping it simple for now

        try:
            await interaction.channel.send(message)
            await interaction.response.send_message("✅ Message sent.", ephemeral=True)
        except discord.HTTPException as exc:
            await interaction.response.send_message(f"❌ Failed to send: {exc}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Utility(bot))
