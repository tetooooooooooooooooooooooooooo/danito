import discord
from discord.ext import commands, tasks
import os
from dotenv import load_dotenv
import Database # Assuming Database.py handles your MongoDB connection
from pymongo import MongoClient
import certifi
import datetime
import asyncio


async def loop(bot):
    t = 10 * 60

    while True:
        now = datetime.datetime.now()
        # Only run mention_players between 12 PM and 2 PM Pacific Daylight Time (PDT)
        # Note: Datetime objects without timezone info are naive, so 'now.hour' will be local time
        # If your bot is hosted on a different timezone, you might need to adjust or use timezone-aware datetime.
        if not (12 <= now.hour < 14):
            await asyncio.sleep(t)
            continue

        await bot.mention_players()
        await asyncio.sleep(t)


class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all()
        super().__init__(command_prefix="!", intents=intents)

        self.cogslist = ["Cogs.commandcog", "Cogs.eventcog"]
        self.MongoClient = MongoClient(
            os.environ.get("Database_Connection_String"), tlsCAFile=certifi.where()
        )
        # Initialize a task for cleaning up old departure records
        self.cleanup_departures.start()


    async def mention_players(self):
        print("Mentioning players!")

        database = Database.get_bot_database(self.MongoClient)
        roles = database["roles"]
        servers = database["servers"]

        wantedDate = (datetime.datetime.now() - datetime.timedelta(days=8)).date()

        objects = roles.find({"date": str(wantedDate)})
        for object in objects:
            # Check if bot has mentioned these clients today.
            # Using 'not in' for dictionary key check is generally preferred over 'not "key" in object'
            if "mentioned" not in object or not object["mentioned"]: # Check if key exists and is False/None
                print(f"Found unmentioned role for date {object['date']} in guild {object['guild_id']}!")

                guild = await self.fetch_guild(object["guild_id"])
                if not guild:
                    print(f"Guild {object['guild_id']} not found or bot isn't in it.")
                    continue

                # Get discovery channel in server and mention role in that channel
                server = servers.find_one({"guild_id": object["guild_id"]})
                if not server or "discovery_channel" not in server:
                    print(f'Could not find server data or discovery channel for guild {object["guild_id"]}')
                    continue

                try:
                    channel = await guild.fetch_channel(server["discovery_channel"])
                except discord.NotFound:
                    print(f"Discovery channel {server['discovery_channel']} not found in guild {object['guild_id']}. Was it deleted?")
                    continue
                except discord.Forbidden:
                    print(f"Bot does not have permission to access channel {server['discovery_channel']} in guild {object['guild_id']}.")
                    continue
                except Exception as e:
                    print(f"An error occurred fetching channel {server['discovery_channel']}: {e}")
                    continue

                if not channel:
                    print(f"Could not find discovery channel in guild {object['guild_id']}.")
                    continue


                try:
                    message = await channel.send(content=f'<@&{object["role_id"]}>')
                    await message.delete(delay=2.0)
                    print(f"Message sent for role {object['role_id']} in guild {object['guild_id']}!")

                    await roles.update_one(
                        {"_id": object["_id"]},
                        {"$set": {"mentioned": True}},
                    )
                except discord.Forbidden:
                    print(f"Bot lacks permissions to send messages in channel {channel.id} or delete messages.")
                except Exception as e:
                    print(f"An error occurred sending/deleting message for role {object['role_id']}: {e}")


        # Delete old data
        oldDate = (datetime.datetime.now() - datetime.timedelta(days=9)).date()
        print(f"Attempting to clean up old roles for date {str(oldDate)}")
        objects_to_delete = list(roles.find({"date": str(oldDate)})) # Convert cursor to list to iterate safely
        
        for object in objects_to_delete:
            guild = await self.fetch_guild(object["guild_id"])
            if not guild:
                print(f"Guild {object['guild_id']} not found for old role cleanup.")
                continue

            role = guild.get_role(object["role_id"])
            if not role:
                print(f"Could not find role with ID {object['role_id']} for date {str(oldDate)} in guild {object['guild_id']}.")
                continue

            try:
                await role.delete(
                    reason="The date became old and was ultimately cleaned up"
                )
                print(f"Deleted role {object['role_id']} in guild {object['guild_id']}.")
            except discord.Forbidden:
                print(f"Bot lacks permissions to delete role {object['role_id']} in guild {object['guild_id']}.")
            except Exception as e:
                print(f"Error deleting role {object['role_id']}: {e}")

        # Delete all the data for the old date from the database
        try:
            delete_result = roles.delete_many({"date": str(oldDate)})
            print(f"Deleted {delete_result.deleted_count} database records for date {str(oldDate)}.")
        except Exception as e:
            print(f"Error deleting database records for old date {str(oldDate)}: {e}")


    async def setup_hook(self):
        for ext in self.cogslist:
            try:
                await self.load_extension(ext)
            except Exception as e:
                print(f"Failed to load extension {ext}: {e}")

    async def on_ready(self):
        print("Bot is ready!")
        self.loop.create_task(loop(self))

        synced = await self.tree.sync()
        print(f"Loaded {len(synced)} slash commands.")

    async def on_member_join(self, member):
        """Sends a direct message to a new member or a welcome back message."""
        if member.bot: # Ignore bots joining
            return

        database = Database.get_bot_database(self.MongoClient)
        departures = database["departures"]

        # Check if the user is in the 'departures' collection for this guild
        departed_record = departures.find_one_and_delete(
            {"user_id": member.id, "guild_id": member.guild.id}
        )

        message_content = "Hello!"
        if departed_record:
            # Check how long they've been gone (optional, but good for "recent" rejoining)
            # You might want to define a "recent" threshold (e.g., within 7 days)
            # For simplicity, we'll just say "welcome back" if a record exists.
            message_content = "Welcome back!"
            print(f"Recognized {member.name} as a returning member to {member.guild.name}.")
        else:
            print(f"New member {member.name} joined {member.guild.name}.")

        try:
            await member.send(message_content)
            print(f"Sent '{message_content}' to {member.name}.")
        except discord.Forbidden:
            print(f"Could not send '{message_content}' to {member.name} (DMs are likely disabled).")
        except Exception as e:
            print(f"An error occurred while trying to send '{message_content}' to {member.name}: {e}")


    async def on_member_remove(self, member):
        """Records a member's departure for potential 'rejoin' detection."""
        if member.bot: # Ignore bots leaving
            return

        database = Database.get_bot_database(self.MongoClient)
        departures = database["departures"]

        # Store the departure. Add a timestamp for cleanup.
        try:
            departures.insert_one({
                "user_id": member.id,
                "guild_id": member.guild.id,
                "departure_time": datetime.datetime.now() # Store datetime object
            })
            print(f"Recorded departure for {member.name} from {member.guild.name}.")
        except Exception as e:
            print(f"Error recording departure for {member.name}: {e}")

    @tasks.loop(hours=24) # Run this task every 24 hours
    async def cleanup_departures(self):
        """Cleans up old departure records from the database."""
        await self.wait_until_ready() # Wait until bot is fully ready

        database = Database.get_bot_database(self.MongoClient)
        departures = database["departures"]

        # Define what constitutes "old" (e.g., departed more than 30 days ago)
        thirty_days_ago = datetime.datetime.now() - datetime.timedelta(days=30)

        try:
            delete_result = departures.delete_many(
                {"departure_time": {"$lt": thirty_days_ago}}
            )
            print(f"Cleaned up {delete_result.deleted_count} old departure records.")
        except Exception as e:
            print(f"Error during departure cleanup: {e}")

    @cleanup_departures.before_loop
    async def before_cleanup_departures(self):
        print("Waiting for bot to be ready before starting departure cleanup loop...")
        await self.wait_until_ready() # Ensure bot is ready before starting loop


load_dotenv()

bot = Bot()
bot.run(os.environ.get("BOT_TOKEN"))
