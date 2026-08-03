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
#   "last_wins": int | None,
#   "last_kills": int | None
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
# Real, documented endpoints only:
#   GET /v2/stats/br/v2?name=...            -> resolve name + fetch stats in one call
#   GET /v2/stats/br/v2/{accountId}         -> fetch stats by account ID
# There is no separate "account lookup" endpoint and no public match-history
# endpoint on fortnite-api.com — stats are the only source of truth we have,
# so wins are detected by diffing the stats counter over time.

def api_headers():
    return {
        "Authorization": FORTNITE_API_KEY
    }


def lookup_stats_by_name(name: str):
    """Resolve a display name AND fetch stats in a single call.
    Returns the 'data' object (contains data['account'] and data['stats']), or None.
    """
    url = f"{API_BASE}/v2/stats/br/v2"
    try:
        response = requests.get(
            url,
            headers=api_headers(),
            params={"name": name, "accountType": "epic", "timeWindow": "lifetime"},
            timeout=10,
        )
    except requests.RequestException:
        return None

    if response.status_code != 200:
        return None

    payload = response.json()
    if payload.get("status") != 200:
        return None

    return payload.get("data")


def lookup_stats_by_account_id(account_id: str):
    """Fetch stats directly by account ID. Returns the 'data' object, or None."""
    url = f"{API_BASE}/v2/stats/br/v2/{account_id}"
    try:
        response = requests.get(
            url,
            headers=api_headers(),
            params={"timeWindow": "lifetime"},
            timeout=10,
        )
    except requests.RequestException:
        return None

    if response.status_code != 200:
        return None

    payload = response.json()
    if payload.get("status") != 200:
        return None

    return payload.get("data")


def extract_all_stats(stats_data: dict):
    """Pull wins/kills/matches out of a stats 'data' object, defaulting to 0."""
    stats = (stats_data or {}).get("stats", {}).get("all", {}).get("overall", {})
    return {
        "wins": stats.get("wins", 0),
        "kills": stats.get("kills", 0),
        "matches": stats.get("matches", 0),
    }


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

    if is_account_id:
        stats_data = lookup_stats_by_account_id(user_input)
        if stats_data is None:
            return await interaction.followup.send(
                "I couldn't find stats for that Account ID. Make sure it's correct "
                "and that you've played at least one BR match."
            )
        account_id = stats_data["account"]["id"]
        display_name = stats_data["account"]["name"]
    else:
        stats_data = lookup_stats_by_name(user_input)
        if stats_data is None:
            return await interaction.followup.send(
                "I couldn't find that Epic account, or it has no BR stats yet. "
                "Double-check the spelling (it's case- and character-sensitive), "
                "or paste your Account ID instead."
            )
        account_id = stats_data["account"]["id"]
        display_name = stats_data["account"]["name"]

    totals = extract_all_stats(stats_data)

    user_db[discord_id] = {
        "display_name": display_name,
        "account_id": account_id,
        "locked": True,
        "last_wins": totals["wins"],
        "last_kills": totals["kills"],
    }
    save_db()

    return await interaction.followup.send(
        f"Your Fortnite account has been linked!\n"
        f"**Display Name:** {display_name}"
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

    stats_data = lookup_stats_by_account_id(account_id)
    if stats_data is None:
        return await interaction.response.send_message(
            "Could not fetch stats. Fortnite API may be down or stats are still hidden.",
            ephemeral=True
        )

    totals = extract_all_stats(stats_data)

    embed = discord.Embed(
        title=f"{display_name}'s Fortnite Stats",
        color=discord.Color.blue()
    )
    embed.add_field(name="Wins", value=totals["wins"])
    embed.add_field(name="Kills", value=totals["kills"])
    embed.add_field(name="Games Played", value=totals["matches"])

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
        "last_wins": None,
        "last_kills": None,
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
# fortnite-api.com has no public match-history endpoint, so we can't get
# per-match details (playlist, duration, exact kill count for that game).
# Instead we poll each linked user's lifetime stats and detect a win by
# watching the "wins" counter go up. We report the kill delta since the
# last check as a reasonable stand-in for "kills that match."

@tasks.loop(seconds=120)
async def check_wins():
    for discord_id, info in list(user_db.items()):
        account_id = info.get("account_id")
        display_name = info.get("display_name")
        if not account_id:
            continue

        stats_data = lookup_stats_by_account_id(account_id)
        if stats_data is None:
            continue

        totals = extract_all_stats(stats_data)
        current_wins = totals["wins"]
        current_kills = totals["kills"]

        last_wins = info.get("last_wins")
        last_kills = info.get("last_kills")

        # First time we've ever seen this user's stats: just baseline, don't announce.
        if last_wins is None:
            user_db[discord_id]["last_wins"] = current_wins
            user_db[discord_id]["last_kills"] = current_kills
            save_db()
            continue

        if current_wins <= last_wins:
            continue  # no new win

        win_count = current_wins - last_wins
        kill_delta = max(current_kills - last_kills, 0)

        user_db[discord_id]["last_wins"] = current_wins
        user_db[discord_id]["last_kills"] = current_kills
        save_db()

        member = None
        for guild in bot.guilds:
            m = guild.get_member(int(discord_id))
            if m:
                member = m
                break

        if not member or not member.guild:
            continue

        guild_id = str(member.guild.id)
        config = guild_config.get(guild_id)
        if not config or not config.get("win_channel_id"):
            continue

        channel = member.guild.get_channel(config["win_channel_id"])
        if not channel:
            continue

        embed = discord.Embed(
            title="🏆 Victory Royale!",
            color=member.color if member.color.value != 0 else discord.Color.gold()
        )
        if win_count > 1:
            embed.description = f"**{display_name}** just won {win_count} matches!"
        else:
            embed.description = f"**{display_name}** just won a match!"
        embed.add_field(name="Kills (since last check)", value=str(kill_delta), inline=False)
        embed.add_field(name="Total Wins", value=str(current_wins), inline=False)

        try:
            await channel.send(embed=embed)
        except Exception:
            pass


@check_wins.before_loop
async def before_check_wins():
    await bot.wait_until_ready()


###############################################
#                BOT RUNNER                  #
###############################################

bot.run(TOKEN)
