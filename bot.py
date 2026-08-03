###############################################
#                IMPORTS & SETUP             #
###############################################

import os
import json
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
# {
#   "display_name": str | None,
#   "account_id": str | None,
#   "locked": bool,
#   "last_match_id": str | None
# }
user_db = {}
# In-memory guild config: {guild_id: {"win_channel_id": int}}
guild_config = {}

API_BASE = "https://fortnite-api.com"


###############################################
#           JSON PERSISTENCE HELPERS         #
###############################################

def save_db():
    data = {
        "users": user_db,
        "guilds": guild_config
    }
    try:
        with open("database.json", "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def load_db():
    global user_db, guild_config
    if not os.path.exists("database.json"):
        return
    try:
        with open("database.json", "r") as f:
            data = json.load(f)
            user_db = data.get("users", {})
            guild_config = data.get("guilds", {})
    except Exception:
        user_db = {}
        guild_config = {}


load_db()


###############################################
#           FORTNITE API FUNCTIONS (2026)    #
###############################################

def api_headers():
    return {
        "Authorization": f"Bearer {FORTNITE_API_KEY}"
    }


def get_account_id_from_username(username: str):
    """Resolve Epic account ID from display name (2026 endpoint)."""
    url = f"{API_BASE}/v2/account?username={username}"
    response = requests.get(url, headers=api_headers())

    if response.status_code != 200:
        return None

    data = response.json()
    if data.get("status") != 200:
        return None

    return data["data"]["id"]


def get_display_name_from_account(account_id: str):
    """Fetch Epic display name from account ID."""
    url = f"{API_BASE}/v2/account/{account_id}"
    response = requests.get(url, headers=api_headers())

    if response.status_code != 200:
        return None

    data = response.json()
    if data.get("status") != 200:
        return None

    return data["data"]["name"]


def lookup_fortnite_user_by_account(account_id: str):
    """Fetch BR stats using account ID (2026)."""
    url = f"{API_BASE}/v2/stats/br/v2?account={account_id}"
    response = requests.get(url, headers=api_headers())

    if response.status_code != 200:
        return None

    data = response.json()
    if data.get("status") != 200:
        return None

    return data


def get_recent_matches(account_id: str):
    """Fetch recent matches for a user by account ID."""
    url = f"{API_BASE}/v2/matches?account={account_id}"
    response = requests.get(url, headers=api_headers())

    if response.status_code != 200:
        return None

    data = response.json()
    if data.get("status") != 200:
        return None

    return data.get("data", [])


def format_mode_title(mode_id: str) -> str:
    mode_map = {
        "battle_royale": "Battle Royale",
        "battle_royale_zero_build": "Battle Royale Zero Build",
        "battle_royale_og": "OG Battle Royale",
        "battle_royale_reload": "Reload",
    }
    return mode_map.get(mode_id, "Battle Royale")


def format_playlist_name(playlist_id: str) -> str:
    pid = (playlist_id or "").lower()

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
    description="Link your Fortnite account using display name or account ID."
)
@app_commands.describe(user_input="Your Epic display name OR your Epic Account ID")
async def fortniteuser(interaction: discord.Interaction, user_input: str):

    await interaction.response.defer(ephemeral=True)
    discord_id = str(interaction.user.id)

    if discord_id in user_db and user_db[discord_id].get("locked"):
        return await interaction.followup.send(
            "You already linked your Fortnite account. Staff can reset it if needed."
        )

    is_account_id = len(user_input) >= 32 and user_input.isalnum()

    # CASE 1: Account ID
    if is_account_id:
        account_id = user_input

        stats_data = lookup_fortnite_user_by_account(account_id)
        if stats_data is None:
            return await interaction.followup.send(
                "This Account ID has no Battle Royale stats yet. "
                "Play one BR match and try again."
            )

        display_name = get_display_name_from_account(account_id)
        if display_name is None:
            return await interaction.followup.send(
                "I validated your Account ID, but couldn't fetch your display name."
            )

        user_db[discord_id] = {
            "display_name": display_name,
            "account_id": account_id,
            "locked": True,
            "last_match_id": None
        }
        save_db()

        return await interaction.followup.send(
            f"Your Fortnite account has been linked!\n"
            f"**Display Name:** {display_name}"
        )

    # CASE 2: Display name
    display_name = user_input

    account_id = get_account_id_from_username(display_name)
    if account_id is None:
        return await interaction.followup.send(
            "I couldn't find your Epic account. "
            "Double-check your display name or paste your Account ID."
        )

    stats_data = lookup_fortnite_user_by_account(account_id)
    if stats_data is None:
        return await interaction.followup.send(
            "I found your account, but you have no BR stats yet. "
            "Play one BR match and try again."
        )

    display_name_resolved = get_display_name_from_account(account_id) or display_name

    user_db[discord_id] = {
        "display_name": display_name_resolved,
        "account_id": account_id,
        "locked": True,
        "last_match_id": None
    }
    save_db()

    return await interaction.followup.send(
        f"Your Fortnite account has been linked!\n"
        f"**Display Name:** {display_name_resolved}"
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

    if discord_id not in user_db or not user_db[discord_id].get("account_id"):
        return await interaction.response.send_message(
            f"{target.display_name} has not linked a Fortnite account.",
            ephemeral=True
        )

    display_name = user_db[discord_id]["display_name"]
    account_id = user_db[discord_id]["account_id"]

    stats_data = lookup_fortnite_user_by_account(account_id)
    if stats_data is None:
        return await interaction.response.send_message(
            "Could not fetch stats. Fortnite API may be down or stats are still hidden.",
            ephemeral=True
        )

    stats = stats_data["data"]["stats"]["all"]

    wins = stats.get("wins", 0)
    kills = stats.get("kills", 0)
    matches = stats.get("matches", 0)

    embed = discord.Embed(
        title=f"{display_name}'s Fortnite Stats",
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
    description="Staff: Reset a user's linked Fortnite account."
)
@app_commands.describe(member="The member to reset")
@app_commands.checks.has_permissions(manage_guild=True)
async def resetuser(interaction: discord.Interaction, member: discord.Member):

    discord_id = str(member.id)

    if discord_id not in user_db or not user_db[discord_id].get("account_id"):
        return await interaction.response.send_message(
            f"{member.display_name} does not have a Fortnite account linked.",
            ephemeral=True
        )

    user_db[discord_id] = {
        "display_name": None,
        "account_id": None,
        "locked": False,
        "last_match_id": None
    }
    save_db()

    await interaction.response.send_message(
        f"{member.display_name}'s Fortnite account link has been reset.",
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
    save_db()

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
        account_id = info.get("account_id")
        display_name = info.get("display_name")
        if not account_id:
            continue

        matches = get_recent_matches(account_id)
        if not matches:
            continue

        latest = matches[0]
        match_id = latest.get("id")

        if info.get("last_match_id") == match_id:
            continue

        result = latest.get("result")
        mode_id = latest.get("mode")
        playlist_id = latest.get("playlist")
        kills = latest.get("kills", 0)
        duration = latest.get("duration", 0)

        if result != "victory":
            user_db[discord_id]["last_match_id"] = match_id
            save_db()
            continue

        if mode_id not in [
            "battle_royale",
            "battle_royale_zero_build",
            "battle_royale_og",
            "battle_royale_reload"
        ]:
            user_db[discord_id]["last_match_id"] = match_id
            save_db()
            continue

        member = None
        for guild in bot.guilds:
            m = guild.get_member(int(discord_id))
            if m:
                member = m
                break

        if not member or not member.guild:
            user_db[discord_id]["last_match_id"] = match_id
            save_db()
            continue

        guild_id = str(member.guild.id)
        config = guild_config.get(guild_id)
        if not config or not config.get("win_channel_id"):
            user_db[discord_id]["last_match_id"] = match_id
            save_db()
            continue

        channel = member.guild.get_channel(config["win_channel_id"])
        if not channel:
            user_db[discord_id]["last_match_id"] = match_id
            save_db()
            continue

        mode_title = format_mode_title(mode_id)
        playlist_name = format_playlist_name(playlist_id or "")
        time_str = format_duration(duration or 0)

        embed = discord.Embed(
            title=f"🏆 {mode_title}",
            color=member.color if member.color.value != 0 else discord.Color.gold()
        )
        embed.description = f"**{display_name}** just won a match!"
        embed.add_field(name="Kills", value=str(kills), inline=False)
        embed.add_field(name="Playlist", value=playlist_name, inline=False)
        embed.add_field(name="Time", value=time_str, inline=False)

        try:
            await channel.send(embed=embed)
        except Exception:
            pass

        user_db[discord_id]["last_match_id"] = match_id
        save_db()


@check_wins.before_loop
async def before_check_wins():
    await bot.wait_until_ready()


###############################################
#                BOT RUNNER                  #
###############################################

bot.run(TOKEN)
