import discord
from discord.ext import commands
from discord import app_commands
import Database


def is_admin(interaction: discord.Interaction):
    # Check if user has permission administrator
    if not interaction.user.guild_permissions.administrator:
        return False

    return True


class commandcog(commands.Cog):
    def __init__(self, client: commands.Bot):
        self.Client = client

    @app_commands.command(
        name="setchannel", description="Set the channel to the discovery channel"
    )
    @app_commands.check(is_admin)
    async def set_discovery_channel(self, interaction: discord.Interaction):
        bot_database = Database.get_bot_database(self.Client.MongoClient)
        servers = bot_database["servers"]

        # Create message for rating in channel here
        view = discord.ui.View()

        for i in range(10):
            button = discord.ui.Button(
                label=f"{i+1}", style=discord.ButtonStyle.blurple
            )
            view.add_item(button)

        message = await interaction.channel.send(
            content=f"How has your experience been with the server?", view=view
        )

        # Set channel for discovery here
        found = servers.find_one_and_replace(
            {
                "guild_id": interaction.guild.id,
            },
            {
                "guild_id": interaction.guild.id,
                "discovery_channel": interaction.channel.id,
                "discovery_message": message.id,
            },
        )

        if not found:
            servers.insert_one(
                {
                    "guild_id": interaction.guild.id,
                    "discovery_channel": interaction.channel.id,
                    "discovery_message": message.id,
                },
            )

    @set_discovery_channel.error
    async def get_key_error(self, interaction, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await interaction.response.send_message(
                content=f"Missing required argument {error.param.name}",
                ephemeral=True,
            )
            return

        if isinstance(error, commands.MissingPermissions):
            await interaction.response.send_message(
                content=f"Missing required permission",
                ephemeral=True,
            )
            return

    @app_commands.command(name="forcesurvey", description="Force survey")
    @app_commands.check(is_admin)
    async def force_survey(self, interaction: discord.Interaction):
        self.mention_players()

        await interaction.response.send_message(
            content=f"Ok I'll try to mention the players for today! I may already have mentioned them, and I will not mention them again :)",
            ephemeral=True,
        )

    @force_survey.error
    async def get_key_error(self, interaction, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await interaction.response.send_message(
                content=f"Missing required argument {error.param.name}",
                ephemeral=True,
            )
            return

        if isinstance(error, commands.MissingPermissions):
            await interaction.response.send_message(
                content=f"Missing required permission",
                ephemeral=True,
            )
            return


async def setup(client: commands.Bot):
    await client.add_cog(commandcog(client))
