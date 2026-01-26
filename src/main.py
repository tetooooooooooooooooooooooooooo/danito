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
        if not (12 <= now.hour < 14):
            await asyncio.sleep(t)
            continue
        await bot.mention_players()
        await asyncio.sleep(t)

class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all()
        super().__init__(command_prefix="!", intents=intents)

        self.cogslist = [
            "Cogs.commandcog",
            "Cogs.eventcog",
            "Cogs.badges",
            "Cogs.help",
            "Cogs.stats",
            "Cogs.utility",
            "Cogs.sync"
        ]

        self.MongoClient = MongoClient(
            os.environ.get("Database_Connection_String"),
            tlsCAFile=certifi.where()
        )

    async def setup_hook(self):
        for ext in self.cogslist:
            await self.load_extension(ext)

    async def on_ready(self):
        print("Bot is ready!")
        asyncio.ensure_future(loop(self))
        self.cleanup_departures.start()

        guild = discord.Object(id=1118504767296122954)  # <-- PUT YOUR SERVER ID
        synced = await self.tree.sync(guild=guild)
        print(f"Loaded {len(synced)} slash commands (guild sync).")

    async def mention_players(self):
        print("Mentioning players!")
        database = Database.get_bot_database(self.MongoClient)
        roles_collection = database["roles"]
        servers_collection = database["servers"]

        wantedDate = (datetime.datetime.now() - datetime.timedelta(days=8)).date()
        objects_to_mention = roles_collection.find({"date": str(wantedDate)})

        for obj in objects_to_mention:
            if "mentioned" not in obj or not obj["mentioned"]:
                guild = await self.fetch_guild(obj["guild_id"])
                if not guild:
                    continue

                server_data = servers_collection.find_one({"guild_id": obj["guild_id"]})
                if not server_data:
                    continue

                try:
                    channel = await guild.fetch_channel(server_data["discovery_channel"])
                    msg = await channel.send(f'<@&{obj["role_id"]}>')
                    await msg.delete(delay=2)
                    await roles_collection.update_one(
                        {"_id": obj["_id"]},
                        {"$set": {"mentioned": True}}
                    )
                except:
                    pass

    async def on_member_join(self, member):
        if member.bot:
            return

        database = Database.get_bot_database(self.MongoClient)
        departures = database["departures"]

        departures.find_one_and_delete({
            "user_id": member.id,
            "guild_id": member.guild.id
        })

        try:
            await member.send("Welcome!")
        except:
            pass

    async def on_member_remove(self, member):
        if member.bot:
            return

        database = Database.get_bot_database(self.MongoClient)
        departures = database["departures"]

        departures.insert_one({
            "user_id": member.id,
            "guild_id": member.guild.id,
            "departure_time": datetime.datetime.now()
        })

    @tasks.loop(hours=24)
    async def cleanup_departures(self):
        database = Database.get_bot_database(self.MongoClient)
        departures = database["departures"]

        threshold = datetime.datetime.now() - datetime.timedelta(days=30)
        departures.delete_many({"departure_time": {"$lt": threshold}})

    @cleanup_departures.before_loop
    async def before_cleanup_departures(self):
        await self.wait_until_ready()

# ---------- BOOT ----------

load_dotenv()
bot = Bot()
bot.run(os.environ.get("BOT_TOKEN"))
