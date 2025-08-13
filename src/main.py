import discord
from discord.ext import commands, tasks
import os
from dotenv import load_dotenv
import Database
from pymongo import MongoClient
import certifi
import datetime
import asyncio


async def loop(bot):
    t = 10 * 60

    while True:
        now = datetime.datetime.now()
        # Check if current time is within 12 PM (noon) to 1:59 PM
        if not (12 <= now.hour < 14):
            await asyncio.sleep(t)
            continue

        await bot.mention_players()
        await asyncio.sleep(t)


class Bot(commands.Bot):
    def __init__(self):
        # Initialize intents to allow the bot to receive specific events from Discord.
        # Intents.all() requires enabling privileged intents in the Discord Developer Portal.
        intents = discord.Intents.all()
        super().__init__(command_prefix="!", intents=intents)

        # List of cogs (extensions) to load
        self.cogslist = ["Cogs.commandcog", "Cogs.eventcog"]

        # Initialize MongoDB client connection using environment variable
        self.MongoClient = MongoClient(
            os.environ.get("Database_Connection_String"), tlsCAFile=certifi.where()
        )


    async def mention_players(self):
        print("Mentioning players!")

        database = Database.get_bot_database(self.MongoClient)
        roles_collection = database["roles"]
        servers_collection = database["servers"]

        # Calculate the date for roles that need to be mentioned (8 days ago)
        wantedDate = (datetime.datetime.now() - datetime.timedelta(days=8)).date()

        objects_to_mention = roles_collection.find({"date": str(wantedDate)})
        for obj in objects_to_mention:
            # Check if this role for this date has already been mentioned
            if "mentioned" not in obj or not obj["mentioned"]:
                print(f"Found unmentioned role for date {obj['date']} in guild {obj['guild_id']}!")

                guild = await self.fetch_guild(obj["guild_id"])
                if not guild:
                    print(f"Guild {obj['guild_id']} not found or bot isn't in it.")
                    continue

                # Get the discovery channel for this guild
                server_data = servers_collection.find_one({"guild_id": obj["guild_id"]})
                if not server_data or "discovery_channel" not in server_data:
                    print(f'Could not find server data or discovery channel for guild {obj["guild_id"]}')
                    continue

                try:
                    channel = await guild.fetch_channel(server_data["discovery_channel"])
                except discord.NotFound:
                    print(f"Discovery channel {server_data['discovery_channel']} not found in guild {obj['guild_id']}. Was it deleted?")
                    continue
                except discord.Forbidden:
                    print(f"Bot does not have permission to access channel {server_data['discovery_channel']} in guild {obj['guild_id']}.")
                    continue
                except Exception as e:
                    print(f"An error occurred fetching channel {server_data['discovery_channel']}: {e}")
                    continue

                if not channel:
                    print(f"Could not find discovery channel in guild {obj['guild_id']}.")
                    continue

                try:
                    # Send message mentioning the role and delete it after a short delay
                    message = await channel.send(content=f'<@&{obj["role_id"]}>')
                    await message.delete(delay=2.0)
                    print(f"Message sent for role {obj['role_id']} in guild {obj['guild_id']}!")

                    # Mark the role as mentioned in the database
                    await roles_collection.update_one(
                        {"_id": obj["_id"]},
                        {"$set": {"mentioned": True}},
                    )
                except discord.Forbidden:
                    print(f"Bot lacks permissions to send messages in channel {channel.id} or delete messages.")
                except Exception as e:
                    print(f"An error occurred sending/deleting message for role {obj['role_id']}: {e}")

        # Delete old data (roles that are 9 days old)
        oldDate = (datetime.datetime.now() - datetime.timedelta(days=9)).date()
        print(f"Attempting to clean up old roles for date {str(oldDate)}")
        objects_to_delete = list(roles_collection.find({"date": str(oldDate)}))

        for obj in objects_to_delete:
            guild = await self.fetch_guild(obj["guild_id"])
            if not guild:
                print(f"Guild {obj['guild_id']} not found for old role cleanup.")
                continue

            role = guild.get_role(obj["role_id"])
            if not role:
                print(f"Could not find role with ID {obj['role_id']} for date {str(oldDate)} in guild {obj['guild_id']}.")
                continue

            try:
                await role.delete(
                    reason="The date became old and was ultimately cleaned up"
                )
                print(f"Deleted role {obj['role_id']} in guild {obj['guild_id']}.")
            except discord.Forbidden:
                print(f"Bot lacks permissions to delete role {obj['role_id']} in guild {obj['guild_id']}.")
            except Exception as e:
                print(f"Error deleting role {obj['role_id']}: {e}")

        # Delete all database records for the old date
        try:
            delete_result = roles_collection.delete_many({"date": str(oldDate)})
            print(f"Deleted {delete_result.deleted_count} database records for date {str(oldDate)}.")
        except Exception as e:
            print(f"Error deleting database records for old date {str(oldDate)}: {e}")


    async def setup_hook(self):
        # Load all specified cogs (extensions)
        for ext in self.cogslist:
            try:
                await self.load_extension(ext)
            except Exception as e:
                print(f"Failed to load extension {ext}: {e}")


    async def on_ready(self):
        # This event fires once the bot has successfully connected to Discord.
        print("Bot is ready!")
        # Start the custom looping task for mentioning players
        asyncio.ensure_future(loop(self))
        # Start the background task for cleaning up departure records
        self.cleanup_departures.start()

        # Synchronize slash commands with Discord
        synced = await self.tree.sync()
        print(f"Loaded {len(synced)} slash commands.")


    async def on_member_join(self, member):
        """Sends a direct message welcome message to a new or rejoining member."""
        if member.bot: # Ignore bots joining the server
            return

        database = Database.get_bot_database(self.MongoClient)
        departures_collection = database["departures"]

        # Attempt to find and delete a departure record for this user in this guild.
        # If found, it means they are rejoining.
        departed_record = departures_collection.find_one_and_delete(
            {"user_id": member.id, "guild_id": member.guild.id}
        )

welcome_message = (
    f"Hey, {member.mention}!\n"
    "Welcome to SoundCord! We're glad to have you here. Enjoy your stay and have fun with our soundboards!\n\n"
    "Feel free to join our 2nd server, SoundCord+, at https://discord.gg/FMzwMHTmv7\n\n"
    "---**CHECK OUT OUR MINECRAFT SKYBLOCK SERVER!**---\n"
    "Our IP is **playcalypso.net**\n\n"
    "We're a Skyblock server with dungeons, bosses, jobs, quests, and factories! "
    "We're in a **beta testing phase** right now, and players who provide great feedback will be rewarded with a high-value rank!"
)

        # Log whether the member is new or rejoining
        if departed_record:
            print(f"Recognized {member.name} as a returning member to {member.guild.name}.")
        else:
            print(f"New member {member.name} joined {member.guild.name}.")

        try:
            await member.send(welcome_message)
            print(f"Sent welcome message to {member.name}.")
        except discord.Forbidden:
            print(f"Could not send welcome message to {member.name} (DMs are likely disabled).")
        except Exception as e:
            print(f"An error occurred while trying to send welcome message to {member.name}: {e}")


    async def on_member_remove(self, member):
        """Records a member's departure for potential 'rejoin' detection."""
        if member.bot: # Ignore bots leaving the server
            return

        database = Database.get_bot_database(self.MongoClient)
        departures_collection = database["departures"]

        # Store the departure record with a timestamp
        try:
            departures_collection.insert_one({
                "user_id": member.id,
                "guild_id": member.guild.id,
                "departure_time": datetime.datetime.now()
            })
            print(f"Recorded departure for {member.name} from {member.guild.name}.")
        except Exception as e:
            print(f"Error recording departure for {member.name}: {e}")


    @tasks.loop(hours=24) # This task runs every 24 hours
    async def cleanup_departures(self):
        """Cleans up old departure records from the database to prevent it from growing too large."""
        # The @before_loop decorator handles waiting for the bot to be ready

        database = Database.get_bot_database(self.MongoClient)
        departures_collection = database["departures"]

        # Calculate the threshold for "old" records (e.g., older than 30 days)
        thirty_days_ago = datetime.datetime.now() - datetime.timedelta(days=30)

        try:
            delete_result = departures_collection.delete_many(
                {"departure_time": {"$lt": thirty_days_ago}} # Delete records older than 30 days
            )
            print(f"Cleaned up {delete_result.deleted_count} old departure records.")
        except Exception as e:
            print(f"Error during departure cleanup: {e}")

    @cleanup_departures.before_loop
    async def before_cleanup_departures(self):
        print("Waiting for bot to be ready before starting departure cleanup loop...")
        await self.wait_until_ready() # Ensures the bot is fully online before the loop starts


# Load environment variables from .env file (for local development)
load_dotenv()

# Create an instance of the Bot class
bot = Bot()

# Run the bot using the token from environment variables
bot.run(os.environ.get("BOT_TOKEN"))
