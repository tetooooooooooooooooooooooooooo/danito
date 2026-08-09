"""Which invite each member came through.

Discord does not tell a bot how somebody joined. The only way to know is to keep your own
count of every invite's uses and, the moment a member arrives, work out which one went up.
That is the whole trick, and it is why this cog exists at all.

Deliberately the only thing here is the cache and the lookup. `Members.on_member_join` is the
single handler for a join, the way that cog's own docstring insists, so this one does not
listen for members at all: it is asked. Two cogs both reacting to the same join is exactly the
double processing that file was written to get rid of.

What can go wrong, and what each case is recorded as:

- No Manage Server permission. `guild.invites()` refuses, so nothing can be attributed. The
  invite the bot itself is added by does not carry that permission unless the server owner
  accepted it, and servers added before this feature existed never will have. Recorded as
  unknown, and the dashboard says which servers are in that state rather than showing an
  empty chart that looks like nobody has joined.
- Two people joining in the same instant through different invites. Both counts move between
  one fetch and the next, so which belongs to whom is genuinely unknowable. Recorded as
  unknown rather than guessed, because a wrong attribution is worse than a missing one: it
  quietly credits the wrong campaign.
- Discovery, the widget, or a server bump. There is no invite involved, so nothing moves.
  Recorded as unknown, which is honest.
- The vanity url, which is not in `guild.invites()` and has to be asked for separately.
"""

import asyncio

import discord
from discord.ext import commands

# What a join is recorded as when the invite genuinely cannot be known. A real code can never
# collide with this: Discord codes are alphanumeric.
UNKNOWN = None

# The vanity url has no code of its own in the uses list, so it gets a reserved one. Shown as
# "the vanity url" rather than as a code, since that is what a server owner calls it.
VANITY = "vanity"


class Invites(commands.Cog, name="Invites"):
    """Keeps a running count of every invite's uses, so a join can be traced back to one."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # guild_id -> {code: uses}. Rebuilt on connect, so a restart costs at most the joins
        # that happen while it is starting up.
        self.uses: dict[int, dict[str, int]] = {}
        # guild_id -> {code: inviter}. Kept beside the counts because an invite that gets
        # deleted between the join and the lookup would otherwise lose its author.
        self.authors: dict[int, dict[str, discord.abc.User | None]] = {}
        # Guilds with a lookup already in flight. A second person arriving inside that window
        # cannot be attributed anyway, so they are answered straight away rather than starting
        # another fetch. That is what stops a raid turning into one api call per joiner.
        self.busy: set[int] = set()

    # ── keeping the counts ───────────────────────────────────────────
    async def _snapshot(self, guild: discord.Guild) -> bool:
        """Read every invite in a guild and remember its use count. False if it can't."""
        if not guild.me or not guild.me.guild_permissions.manage_guild:
            return False
        try:
            invites = await guild.invites()
        except discord.Forbidden:
            return False
        except discord.HTTPException as e:
            print(f"[Invites] couldn't read invites for {guild.id}: {e}")
            return False

        self.uses[guild.id] = {i.code: (i.uses or 0) for i in invites}
        self.authors[guild.id] = {i.code: i.inviter for i in invites}

        # The vanity url is not in that list and has its own counter.
        if "VANITY_URL" in guild.features:
            try:
                vanity = await guild.vanity_invite()
            except (discord.Forbidden, discord.HTTPException):
                vanity = None
            if vanity is not None:
                self.uses[guild.id][VANITY] = vanity.uses or 0
                self.authors[guild.id][VANITY] = None
        return True

    @commands.Cog.listener()
    async def on_ready(self):
        # Every guild at once rather than in sequence: a bot in a few hundred servers would
        # otherwise spend minutes unable to attribute anything.
        await asyncio.gather(*(self._snapshot(g) for g in self.bot.guilds),
                             return_exceptions=True)
        known = sum(1 for g in self.bot.guilds if g.id in self.uses)
        print(f"[Invites] tracking invites in {known}/{len(self.bot.guilds)} servers")

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self._snapshot(guild)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        self.uses.pop(guild.id, None)
        self.authors.pop(guild.id, None)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        guild = invite.guild
        if guild is None or guild.id not in self.uses:
            return
        # A brand new invite starts at zero, so seeding it here means the first person through
        # it registers as a change rather than as an unrecognised code.
        self.uses[guild.id][invite.code] = invite.uses or 0
        self.authors[guild.id][invite.code] = invite.inviter

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        guild = invite.guild
        if guild is None or guild.id not in self.uses:
            return
        self.uses[guild.id].pop(invite.code, None)
        # The author is kept. A join attributed to this code seconds before it was deleted
        # should still be able to say who made it.

    # ── the lookup ───────────────────────────────────────────────────
    async def resolve(self, guild: discord.Guild):
        """Which invite was just used. Returns (code, inviter_id, inviter_name).

        Called by Members the moment somebody joins, before the counts are re-read, so the
        comparison is against the state from just before they arrived.
        """
        if guild.id in self.busy:
            # Somebody else arrived while this server's counts were being read. Which invite
            # belongs to which of them is unknowable, so the answer is already decided, and a
            # second fetch would cost a call to learn nothing. A raid answers instantly here
            # instead of queueing behind a few hundred rate limited requests.
            return UNKNOWN, None, None

        self.busy.add(guild.id)
        try:
            return await self._compare(guild)
        finally:
            self.busy.discard(guild.id)

    async def _compare(self, guild: discord.Guild):
        before = self.uses.get(guild.id)
        if before is None:
            # Never snapshotted: either no permission, or the bot joined mid-session. Try
            # once, so the next person through is attributable even if this one isn't.
            await self._snapshot(guild)
            return UNKNOWN, None, None

        authors = self.authors.get(guild.id, {})
        if not await self._snapshot(guild):
            return UNKNOWN, None, None
        after = self.uses.get(guild.id, {})

        # A code that appeared since the last snapshot counts as moved only if it is already
        # above zero, so an invite created and unused doesn't look like the answer.
        moved = [code for code, count in after.items() if count > before.get(code, 0)]

        # One clear winner, or nothing to say. Two at once is unknowable rather than a
        # coin flip: crediting the wrong invite is worse than crediting none.
        if len(moved) != 1:
            return UNKNOWN, None, None

        code = moved[0]
        inviter = authors.get(code) or self.authors.get(guild.id, {}).get(code)
        if inviter is None:
            return code, None, None
        return code, inviter.id, (getattr(inviter, "global_name", None) or inviter.name)

    def tracked(self, guild_id: int) -> bool:
        """Whether this server's invites can be read at all, for the dashboard to say so."""
        return guild_id in self.uses


async def setup(bot: commands.Bot):
    await bot.add_cog(Invites(bot))
    print("✓ Invites cog loaded")
