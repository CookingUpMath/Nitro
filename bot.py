import discord
from discord import app_commands
from discord.ext import commands
import requests
import os

TOKEN = "YOUR_DISCORD_BOT_TOKEN"
FORTNITE_API_KEY = "YOUR_FORTNITE_API_KEY"

# Temporary in-memory database (replace with real DB later)
user_db = {}

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# Fortnite API lookup
def lookup_fortnite_user(username):
    url = f"https://fortnite-api.com/v2/stats/br/v2?name={username}"
    headers = {"Authorization": FORTNITE_API_KEY}

    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        return None

    data = response.json()

    # API returns {"status":404} if user doesn't exist
    if data.get("status") == 404:
        return None

    return data


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")


# /fortniteuser command
@bot.tree.command(name="fortniteuser", description="Set your Fortnite username.")
@app_commands.describe(username="Your Fortnite username")
async def fortniteuser(interaction: discord.Interaction, username: str):

    discord_id = str(interaction.user.id)

    # Check if user already set a username
    if discord_id in user_db and user_db[discord_id]["locked"]:
        await interaction.response.send_message(
            "You already set your Fortnite username. Contact staff to reset it.",
            ephemeral=True
        )
        return

    # Validate username via API
    data = lookup_fortnite_user(username)
    if data is None:
        await interaction.response.send_message(
            "That Fortnite username does not exist. Double-check your spelling.",
            ephemeral=True
        )
        return

    # Save user
    user_db[discord_id] = {
        "username": username,
        "locked": True
    }

    await interaction.response.send_message(
        f"Your Fortnite username has been set to **{username}** and is now locked.",
        ephemeral=True
    )


# /fortnite command
@bot.tree.command(name="fortnite", description="View Fortnite stats.")
@app_commands.describe(user="Mention a user to view their stats")
async def fortnite(interaction: discord.Interaction, user: discord.Member = None):

    target = user or interaction.user
    discord_id = str(target.id)

    # Check if user has a saved username
    if discord_id not in user_db:
        await interaction.response.send_message(
            f"{target.display_name} has not set a Fortnite username.",
            ephemeral=True
        )
        return

    username = user_db[discord_id]["username"]

    # Fetch stats
    data = lookup_fortnite_user(username)
    if data is None:
        await interaction.response.send_message(
            "Could not fetch stats. Fortnite API may be down.",
            ephemeral=True
        )
        return

    stats = data["data"]["stats"]

    wins = stats["all"]["wins"]
    kills = stats["all"]["kills"]
    matches = stats["all"]["matches"]

    embed = discord.Embed(
        title=f"{username}'s Fortnite Stats",
        color=discord.Color.blue()
    )
    embed.add_field(name="Wins", value=wins)
    embed.add_field(name="Kills", value=kills)
    embed.add_field(name="Games Played", value=matches)

    await interaction.response.send_message(embed=embed)


bot.run(TOKEN)
