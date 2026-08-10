"""Support tickets, opened on the dashboard and answered from Discord.

The two halves live in different processes and share only Mongo, so this works the same way
role panels do: the website writes the ticket down and raises a flag, and the bot is what
announces it and what carries a reply back.

A reply reaches the person by direct message, not only on the website. Somebody who opened a
ticket and closed the tab has no reason to keep checking a page, and a support system nobody
notices being answered is the same as one nobody answered.
"""

import asyncio
import datetime
import os
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import Database
from Brand import MINT

OWNER_GUILD_ID = os.environ.get("OWNER_GUILD_ID")
SUPPORT_CHANNEL_ID = os.environ.get("SUPPORT_CHANNEL_ID")
DASHBOARD_URL = (os.environ.get("DASHBOARD_URL") or "").strip().rstrip("/")

CHECK_EVERY = 15
MAX_BODY = 2000

COLOR_OPEN = 0xE67E22
COLOR_ANSWERED = MINT
COLOR_CLOSED = 0x99AAB5

CATEGORIES = {
    "broken": "Something isn't working",
    "howto": "How do I do something",
    "idea": "An idea or a request",
    "data": "Data, privacy or deletion",
    "other": "Something else",
}
STATUS_COLORS = {"open": COLOR_OPEN, "answered": COLOR_ANSWERED, "closed": COLOR_CLOSED}


def _trim(text: str, limit: int = 1024) -> str:
    text = (text or "").strip() or "*empty*"
    return text if len(text) <= limit else text[:limit - 1] + "…"


class Support(commands.GroupCog, name="Support", group_name="tickets",
              group_description="Support tickets from the dashboard"):
    """Reading and answering what people send in."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Same gate as the owner tools: the application owner, not a guild permission, so a
        # server admin can't grant it to themselves.
        if await self.bot.is_owner(interaction.user):
            return True
        await interaction.response.send_message("Unknown command.", ephemeral=True)
        return False

    async def _run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(lambda: fn(*args, **kwargs))

    @property
    def tickets(self):
        return Database.get_bot_database(self.bot.MongoClient)["tickets"]

    async def cog_load(self):
        try:
            await self._run(self._ensure_indexes)
        except Exception as e:
            print(f"[Support] index setup failed: {e}")
        self.announce.start()

    async def cog_unload(self):
        self.announce.cancel()

    def _ensure_indexes(self):
        self.tickets.create_index([("number", 1)], unique=True, name="number")
        self.tickets.create_index([("user_id", 1), ("created_at", -1)], name="user_recent")
        self.tickets.create_index([("posted", 1)], name="pending")

    # ── announcing ───────────────────────────────────────────────────
    def _embed(self, doc: dict) -> discord.Embed:
        status = doc.get("status", "open")
        embed = discord.Embed(
            title=f"#{doc.get('number')} · {_trim(doc.get('subject'), 200)}",
            color=STATUS_COLORS.get(status, COLOR_OPEN),
            timestamp=discord.utils.utcnow())
        embed.add_field(name="From",
                        value=f"{doc.get('user_tag', 'somebody')}\n`{doc.get('user_id')}`",
                        inline=True)
        embed.add_field(name="About",
                        value=CATEGORIES.get(doc.get("category"), "Something else"),
                        inline=True)
        if doc.get("guild_name"):
            embed.add_field(name="Server",
                            value=f"{doc['guild_name']}\n`{doc.get('guild_id')}`", inline=True)
        embed.add_field(name="What they said", value=_trim(doc.get("body")), inline=False)

        # Only the last few, since the whole thread is on the website anyway.
        recent = (doc.get("messages") or [])[-3:]
        if recent:
            lines = []
            for m in recent:
                who = "They said" if m.get("from") == "you" else "You said"
                lines.append(f"**{who}:** {_trim(m.get('body'), 260)}")
            embed.add_field(name="Since then", value=_trim("\n\n".join(lines)), inline=False)

        embed.set_footer(text=f"{status} · reply with /tickets reply {doc.get('number')}")
        return embed

    @tasks.loop(seconds=CHECK_EVERY)
    async def announce(self):
        """Post anything new, and anything the person has added to since."""
        if not SUPPORT_CHANNEL_ID:
            return
        try:
            due = await self._run(lambda: list(
                self.tickets.find({"posted": False}).limit(10)))
        except Exception as e:
            print(f"[Support] couldn't look for new tickets: {e}")
            return
        if not due:
            return

        channel = self.bot.get_channel(int(SUPPORT_CHANNEL_ID))
        if channel is None:
            print(f"[Support] can't see channel {SUPPORT_CHANNEL_ID}")
            return

        for doc in due:
            try:
                await channel.send(embed=self._embed(doc))
            except (discord.Forbidden, discord.HTTPException) as e:
                print(f"[Support] couldn't post ticket {doc.get('number')}: {e}")
                continue
            # Cleared even if the next one fails, so one bad ticket can't replay forever.
            await self._run(self.tickets.update_one,
                            {"_id": doc["_id"]}, {"$set": {"posted": True}})

    @announce.before_loop
    async def before_announce(self):
        await self.bot.wait_until_ready()

    # ── answering ────────────────────────────────────────────────────
    async def _find(self, number: int) -> Optional[dict]:
        try:
            return await self._run(self.tickets.find_one, {"number": int(number)})
        except Exception as e:
            print(f"[Support] lookup failed: {e}")
            return None

    async def _tell_them(self, doc: dict, body: str, closed: bool = False) -> bool:
        """Direct message the person who opened it. Returns whether it landed."""
        user = self.bot.get_user(int(doc["user_id"]))
        if user is None:
            try:
                user = await self.bot.fetch_user(int(doc["user_id"]))
            except (discord.NotFound, discord.HTTPException):
                return False

        embed = discord.Embed(
            title=f"Ticket #{doc['number']} · {_trim(doc.get('subject'), 200)}",
            description=_trim(body, 4000),
            color=COLOR_CLOSED if closed else COLOR_ANSWERED,
            timestamp=discord.utils.utcnow())
        if closed:
            embed.set_footer(text="This ticket is now closed.")
        elif DASHBOARD_URL:
            embed.add_field(name="Reply", value=f"{DASHBOARD_URL}/support", inline=False)
        else:
            embed.set_footer(text="Reply on the support page of the dashboard.")

        try:
            await user.send(embed=embed)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False          # plenty of people have DMs closed

    @app_commands.command(name="list", description="Tickets waiting on a reply")
    @app_commands.describe(status="Which ones. Defaults to the ones needing an answer.")
    @app_commands.choices(status=[
        app_commands.Choice(name="Waiting on us", value="open"),
        app_commands.Choice(name="Replied", value="answered"),
        app_commands.Choice(name="Closed", value="closed"),
        app_commands.Choice(name="All of them", value="all"),
    ])
    async def list_tickets(self, interaction: discord.Interaction,
                           status: Optional[app_commands.Choice[str]] = None):
        await interaction.response.defer(ephemeral=True)
        chosen = status.value if status else "open"
        query = {} if chosen == "all" else {"status": chosen}
        try:
            found = await self._run(lambda: list(
                self.tickets.find(query).sort("updated_at", -1).limit(20)))
        except Exception as e:
            await interaction.followup.send(f"Couldn't read them: {e}", ephemeral=True)
            return

        embed = discord.Embed(title="Support tickets", color=COLOR_ANSWERED,
                              timestamp=discord.utils.utcnow())
        if not found:
            embed.description = ("Nothing waiting." if chosen == "open"
                                 else "Nothing matching that.")
        else:
            lines = []
            for doc in found:
                when = doc.get("updated_at") or doc.get("created_at")
                stamp = (f"<t:{int(when.replace(tzinfo=datetime.timezone.utc).timestamp())}:R>"
                         if isinstance(when, datetime.datetime) else "")
                lines.append(f"`#{doc.get('number')}` **{_trim(doc.get('subject'), 60)}** · "
                             f"{doc.get('user_tag', '?')} · {doc.get('status')} {stamp}")
            embed.description = _trim("\n".join(lines), 4000)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="show", description="Read one ticket in full")
    @app_commands.describe(number="The ticket number.")
    async def show(self, interaction: discord.Interaction, number: int):
        await interaction.response.defer(ephemeral=True)
        doc = await self._find(number)
        if doc is None:
            await interaction.followup.send(f"No ticket #{number}.", ephemeral=True)
            return
        await interaction.followup.send(embed=self._embed(doc), ephemeral=True)

    @app_commands.command(name="reply", description="Answer a ticket")
    @app_commands.describe(number="The ticket number.", message="What to send back.")
    async def reply(self, interaction: discord.Interaction, number: int,
                    message: app_commands.Range[str, 1, MAX_BODY]):
        await interaction.response.defer(ephemeral=True)
        doc = await self._find(number)
        if doc is None:
            await interaction.followup.send(f"No ticket #{number}.", ephemeral=True)
            return
        if doc.get("status") == "closed":
            await interaction.followup.send(
                f"Ticket #{number} is closed. They'd have to open a new one.", ephemeral=True)
            return

        now = datetime.datetime.now(datetime.timezone.utc)
        await self._run(
            self.tickets.update_one, {"_id": doc["_id"]},
            {"$set": {"status": "answered", "updated_at": now},
             "$push": {"messages": {"from": "staff", "author": str(interaction.user),
                                    "body": message, "at": now}}})

        told = await self._tell_them(doc, message)
        note = ("They've been sent a direct message." if told else
                "Their DMs are closed, so they'll only see it on the website.")
        await interaction.followup.send(f"Replied to #{number}. {note}", ephemeral=True)

    @app_commands.command(name="close", description="Close a ticket")
    @app_commands.describe(number="The ticket number.",
                           message="Optional last word, sent to them as well.")
    async def close(self, interaction: discord.Interaction, number: int,
                    message: Optional[app_commands.Range[str, 1, MAX_BODY]] = None):
        await interaction.response.defer(ephemeral=True)
        doc = await self._find(number)
        if doc is None:
            await interaction.followup.send(f"No ticket #{number}.", ephemeral=True)
            return
        if doc.get("status") == "closed":
            await interaction.followup.send(f"#{number} is already closed.", ephemeral=True)
            return

        now = datetime.datetime.now(datetime.timezone.utc)
        update = {"$set": {"status": "closed", "updated_at": now}}
        if message:
            update["$push"] = {"messages": {"from": "staff", "author": str(interaction.user),
                                            "body": message, "at": now}}
        await self._run(self.tickets.update_one, {"_id": doc["_id"]}, update)

        if message:
            await self._tell_them(doc, message, closed=True)
        await interaction.followup.send(f"Closed #{number}.", ephemeral=True)


async def setup(bot: commands.Bot):
    cog = Support(bot)
    if OWNER_GUILD_ID:
        await bot.add_cog(cog, guild=discord.Object(id=int(OWNER_GUILD_ID)))
        print(f"Support cog loaded ✓ (private to guild {OWNER_GUILD_ID})")
    else:
        await bot.add_cog(cog)
        print("Support cog loaded ✓ (WARNING: OWNER_GUILD_ID unset — /tickets is globally "
              "visible, though still owner-only to run)")
