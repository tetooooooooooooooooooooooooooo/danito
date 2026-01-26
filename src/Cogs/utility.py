import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import asyncio
from datetime import datetime, timedelta
from PIL import Image, ImageFilter, ImageEnhance, ImageOps, ImageDraw, ImageFont
from io import BytesIO
import urllib.parse

class Utility(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.reminders = []  # Store reminders in memory
        self.session = None
    
    async def cog_load(self):
        """Called when the cog is loaded"""
        self.session = aiohttp.ClientSession()
        # Start reminder checker
        self.bot.loop.create_task(self.check_reminders())
    
    async def cog_unload(self):
        """Called when the cog is unloaded"""
        if self.session:
            await self.session.close()
    
    # ========== INVITES ==========
    @app_commands.command(name="invites", description="Check how many people a user has invited")
    @app_commands.describe(user="User to check (defaults to yourself)")
    async def invites(self, interaction: discord.Interaction, user: discord.Member = None):
        await interaction.response.defer()
        
        user = user or interaction.user
        guild = interaction.guild
        
        try:
            invites = await guild.invites()
            user_invites = [inv for inv in invites if inv.inviter and inv.inviter.id == user.id]
            total_uses = sum(inv.uses for inv in user_invites)
            
            embed = discord.Embed(
                title=f"📨 Invite Stats for {user.display_name}",
                color=discord.Color.green()
            )
            embed.add_field(name="Total Invites", value=str(total_uses), inline=True)
            embed.add_field(name="Invite Links", value=str(len(user_invites)), inline=True)
            embed.set_thumbnail(url=user.display_avatar.url)
            
            await interaction.followup.send(embed=embed)
        except discord.Forbidden:
            await interaction.followup.send("❌ I don't have permission to view server invites.")
    
    # ========== URBAN DICTIONARY ==========
    @app_commands.command(name="urban", description="Look up a term on Urban Dictionary")
    @app_commands.describe(term="Term to search")
    async def urban(self, interaction: discord.Interaction, term: str):
        await interaction.response.defer()
        
        url = f"https://api.urbandictionary.com/v0/define?term={urllib.parse.quote(term)}"
        
        try:
            async with self.session.get(url) as response:
                if response.status != 200:
                    await interaction.followup.send("❌ Failed to fetch definition.")
                    return
                
                data = await response.json()
                
                if not data.get("list"):
                    await interaction.followup.send(f"❌ No definition found for **{term}**.")
                    return
                
                result = data["list"][0]
                definition = result["definition"][:1024]  # Discord embed limit
                example = result.get("example", "No example")[:1024]
                
                embed = discord.Embed(
                    title=f"📖 {result['word']}",
                    url=result["permalink"],
                    color=discord.Color.orange()
                )
                embed.add_field(name="Definition", value=definition, inline=False)
                embed.add_field(name="Example", value=example, inline=False)
                embed.add_field(name="👍", value=result["thumbs_up"], inline=True)
                embed.add_field(name="👎", value=result["thumbs_down"], inline=True)
                embed.set_footer(text=f"By {result['author']}")
                
                await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(f"❌ An error occurred: {str(e)}")
    
    # ========== WIKIPEDIA ==========
    @app_commands.command(name="wikipedia", description="Search Wikipedia")
    @app_commands.describe(query="Search query")
    async def wikipedia(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(query)}"
        
        try:
            async with self.session.get(url) as response:
                if response.status == 404:
                    await interaction.followup.send(f"❌ No Wikipedia article found for **{query}**.")
                    return
                
                if response.status != 200:
                    await interaction.followup.send("❌ Failed to fetch Wikipedia article.")
                    return
                
                data = await response.json()
                
                embed = discord.Embed(
                    title=data["title"],
                    url=data["content_urls"]["desktop"]["page"],
                    description=data["extract"][:2048],
                    color=discord.Color.blue()
                )
                
                if data.get("thumbnail"):
                    embed.set_thumbnail(url=data["thumbnail"]["source"])
                
                await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(f"❌ An error occurred: {str(e)}")
    
    # ========== YOUTUBE ==========
    @app_commands.command(name="youtube", description="Search YouTube")
    @app_commands.describe(query="Search query")
    async def youtube(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        
        search_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}"
        
        embed = discord.Embed(
            title=f"🔎 YouTube Search: {query}",
            description=f"[Click here to see results]({search_url})",
            color=discord.Color.red()
        )
        
        await interaction.followup.send(embed=embed)
    
    # ========== TRANSLATE ==========
    @app_commands.command(name="translate", description="Translate text")
    @app_commands.describe(
        text="Text to translate",
        target_language="Target language code (e.g., es, fr, de, ja) - default: en"
    )
    async def translate(self, interaction: discord.Interaction, text: str, target_language: str = "en"):
        await interaction.response.defer()
        
        # Using Google Translate API (free, unofficial)
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": "auto",
            "tl": target_language,
            "dt": "t",
            "q": text
        }
        
        try:
            async with self.session.get(url, params=params) as response:
                if response.status != 200:
                    await interaction.followup.send("❌ Translation failed.")
                    return
                
                data = await response.json()
                translated = "".join([sentence[0] for sentence in data[0] if sentence[0]])
                
                embed = discord.Embed(
                    title="🌐 Translation",
                    color=discord.Color.blue()
                )
                embed.add_field(name="Original", value=text[:1024], inline=False)
                embed.add_field(name=f"Translated ({target_language})", value=translated[:1024], inline=False)
                
                await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(f"❌ An error occurred: {str(e)}")
    
    # ========== MOVIE ==========
    @app_commands.command(name="movie", description="Search for movie information")
    @app_commands.describe(title="Movie title")
    async def movie(self, interaction: discord.Interaction, title: str):
        await interaction.response.defer()
        
        # Using OMDB API (free tier)
        # You'll need to get a free API key from http://www.omdbapi.com/apikey.aspx
        api_key = "YOUR_OMDB_API_KEY"  # Replace with your API key or store in env
        url = f"http://www.omdbapi.com/?apikey={api_key}&t={urllib.parse.quote(title)}"
        
        try:
            async with self.session.get(url) as response:
                if response.status != 200:
                    await interaction.followup.send("❌ Failed to fetch movie info.")
                    return
                
                data = await response.json()
                
                if data.get("Response") == "False":
                    await interaction.followup.send(f"❌ Movie not found: **{title}**")
                    return
                
                embed = discord.Embed(
                    title=data["Title"],
                    description=data.get("Plot", "No plot available"),
                    color=discord.Color.gold()
                )
                
                if data.get("Poster") != "N/A":
                    embed.set_thumbnail(url=data["Poster"])
                
                embed.add_field(name="Year", value=data.get("Year", "N/A"), inline=True)
                embed.add_field(name="Rating", value=data.get("imdbRating", "N/A"), inline=True)
                embed.add_field(name="Runtime", value=data.get("Runtime", "N/A"), inline=True)
                embed.add_field(name="Genre", value=data.get("Genre", "N/A"), inline=True)
                embed.add_field(name="Director", value=data.get("Director", "N/A"), inline=True)
                embed.add_field(name="Actors", value=data.get("Actors", "N/A"), inline=False)
                
                await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(f"❌ An error occurred: {str(e)}")
    
    # ========== REMIND ME ==========
    @app_commands.command(name="remindme", description="Set a reminder")
    @app_commands.describe(
        time="Time (e.g., 10m, 1h, 2d)",
        message="Reminder message"
    )
    async def remindme(self, interaction: discord.Interaction, time: str, message: str):
        await interaction.response.defer(ephemeral=True)
        
        # Parse time
        multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        try:
            unit = time[-1].lower()
            amount = int(time[:-1])
            
            if unit not in multipliers:
                await interaction.followup.send("❌ Invalid time format. Use: 10s, 5m, 2h, 1d")
                return
            
            seconds = amount * multipliers[unit]
            remind_time = datetime.now() + timedelta(seconds=seconds)
            
            # Store reminder
            self.reminders.append({
                "user_id": interaction.user.id,
                "channel_id": interaction.channel.id,
                "message": message,
                "time": remind_time
            })
            
            await interaction.followup.send(f"✅ Reminder set for **{time}** from now!")
        except ValueError:
            await interaction.followup.send("❌ Invalid time format. Use: 10s, 5m, 2h, 1d")
    
    async def check_reminders(self):
        """Background task to check reminders"""
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            now = datetime.now()
            for reminder in self.reminders[:]:  # Copy list to avoid modification issues
                if now >= reminder["time"]:
                    try:
                        channel = self.bot.get_channel(reminder["channel_id"])
                        user = self.bot.get_user(reminder["user_id"])
                        
                        if channel and user:
                            embed = discord.Embed(
                                title="⏰ Reminder",
                                description=reminder["message"],
                                color=discord.Color.blue()
                            )
                            await channel.send(f"{user.mention}", embed=embed)
                        
                        self.reminders.remove(reminder)
                    except Exception as e:
                        print(f"Error sending reminder: {e}")
                        self.reminders.remove(reminder)
            
            await asyncio.sleep(10)  # Check every 10 seconds
    
    # ========== IMAGE MANIPULATION ==========
    async def process_avatar(self, user: discord.Member, effect: str) -> discord.File:
        """Process user avatar with various effects"""
        avatar_url = user.display_avatar.url
        
        async with self.session.get(str(avatar_url)) as response:
            avatar_bytes = await response.read()
        
        img = Image.open(BytesIO(avatar_bytes)).convert("RGBA")
        img = img.resize((512, 512))
        
        if effect == "pixelate":
            small = img.resize((32, 32), Image.NEAREST)
            img = small.resize((512, 512), Image.NEAREST)
        
        elif effect == "blur":
            img = img.filter(ImageFilter.GaussianBlur(radius=10))
        
        elif effect == "invert":
            r, g, b, a = img.split()
            rgb = Image.merge("RGB", (r, g, b))
            inverted = ImageOps.invert(rgb)
            r, g, b = inverted.split()
            img = Image.merge("RGBA", (r, g, b, a))
        
        elif effect == "grayscale":
            img = ImageOps.grayscale(img).convert("RGBA")
        
        elif effect == "deepfry":
            # Increase contrast and saturation
            enhancer = ImageEnhance.Contrast(img)
            img = enhancer.enhance(3.0)
            enhancer = ImageEnhance.Color(img)
            img = enhancer.enhance(2.0)
            enhancer = ImageEnhance.Sharpness(img)
            img = enhancer.enhance(10.0)
        
        elif effect == "frame":
            # Add a simple frame
            border = Image.new("RGBA", (562, 562), (255, 215, 0, 255))  # Gold border
            border.paste(img, (25, 25))
            img = border
        
        elif effect == "wanted":
            # Create wanted poster
            wanted = Image.new("RGBA", (512, 700), (222, 184, 135, 255))
            draw = ImageDraw.Draw(wanted)
            
            # Try to load font, fall back to default if not available
            try:
                font = ImageFont.truetype("arial.ttf", 48)
                font_small = ImageFont.truetype("arial.ttf", 24)
            except:
                font = ImageFont.load_default()
                font_small = ImageFont.load_default()
            
            # Draw text
            draw.text((256, 30), "WANTED", fill=(0, 0, 0), anchor="mt", font=font)
            wanted.paste(img, (0, 100))
            draw.text((256, 630), "REWARD: $10,000", fill=(0, 0, 0), anchor="mt", font=font_small)
            img = wanted
        
        elif effect == "trigger":
            # Add red overlay for triggered effect
            red_overlay = Image.new("RGBA", img.size, (255, 0, 0, 100))
            img = Image.alpha_composite(img, red_overlay)
        
        # Save to bytes
        buffer = BytesIO()
        img.save(buffer, "PNG")
        buffer.seek(0)
        
        return discord.File(buffer, filename=f"{effect}.png")
    
    @app_commands.command(name="pixelate", description="Pixelate a user's avatar")
    @app_commands.describe(user="User to pixelate (defaults to yourself)")
    async def pixelate(self, interaction: discord.Interaction, user: discord.Member = None):
        await interaction.response.defer()
        user = user or interaction.user
        file = await self.process_avatar(user, "pixelate")
        await interaction.followup.send(file=file)
    
    @app_commands.command(name="blur", description="Blur a user's avatar")
    @app_commands.describe(user="User to blur (defaults to yourself)")
    async def blur(self, interaction: discord.Interaction, user: discord.Member = None):
        await interaction.response.defer()
        user = user or interaction.user
        file = await self.process_avatar(user, "blur")
        await interaction.followup.send(file=file)
    
    @app_commands.command(name="invert", description="Invert a user's avatar colors")
    @app_commands.describe(user="User to invert (defaults to yourself)")
    async def invert(self, interaction: discord.Interaction, user: discord.Member = None):
        await interaction.response.defer()
        user = user or interaction.user
        file = await self.process_avatar(user, "invert")
        await interaction.followup.send(file=file)
    
    @app_commands.command(name="grayscale", description="Make a user's avatar grayscale")
    @app_commands.describe(user="User (defaults to yourself)")
    async def grayscale(self, interaction: discord.Interaction, user: discord.Member = None):
        await interaction.response.defer()
        user = user or interaction.user
        file = await self.process_avatar(user, "grayscale")
        await interaction.followup.send(file=file)
    
    @app_commands.command(name="deepfry", description="Deep fry a user's avatar")
    @app_commands.describe(user="User to deep fry (defaults to yourself)")
    async def deepfry(self, interaction: discord.Interaction, user: discord.Member = None):
        await interaction.response.defer()
        user = user or interaction.user
        file = await self.process_avatar(user, "deepfry")
        await interaction.followup.send(file=file)
    
    @app_commands.command(name="frame", description="Add a frame to a user's avatar")
    @app_commands.describe(user="User (defaults to yourself)")
    async def frame(self, interaction: discord.Interaction, user: discord.Member = None):
        await interaction.response.defer()
        user = user or interaction.user
        file = await self.process_avatar(user, "frame")
        await interaction.followup.send(file=file)
    
    @app_commands.command(name="wanted", description="Create a wanted poster")
    @app_commands.describe(user="User (defaults to yourself)")
    async def wanted(self, interaction: discord.Interaction, user: discord.Member = None):
        await interaction.response.defer()
        user = user or interaction.user
        file = await self.process_avatar(user, "wanted")
        await interaction.followup.send(file=file)
    
    @app_commands.command(name="trigger", description="Create a triggered effect")
    @app_commands.describe(user="User (defaults to yourself)")
    async def trigger(self, interaction: discord.Interaction, user: discord.Member = None):
        await interaction.response.defer()
        user = user or interaction.user
        file = await self.process_avatar(user, "trigger")
        await interaction.followup.send(file=file)
    
    # ========== SAY ==========
    @app_commands.command(name="say", description="Make the bot say something")
    @app_commands.describe(message="Message to send")
    async def say(self, interaction: discord.Interaction, message: str):
        # Check if user has manage messages permission
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("❌ You need Manage Messages permission to use this.", ephemeral=True)
            return
        
        await interaction.response.send_message("✅ Message sent!", ephemeral=True)
        await interaction.channel.send(message)

async def setup(bot):
    await bot.add_cog(Utility(bot))
