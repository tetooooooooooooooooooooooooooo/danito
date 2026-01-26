import discord
from discord import app_commands
from discord.ext import commands
import aiohttp

LANGUAGES = {
    "English": "en",
    "Arabic": "ar",
    "French": "fr",
    "German": "de",
    "Spanish": "es",
    "Italian": "it",
    "Portuguese": "pt",
    "Russian": "ru",
    "Turkish": "tr",
    "Japanese": "ja",
    "Korean": "ko",
    "Chinese (Simplified)": "zh-CN",
    "Hindi": "hi",
    "Dutch": "nl",
    "Swedish": "sv",
    "Polish": "pl",
    "Greek": "el",
    "Persian": "fa",
    "Urdu": "ur"
}

class Utility(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.session = None

    async def cog_load(self):
        self.session = aiohttp.ClientSession()

    async def cog_unload(self):
        if self.session:
            await self.session.close()

    # ========== TRANSLATE ==========
    @app_commands.command(name="translate", description="Translate text between languages")
    @app_commands.describe(
        text="Text to translate",
        target="Target language"
    )
    @app_commands.choices(
        target=[app_commands.Choice(name=name, value=code) for name, code in LANGUAGES.items()]
    )
    async def translate(
        self,
        interaction: discord.Interaction,
        text: str,
        target: app_commands.Choice[str]
    ):
        await interaction.response.defer()

        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": "auto",
            "tl": target.value,
            "dt": "t",
            "q": text
        }

        try:
            async with self.session.get(url, params=params) as response:
                if response.status != 200:
                    await interaction.followup.send("❌ Translation failed.")
                    return

                data = await response.json()

                translated = "".join([part[0] for part in data[0]])
                detected_lang = data[2]

                detected_name = next(
                    (name for name, code in LANGUAGES.items() if code == detected_lang),
                    detected_lang
                )

                embed = discord.Embed(
                    title="🌐 Translation",
                    color=discord.Color.blue()
                )

                embed.add_field(
                    name="From",
                    value=detected_name,
                    inline=True
                )

                embed.add_field(
                    name="To",
                    value=target.name,
                    inline=True
                )

                embed.add_field(
                    name="Original",
                    value=text[:1024],
                    inline=False
                )

                embed.add_field(
                    name="Translated",
                    value=translated[:1024],
                    inline=False
                )

                embed.set_footer(text="Powered by Google Translate")

                await interaction.followup.send(embed=embed)

        except Exception as e:
            await interaction.followup.send("❌ Translation error.")

    # ========== SAY ==========
    @app_commands.command(name="say", description="Make the bot say something")
    @app_commands.describe(message="Message to send")
    async def say(self, interaction: discord.Interaction, message: str):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message(
                "❌ You need Manage Messages permission.",
                ephemeral=True
            )
            return

        await interaction.response.send_message("✅ Sent.", ephemeral=True)
        await interaction.channel.send(message)

async def setup(bot):
    await bot.add_cog(Utility(bot))
