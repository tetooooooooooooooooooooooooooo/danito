import discord
from discord import app_commands
from discord.ext import commands
from collections import Counter
from datetime import datetime, timedelta

class Stats(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="roletop", description="Show most common roles in server")
    @app_commands.describe(limit="Number of roles to show (default: 10)")
    async def roletop(self, interaction: discord.Interaction, limit: int = 10):
        await interaction.response.defer()
        
        guild = interaction.guild
        if not guild:
            return
        
        # Count role occurrences (excluding @everyone)
        role_counts = Counter()
        for member in guild.members:
            for role in member.roles:
                if role.name != "@everyone":
                    role_counts[role] += 1
        
        if not role_counts:
            await interaction.followup.send("No roles found.")
            return
        
        # Get top roles
        top_roles = role_counts.most_common(limit)
        
        # Calculate total members for percentages
        total_members = len(guild.members)
        
        # Build response
        lines = []
        for i, (role, count) in enumerate(top_roles, 1):
            percentage = (count / total_members) * 100
            lines.append(f"`{i}.` {role.mention} - **{count}** members ({percentage:.1f}%)")
        
        embed = discord.Embed(
            title=f"🏆 Top {len(top_roles)} Roles in {guild.name}",
            description="\n".join(lines),
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"Total members: {total_members}")
        
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="activity", description="Show server activity stats")
    @app_commands.describe(
        channel="Specific channel to analyze (optional)",
        hours="Hours to look back (default: 24)"
    )
    async def activity(
        self, 
        interaction: discord.Interaction, 
        channel: discord.TextChannel = None,
        hours: int = 24
    ):
        await interaction.response.defer()
        
        guild = interaction.guild
        target_channel = channel or interaction.channel
        
        # Limit hours to prevent excessive API calls
        if hours > 168:  # 1 week max
            hours = 168
        
        time_threshold = datetime.now(datetime.timezone.utc) - timedelta(hours=hours)
        
        try:
            # Collect messages
            messages = []
            async for message in target_channel.history(
                limit=None, 
                after=time_threshold
            ):
                messages.append(message)
            
            if not messages:
                await interaction.followup.send(
                    f"No messages found in {target_channel.mention} in the last {hours} hours."
                )
                return
            
            # Calculate stats
            total_messages = len(messages)
            unique_users = len(set(m.author.id for m in messages))
            
            # Messages per hour
            messages_per_hour = total_messages / hours
            
            # Top chatters
            user_counts = Counter(m.author for m in messages)
            top_chatters = user_counts.most_common(5)
            
            # Hour distribution
            hour_counts = Counter(m.created_at.hour for m in messages)
            
            # Build embed
            embed = discord.Embed(
                title=f"📊 Activity Stats for {target_channel.name}",
                color=discord.Color.green(),
                timestamp=datetime.now(datetime.timezone.utc)
            )
            
            embed.add_field(
                name="📈 Overview",
                value=f"**Total Messages:** {total_messages}\n"
                      f"**Unique Users:** {unique_users}\n"
                      f"**Time Period:** {hours} hours\n"
                      f"**Avg per Hour:** {messages_per_hour:.1f}",
                inline=False
            )
            
            # Top chatters
            chatter_lines = []
            for i, (user, count) in enumerate(top_chatters, 1):
                percentage = (count / total_messages) * 100
                chatter_lines.append(f"`{i}.` {user.mention} - {count} ({percentage:.1f}%)")
            
            embed.add_field(
                name="💬 Top Chatters",
                value="\n".join(chatter_lines) if chatter_lines else "None",
                inline=False
            )
            
            # Peak hour
            if hour_counts:
                peak_hour, peak_count = hour_counts.most_common(1)[0]
                embed.add_field(
                    name="⏰ Peak Hour",
                    value=f"{peak_hour:02d}:00 UTC ({peak_count} messages)",
                    inline=True
                )
            
            await interaction.followup.send(embed=embed)
            
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I don't have permission to read message history in that channel."
            )
        except Exception as e:
            await interaction.followup.send(f"❌ An error occurred: {str(e)}")

async def setup(bot):
    await bot.add_cog(Stats(bot))
