import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
from dotenv import load_dotenv
import Database
import GuildConfig
from pymongo import MongoClient
import certifi
import datetime
import asyncio


# How often the bot writes down that it's alive. The status page calls it offline
# after a few missed beats, so this also sets how quickly an outage shows up.
HEARTBEAT_SECONDS = 60


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
        # Declared explicitly instead of Intents.all(). Everything switched on below is read
        # by something; everything left off (typing, reactions, voice states, invites,
        # webhooks, scheduled events, moderation events, DMs) was gateway traffic and memory
        # we never looked at.
        #
        # Three of these are privileged and need Discord's written approval past 100 servers,
        # so the list is kept as short as it can be:
        #   members         - join/leave handling, role.members for reminder reach, /stats scans
        #   message_content - without it Discord strips content *and attachments* from any
        #                     message that doesn't mention the bot, which would break both
        #                     MediaLog and the spam filter
        #   presences       - only /stats playing and the online_only filters need it, and it
        #                     is the most expensive of the three: Discord streams a presence
        #                     update for every status change of every member of every server.
        #                     Set PRESENCE_INTENT=0 to drop it and stay under the bar; the two
        #                     commands that depend on it then say so rather than quietly
        #                     returning nothing.
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        intents.guild_messages = True
        intents.message_content = True
        # Bans and unbans arrive on their own intent. Not privileged, and it carries almost no
        # traffic, but without it the ban and unban logs never fire at all.
        intents.moderation = True
        # Voice movement, for the voice log. Also not privileged, but unlike moderation it is
        # genuinely chatty: Discord sends an update for every mute, deafen, stream and camera
        # toggle as well as every join and leave. The log throws away everything except actual
        # movement, so the cost is gateway traffic rather than noise in anybody's channel.
        intents.voice_states = True
        intents.presences = os.environ.get("PRESENCE_INTENT", "1") != "0"

        # Larger message cache so a deleted message still carries its author/content even
        # when MediaLog no longer holds the file bytes.
        super().__init__(command_prefix="!", intents=intents, max_messages=5000)
        # Permissions are declared per command rather than gated globally. A single
        # manage_guild gate over everything meant moderators (who typically hold
        # kick/ban/timeout but not Manage Server) couldn't run moderation commands, and
        # read-only things like /help and /stats were locked away from ordinary members.
        self.tree.on_error = self.on_tree_error

        # List of cogs (extensions) to load
        self.cogslist = [
            "Cogs.Ratings",
            "Cogs.Members",
            "Cogs.Greetings",
            "Cogs.AutoRole",
            "Cogs.RoleButtons",
            "Cogs.help",
            "Cogs.stats",
            "Cogs.utility",
            "Cogs.ImageSpamFilter",
            "Cogs.MediaLog",
            "Cogs.Logging",
            "Cogs.PingLog",
            "Cogs.Moderation",
            "Cogs.AutoMod",
            "Cogs.Lifecycle",
            "Cogs.owner",
        ]

        # MongoDB connection
        self.MongoClient = MongoClient(
            os.environ.get("Database_Connection_String"),
            tlsCAFile=certifi.where()
        )

        # Log channel ID
        self.log_channel_id = 1465493782245146886

        self._ready_once = False
        self.start_time = datetime.datetime.now(datetime.timezone.utc)

        # Optional: the guild that owner-only /admin commands are registered to, so they
        # stay invisible everywhere else. Unset means they register globally instead.
        self.owner_guild_id = os.environ.get("OWNER_GUILD_ID")

    async def _db(self, fn, *args, **kwargs):
        """pymongo is synchronous, so keep it off the event loop."""
        return await asyncio.to_thread(lambda: fn(*args, **kwargs))

    async def on_tree_error(self, interaction: discord.Interaction, error):
        """Assigned to tree.on_error, which is where discord.py actually dispatches app
        command errors — a bot-level on_app_command_error method is never called."""
        if isinstance(error, app_commands.CommandInvokeError):
            error = error.original

        if isinstance(error, app_commands.MissingPermissions):
            perms = ", ".join(p.replace("_", " ").title() for p in error.missing_permissions)
            message = f"❌ You need **{perms}** to use this command."
        elif isinstance(error, app_commands.BotMissingPermissions):
            perms = ", ".join(p.replace("_", " ").title() for p in error.missing_permissions)
            message = f"❌ I'm missing **{perms}**. Grant it and try again."
        elif isinstance(error, app_commands.NoPrivateMessage):
            message = "❌ This command only works inside a server."
        elif isinstance(error, app_commands.CommandOnCooldown):
            message = f"⏳ Slow down. Try again in {error.retry_after:.0f}s."
        elif isinstance(error, app_commands.CheckFailure):
            message = "❌ You can't use this command."
        else:
            message = "❌ Something went wrong running that command."
            print(f"[COMMAND ERROR] {interaction.command and interaction.command.qualified_name}: "
                  f"{type(error).__name__}: {error}")
            await self.send_log(
                title=f"Command Error: /{interaction.command.qualified_name if interaction.command else '?'}",
                description=f"```py\n{type(error).__name__}: {str(error)[:500]}\n```",
                fields={
                    "User": f"{interaction.user} ({interaction.user.id})",
                    "Guild": f"{interaction.guild} ({interaction.guild.id})" if interaction.guild else "DM",
                },
                color=0xe74c3c,
            )

        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass

    async def send_log(self, title: str, description: str = None, fields: dict = None, color=0x2b2d31):
        """Send a formatted embed to the log channel"""
        channel = self.get_channel(self.log_channel_id)
        if not channel:
            print(f"[LOG ERROR] Channel {self.log_channel_id} not found.")
            return

        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        embed.set_footer(text=self.user.name, icon_url=self.user.display_avatar.url)

        if fields:
            for name, value in fields.items():
                embed.add_field(name=name, value=str(value)[:1024], inline=False)

        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"[LOG ERROR] Failed to send log: {e}")

    async def mention_players(self, days: int = 8, guild_id: int = None, cleanup: bool = True):
        """Remind the cohort that joined `days` ago.

        `guild_id` scopes it to one server. The scheduled midday pass leaves it None so every
        server gets its reminders, but /forcesurvey passes its own guild: without that, one admin
        running the command would fire reminders in every server the bot is in.

        Returns a summary so the caller can say what actually happened instead of guessing.
        """
        print(f"Mentioning players! (days={days}, guild={guild_id or 'all'})")
        database = Database.get_bot_database(self.MongoClient)
        roles_collection = database["roles"]

        summary = {"date": None, "found": 0, "pinged": 0, "already": 0,
                   "no_channel": 0, "failed": 0, "cleaned": 0}

        wantedDate = (datetime.datetime.now() - datetime.timedelta(days=days)).date()
        summary["date"] = str(wantedDate)

        query = {"date": str(wantedDate)}
        if guild_id is not None:
            query["guild_id"] = guild_id

        objects_to_mention = await self._db(lambda: list(roles_collection.find(query)))
        summary["found"] = len(objects_to_mention)
        for obj in objects_to_mention:
            if obj.get("mentioned"):
                summary["already"] += 1
                continue
            print(f"Found unmentioned role for date {obj['date']} in guild {obj['guild_id']}!")

            guild = await self.fetch_guild(obj["guild_id"])
            if not guild:
                print(f"Guild {obj['guild_id']} not found or bot isn't in it.")
                summary["failed"] += 1
                continue

            server_data = await GuildConfig.get(self, obj["guild_id"])
            if not server_data or "discovery_channel" not in server_data:
                print(f'Could not find server data or discovery channel for guild {obj["guild_id"]}')
                summary["no_channel"] += 1
                continue

            try:
                channel = await guild.fetch_channel(server_data["discovery_channel"])
            except discord.NotFound:
                print(f"Discovery channel {server_data['discovery_channel']} not found.")
                summary["no_channel"] += 1
                continue
            except discord.Forbidden:
                print(f"No permission to access channel {server_data['discovery_channel']}.")
                summary["no_channel"] += 1
                continue
            except Exception as e:
                print(f"Error fetching channel: {e}")
                summary["failed"] += 1
                continue

            if not channel:
                summary["no_channel"] += 1
                continue

            try:
                message = await channel.send(content=f'<@&{obj["role_id"]}>')
                await message.delete(delay=2.0)
                print(f"Message sent for role {obj['role_id']} in guild {obj['guild_id']}!")
                summary["pinged"] += 1
            except Exception as e:
                print(f"Error sending/deleting message: {e}")
                summary["failed"] += 1
                continue

            # Not awaited: pymongo is synchronous and an UpdateResult isn't awaitable.
            # This used to be `await`ed inside the try above, so it raised TypeError every
            # time, "mentioned" was never set, and the cohort got re-pinged on every pass
            # of the 10-minute loop for the whole midday window.
            try:
                await self._db(roles_collection.update_one,
                               {"_id": obj["_id"]},
                               {"$set": {"mentioned": True}})
            except Exception as e:
                print(f"Error marking role as mentioned: {e}")

            # Stamp the membership spells for this cohort, so /retention can show which
            # joining groups were reminded and which weren't.
            try:
                await self._db(
                    database["memberships"].update_many,
                    {"guild_id": obj["guild_id"], "cohort": obj["date"]},
                    {"$set": {"nudged": True}})
            except Exception as e:
                print(f"Error marking cohort as reminded: {e}")

        if not cleanup:
            return summary

        # Cleanup old roles (9 days)
        oldDate = (datetime.datetime.now() - datetime.timedelta(days=9)).date()
        print(f"Cleaning up roles for date {str(oldDate)}")
        old_query = {"date": str(oldDate)}
        if guild_id is not None:
            old_query["guild_id"] = guild_id
        objects_to_delete = await self._db(lambda: list(roles_collection.find(old_query)))

        for obj in objects_to_delete:
            guild = await self.fetch_guild(obj["guild_id"])
            if not guild:
                continue

            role = guild.get_role(obj["role_id"])
            if not role:
                continue

            try:
                await role.delete(reason="Date became old and was cleaned up")
                print(f"Deleted role {obj['role_id']} in guild {obj['guild_id']}.")
            except Exception as e:
                print(f"Error deleting role: {e}")

        try:
            delete_result = await self._db(roles_collection.delete_many, old_query)
            summary["cleaned"] = delete_result.deleted_count
            print(f"Deleted {delete_result.deleted_count} old database records.")
        except Exception as e:
            print(f"Error deleting old records: {e}")

        return summary

    # ── talking to the dashboard ─────────────────────────────────────
    async def publish_guilds(self):
        """Write which guilds the bot is in, so the dashboard can show a server picker without
        asking Discord. The two processes only share MongoDB."""
        try:
            await self._db(
                Database.get_bot_database(self.MongoClient)["runtime"].update_one,
                {"_id": "bot"},
                {"$set": {"guild_ids": [g.id for g in self.guilds],
                          "updated_at": datetime.datetime.now(datetime.timezone.utc)}},
                True)
        except Exception as e:
            print(f"[dashboard] couldn't publish the guild list: {e}")

    @tasks.loop(seconds=HEARTBEAT_SECONDS)
    async def heartbeat(self):
        """Write down that the bot is still alive, for the status page.

        The dashboard is a separate process and can't see the bot at all, so "is it up" can
        only be answered by the bot leaving a mark and the web deciding whether it's recent.
        Written into the same runtime document as the guild list, on different fields.
        """
        try:
            await self._db(
                Database.get_bot_database(self.MongoClient)["runtime"].update_one,
                {"_id": "bot"},
                {"$set": {
                    "last_seen": datetime.datetime.now(datetime.timezone.utc),
                    "started_at": self.start_time,
                    "guild_count": len(self.guilds),
                    "member_count": sum(g.member_count or 0 for g in self.guilds),
                    # None until the first heartbeat arrives, and inf if the socket is gone.
                    "latency_ms": (round(self.latency * 1000)
                                   if self.latency and self.latency == self.latency
                                   and self.latency != float("inf") else None),
                }},
                True)
        except Exception as e:
            print(f"[status] heartbeat failed: {e}")

    @heartbeat.before_loop
    async def before_heartbeat(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=10)
    async def watch_dashboard_edits(self):
        """Settings are cached for five minutes, so without this a dashboard save would look
        like it did nothing for up to five minutes. The dashboard flags the guild it changed
        and this drops the cached copy, which costs one small query every ten seconds however
        many servers there are."""
        try:
            collection = Database.get_bot_database(self.MongoClient)["config_dirty"]
            flagged = await self._db(lambda: list(collection.find({}, {"_id": 1})))
            if not flagged:
                return
            ids = [d["_id"] for d in flagged]
            for guild_id in ids:
                GuildConfig.invalidate(guild_id)
            await self._db(collection.delete_many, {"_id": {"$in": ids}})
            print(f"[dashboard] picked up changes for {len(ids)} server(s)")
        except Exception as e:
            print(f"[dashboard] change poll failed: {e}")

    @watch_dashboard_edits.before_loop
    async def before_watch(self):
        await self.wait_until_ready()

    async def on_guild_join(self, guild):
        await self.publish_guilds()

    async def on_guild_remove(self, guild):
        await self.publish_guilds()

    async def setup_hook(self):
        await GuildConfig.ensure_indexes(self)
        for ext in self.cogslist:
            try:
                await self.load_extension(ext)
            except Exception as e:
                print(f"Failed to load {ext}: {e}")

    async def on_ready(self):
        print("Bot is ready!")
        await self.publish_guilds()

        # on_ready can fire again after a gateway resume/reconnect, not just on first boot.
        # Global command sync is unnecessary (and rate-limited) to repeat on every one of
        # those — the command set hasn't changed since the last sync.
        if not self._ready_once:
            self._ready_once = True
            asyncio.ensure_future(loop(self))
            self.watch_dashboard_edits.start()
            self.heartbeat.start()

            synced = await self.tree.sync()
            print(f"Loaded {len(synced)} slash commands.")
            print(f"Presence intent: {'on' if self.intents.presences else 'off'} "
                  f"({len(self.guilds)} servers)")

            # Guild-scoped commands live in a separate scope and need their own sync.
            if self.owner_guild_id:
                try:
                    owner_guild = discord.Object(id=int(self.owner_guild_id))
                    private = await self.tree.sync(guild=owner_guild)
                    print(f"Loaded {len(private)} owner-only commands "
                          f"in guild {self.owner_guild_id}.")
                except Exception as e:
                    print(f"Failed to sync owner commands: {e}")

# Load environment variables
load_dotenv()

# Create bot instance
bot = Bot()
# Run the bot
bot.run(os.environ.get("BOT_TOKEN"))
