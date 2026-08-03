###############################################
#                IMPORTS & SETUP             #
###############################################

import os
import discord
from discord import app_commands
from discord.ext import commands, tasks
import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
FORTNITE_API_KEY = os.getenv("FORTNITE_API_KEY")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# In-memory user database: {discord_id: {...}}
user_db = {}
# In-memory guild config: {guild_id: {"win_channel_id": int}}
guild_config = {}


###############################################
#           FORTNITE API FUNCTIONS           #
###############################################

API_BASE = "https://fortnite-api.com"


def lookup_fortnite_user(username: str):
    """Validate Fortnite username and return stats if real."""
    url = f"{API_BASE}/v2/stats/br/v2?name={username}"
    headers = {"Authorization": FORTNITE_API_KEY}

    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        return None

    data = response.json()
    if data.get("status") == 404:
        return None

    return data


def get_recent_matches(username: str):
    """Fetch recent matches for a user."""
    url = f"{API_BASE}/v2/matches?name={username}"
    headers = {"Authorization": FORTNITE_API_KEY}

    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        return None

    data = response.json()
    if data.get("status") == 404:
        return None

    return data.get("data", [])


def format_mode_title(mode_id: str) -> str:
    """Convert mode ID to human-readable title."""
    mode_map = {
        "battle_royale": "Battle Royale",
        "battle_royale_zero_build": "Battle Royale Zero Build",
        "battle_royale_og": "OG Battle Royale",
        "battle_royale_reload": "Reload",
    }
    return mode_map.get(mode_id, "Battle Royale")


def format_playlist_name(playlist_id: str) -> str:
    """Convert playlist ID to Solo/Duos/Trios/Squads."""
    pid = playlist_id.lower()

    if "solo" in pid:
        return "Solo"
    if "duo" in pid:
        return "Duos"
    if "trio" in pid:
        return "Trios"
    if "squad" in pid:
        return "Squads"

    return "Unknown"


def format_duration(seconds: int) -> str:
    """Format duration in MM:SS."""
    minutes = seconds // 60
    secs = seconds % 60
    return f"{minutes:02d}:{secs:02d}"


###############################################
#                BOT READY EVENT             #
###############################################

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Bot is online as {bot.user}")
    check_wins.start()


###############################################
#          /fortniteuser COMMAND             #
###############################################

@bot.tree.command(
    name="fortniteuser",
    description="Set your Fortnite username (locked after setting)."
)
@app_commands.describe(username="Your Fortnite username")
async def fortniteuser(interaction: discord.Interaction, username: str):

    discord_id = str(interaction.user.id)

    if discord_id in user_db and user_db[discord_id]["locked"]:
        await interaction.response.send_message(
            "You already set your Fortnite username. Contact staff to reset it.",
            ephemeral=True
        )
        return

    data = lookup_fortnite_user(username)
    if data is None:
        await interaction.response.send_message(
            "That Fortnite username does not exist. Double-check your spelling.",
            ephemeral=True
        )
        return

    user_db[discord_id] = {
        "username": username,
        "locked": True,
        "last_match_id": None
    }

    await interaction.response.send_message(
        f"Your Fortnite username has been set to **{username}** and is now locked.",
        ephemeral=True
    )


###############################################
#             /fortnite COMMAND              #
###############################################

@bot.tree.command(
    name="fortnite",
    description="View Fortnite stats for yourself or another user."
)
@app_commands.describe(user="Mention a user to view their stats")
async def fortnite(interaction: discord.Interaction, user: discord.Member = None):

    target = user or interaction.user
    discord_id = str(target.id)

    if discord_id not in user_db or not user_db[discord_id]["username"]:
        await interaction.response.send_message(
            f"{target.display_name} has not set a Fortnite username.",
            ephemeral=True
        )
        return

    username = user_db[discord_id]["username"]

    data = lookup_fortnite_user(username)
    if data is None:
        await interaction.response.send_message(
            "Could not fetch stats. Fortnite API may be down.",
            ephemeral=True
        )
        return

    stats = data["data"]["stats"]["all"]

    wins = stats["wins"]
    kills = stats["kills"]
    matches = stats["matches"]

    embed = discord.Embed(
        title=f"{username}'s Fortnite Stats",
        color=discord.Color.blue()
    )
    embed.add_field(name="Wins", value=wins)
    embed.add_field(name="Kills", value=kills)
    embed.add_field(name="Games Played", value=matches)

    await interaction.response.send_message(embed=embed)


###############################################
#            /resetuser COMMAND              #
###############################################

@bot.tree.command(
    name="resetuser",
    description="Staff: Reset a user's Fortnite username."
)
@app_commands.describe(member="The member to reset")
@app_commands.checks.has_permissions(manage_guild=True)
async def resetuser(interaction: discord.Interaction, member: discord.Member):

    discord_id = str(member.id)

    if discord_id not in user_db or not user_db[discord_id]["username"]:
        await interaction.response.send_message(
            f"{member.display_name} does not have a Fortnite username set.",
            ephemeral=True
        )
        return

    user_db[discord_id] = {
        "username": None,
        "locked": False,
        "last_match_id": None
    }

    await interaction.response.send_message(
        f"{member.display_name}'s Fortnite username has been reset.",
        ephemeral=True
    )


@resetuser.error
async def resetuser_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "You do not have permission to use this command.",
            ephemeral=True
        )


###############################################
#          /setwinchannel COMMAND            #
###############################################

@bot.tree.command(
    name="setwinchannel",
    description="Set the channel where win messages will be posted."
)
@app_commands.describe(channel="Channel for win announcements")
@app_commands.checks.has_permissions(manage_guild=True)
async def setwinchannel(interaction: discord.Interaction, channel: discord.TextChannel):

    guild_id = str(interaction.guild.id)

    guild_config[guild_id] = {
        "win_channel_id": channel.id
    }

    await interaction.response.send_message(
        f"Win announcements will be posted in {channel.mention}.",
        ephemeral=True
    )


@setwinchannel.error
async def setwinchannel_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "You do not have permission to use this command.",
            ephemeral=True
        )


###############################################
#           WIN DETECTION BACKGROUND         #
###############################################

@tasks.loop(seconds=120)
async def check_wins():
    """Check for new wins every 120 seconds and post them."""
    for discord_id, info in user_db.items():
        username = info.get("username")
        if not username:
            continue

        matches = get_recent_matches(username)
        if not matches:
            continue

        latest = matches[0]
        match_id = latest.get("id")

        # Avoid duplicate announcements
        if info.get("last_match_id") == match_id:
            continue

        result = latest.get("result")
        mode_id = latest.get("mode")
        playlist_id = latest.get("playlist")
        kills = latest.get("kills", 0)
        duration = latest.get("duration", 0)

        # Only announce wins in official modes
        if result != "victory":
            user_db[discord_id]["last_match_id"] = match_id
            continue

        if mode_id not in [
            "battle_royale",
            "battle_royale_zero_build",
            "battle_royale_og",
            "battle_royale_reload"
        ]:
            user_db[discord_id]["last_match_id"] = match_id
            continue

        # Find member and guilds
        member = None
        for guild in bot.guilds:
            m = guild.get_member(int(discord_id))
            if m:
                member = m
                break

        if not member or not member.guild:
            user_db[discord_id]["last_match_id"] = match_id
            continue

        guild_id = str(member.guild.id)
        config = guild_config.get(guild_id)
        if not config or not config.get("win_channel_id"):
            user_db[discord_id]["last_match_id"] = match_id
            continue

        channel = member.guild.get_channel(config["win_channel_id"])
        if not channel:
            user_db[discord_id]["last_match_id"] = match_id
            continue

        # Build embed
        mode_title = format_mode_title(mode_id)
        playlist_name = format_playlist_name(playlist_id or "")
        time_str = format_duration(duration or 0)

        embed = discord.Embed(
            title=f"🏆 {mode_title}",
            color=member.color if member.color.value != 0 else discord.Color.gold()
        )
        embed.description = f"**{username}** just won a match!"
        embed.add_field(name="Kills", value=str(kills), inline=False)
        embed.add_field(name="Playlist", value=playlist_name, inline=False)
        embed.add_field(name="Time", value=time_str, inline=False)

        try:
            await channel.send(embed=embed)
        except Exception:
            pass

        user_db[discord_id]["last_match_id"] = match_id


@check_wins.before_loop
async def before_check_wins():
    await bot.wait_until_ready()


###############################################
#                BOT RUNNER                  #
###############################################

bot.run(TOKEN)
