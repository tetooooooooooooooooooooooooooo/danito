import discord
from discord.ext import commands
from discord import app_commands
import Database
import datetime


class eventcog(commands.Cog):
    def __init__(self, client: commands.Bot):
        self.Client = client

    # When a player checks their activity level, the level should be changed accordingly to how long ago the activity-level changed. So a given amount of points should be removed for every day that has progressed.
    @commands.Cog.listener()
    async def on_member_join(self, member):
        # Add role for date to member
        database = Database.get_bot_database(self.Client.MongoClient)
        roles = database["roles"]
        servers = database["servers"]

        date = datetime.date.today()

        # Get role for today
        role_data = roles.find_one({"date": str(date), "guild_id": member.guild.id})

        role = None
        if not role_data:
            print("Creating new role!")
            # If role doesn't exist then create it
            role = await member.guild.create_role(name=str(date))
            roles.insert_one(
                {
                    "date": str(date),
                    "role_id": role.id,
                    "guild_id": member.guild.id,
                }
            )
        else:
            print("Getting role")
            role = member.guild.get_role(role_data["role_id"])
            if not role:
                print("Did not find role?!")
                role = await member.guild.create_role(name=str(date))
                roles.find_one_and_replace(
                    {
                        "date": str(date),
                        "role_id": role_data["role_id"],
                        "guild_id": member.guild.id,
                    },
                    {
                        "date": str(date),
                        "role_id": role.id,
                        "guild_id": member.guild.id,
                    },
                )

        print("Adding role!")
        # Add member to role!
        await member.add_roles(role, reason="Added discovery role to member!")

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        # Remove data for member if it excists
        return

    @commands.Cog.listener()
    async def on_interaction(self, interaction):
        # Check if its a button
        print("Interaction!")

        if not (interaction.type == discord.InteractionType.component):
            return

        if not (interaction.message):
            return

        # Check if the interactions message is the survey message
        database = Database.get_bot_database(self.Client.MongoClient)
        servers = database["servers"]

        server_data = servers.find_one(
            {
                "guild_id": interaction.guild.id,
                "discovery_channel": interaction.channel.id,
                "discovery_message": interaction.message.id,
            }
        )
        if not server_data:
            print("No server data found...")
            return

        await interaction.response.send_message(
            content=f"Thank you for participating in this survey! Enjoy your stay at {interaction.guild.name}!",
            ephemeral=True,
        )


async def setup(client: commands.Bot):
    await client.add_cog(eventcog(client))
