###############################################
#                IMPORTS & SETUP             #
###############################################

import os
import json
import time
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
#   "last_kills": int | None,
#   "last_mode_wins": dict | None,   # {"solo": 10, "duo": 3, ...} snapshot for playlist detection
#   "last_win_ts": int | None,       # unix timestamp of the last detected win
#   "skin_name": str | None,         # cosmetic the member picked for their card
#   "skin_icon_url": str | None
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


def extract_mode_wins(stats_data: dict):
    """Return {mode_key: wins} for each per-playlist bucket (solo/duo/trio/squad),
    skipping the 'overall' aggregate. Used to figure out which playlist a new
    win came from, since the API doesn't expose individual match history.
    """
    all_stats = (stats_data or {}).get("stats", {}).get("all", {}) or {}
    modes = {}
    for key, value in all_stats.items():
        if key == "overall" or not isinstance(value, dict):
            continue
        modes[key] = value.get("wins", 0)
    return modes


PLAYLIST_LABELS = {
    "solo": "Solo",
    "duo": "Duo",
    "duos": "Duo",
    "trio": "Trio",
    "trios": "Trio",
    "squad": "Squad",
    "squads": "Squad",
}


def format_playlist_label(mode_key: str) -> str:
    return PLAYLIST_LABELS.get(mode_key.lower(), mode_key.title())


def search_outfit_icon(name: str):
    """Search the BR cosmetics catalog for an outfit and return (name, icon_url)
    for the best match, or None if nothing was found. No API key required.
    """
    url = f"{API_BASE}/v2/cosmetics/br/search/all"
    try:
        response = requests.get(
            url,
            params={"name": name, "matchMethod": "contains", "type": "outfit"},
            timeout=10,
        )
    except requests.RequestException:
        return None

    if response.status_code != 200:
        return None

    payload = response.json()
    if payload.get("status") != 200:
        return None

    results = payload.get("data") or []
    if not results:
        return None

    best = results[0]
    images = best.get("images", {}) or {}
    icon_url = images.get("icon") or images.get("smallIcon")
    if not icon_url:
        return None

    return best.get("name", name), icon_url


# Standard Discord "color circle" emoji, used to pick a title emoji that
# roughly matches a member's role color.
COLOR_EMOJIS = {
    (237, 66, 69): "🔴",     # red
    (230, 126, 34): "🟠",    # orange
    (241, 196, 15): "🟡",    # yellow
    (67, 181, 129): "🟢",    # green
    (52, 152, 219): "🔵",    # blue
    (155, 89, 182): "🟣",    # purple
    (121, 85, 72): "🟤",     # brown
    (35, 39, 42): "⚫",      # black / dark
    (255, 255, 255): "⚪",   # white
}


def color_to_emoji(color: discord.Color) -> str:
    if color is None or color.value == 0:
        return "⚪"
    r, g, b = color.r, color.g, color.b
    closest = min(
        COLOR_EMOJIS.items(),
        key=lambda kv: (kv[0][0] - r) ** 2 + (kv[0][1] - g) ** 2 + (kv[0][2] - b) ** 2,
    )
    return closest[1]


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
    mode_wins = extract_mode_wins(stats_data)

    user_db[discord_id] = {
        "display_name": display_name,
        "account_id": account_id,
        "locked": True,
        "last_wins": totals["wins"],
        "last_kills": totals["kills"],
        "last_mode_wins": mode_wins,
        "last_win_ts": None,
        "skin_name": None,
        "skin_icon_url": None,
    }
    save_db()

    return await interaction.followup.send(
        f"Your Fortnite account has been linked!\n"
        f"**Display Name:** {display_name}\n\n"
        f"Tip: run `/fortniteskin <outfit name>` to set a thumbnail for your `/fortnite` card."
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

    record = user_db[discord_id]
    display_name = record["display_name"]
    account_id = record["account_id"]

    stats_data = lookup_stats_by_account_id(account_id)
    if stats_data is None:
        return await interaction.response.send_message(
            "Could not fetch stats. Fortnite API may be down or stats are still hidden.",
            ephemeral=True
        )

    totals = extract_all_stats(stats_data)

    last_win_ts = record.get("last_win_ts")
    last_win_str = f"<t:{last_win_ts}:f>" if last_win_ts else "Not tracked yet"

    role_color = target.color if target.color.value != 0 else discord.Color.blurple()
    dot = color_to_emoji(target.color)

    embed = discord.Embed(
        description=(
            f"**{dot} {display_name}**\n"
            f"-# 🏆 Wins: {totals['wins']}\n"
            f"-#  🔫 Kills: {totals['kills']}\n"
            f"-# 🕹️ Played: {totals['matches']}\n"
            f"-# ⏳ Last Win: {last_win_str}"
        ),
        color=role_color,
    )

    skin_icon_url = record.get("skin_icon_url")
    if skin_icon_url:
        embed.set_thumbnail(url=skin_icon_url)

    await interaction.response.send_message(embed=embed)


###############################################
#          /fortniteskin COMMAND             #
###############################################
# There's no public API for "what skin is this account currently wearing" —
# that data lives inside Epic's private, authenticated game session and
# isn't exposed anywhere. Instead, members pick a favorite outfit from the
# public cosmetics catalog and we pin its icon to their stats card.

@bot.tree.command(
    name="fortniteskin",
    description="Pick an outfit icon to show on your /fortnite stats card."
)
@app_commands.describe(outfit_name="Search for an outfit by name, e.g. 'Peely'")
async def fortniteskin(interaction: discord.Interaction, outfit_name: str):

    await interaction.response.defer(ephemeral=True)
    discord_id = str(interaction.user.id)

    if discord_id not in user_db or not user_db[discord_id].get("account_id"):
        return await interaction.followup.send(
            "Link your Fortnite account first with `/fortniteuser`."
        )

    result = search_outfit_icon(outfit_name)
    if result is None:
        return await interaction.followup.send(
            f"Couldn't find an outfit matching **{outfit_name}**. Try a different spelling."
        )

    matched_name, icon_url = result
    user_db[discord_id]["skin_name"] = matched_name
    user_db[discord_id]["skin_icon_url"] = icon_url
    save_db()

    preview = discord.Embed(
        description=f"Thumbnail set to **{matched_name}**",
        color=discord.Color.blurple(),
    )
    preview.set_thumbnail(url=icon_url)

    await interaction.followup.send(embed=preview)


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
        "last_mode_wins": None,
        "last_win_ts": None,
        "skin_name": None,
        "skin_icon_url": None,
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
        try:
            await check_wins_for_user(discord_id, info)
        except Exception as e:
            # Never let one user's bad data or a flaky API response kill the
            # whole loop for everyone else.
            print(f"[check_wins] error processing {discord_id}: {e}")
            continue


@check_wins.error
async def check_wins_error(error):
    # Belt-and-suspenders: if something still slips past the try/except
    # above and the loop dies, log it and restart it instead of going silent.
    print(f"[check_wins] loop crashed, restarting: {error}")
    if not check_wins.is_running():
        check_wins.start()


async def check_wins_for_user(discord_id, info):
    account_id = info.get("account_id")
    display_name = info.get("display_name")
    if not account_id:
        return

    stats_data = lookup_stats_by_account_id(account_id)
    if stats_data is None:
        return

    totals = extract_all_stats(stats_data)
    current_wins = totals["wins"]
    current_kills = totals["kills"]
    current_mode_wins = extract_mode_wins(stats_data)

    last_wins = info.get("last_wins")
    last_kills = info.get("last_kills")
    last_mode_wins = info.get("last_mode_wins") or {}

    # First time we've ever seen this user's stats: just baseline, don't announce.
    if last_wins is None:
        user_db[discord_id]["last_wins"] = current_wins
        user_db[discord_id]["last_kills"] = current_kills
        user_db[discord_id]["last_mode_wins"] = current_mode_wins
        save_db()
        return

    if current_wins <= last_wins:
        # Still update the per-mode snapshot so future playlist detection stays accurate.
        user_db[discord_id]["last_mode_wins"] = current_mode_wins
        save_db()
        return

    win_count = current_wins - last_wins
    kill_delta = max(current_kills - last_kills, 0)
    win_ts = int(time.time())

    # Figure out which playlist the win came from by seeing whose bucket grew the most.
    increased = [
        (mode, wins - last_mode_wins.get(mode, wins))
        for mode, wins in current_mode_wins.items()
        if wins > last_mode_wins.get(mode, wins)
    ]
    if increased:
        increased.sort(key=lambda pair: pair[1], reverse=True)
        playlist_label = format_playlist_label(increased[0][0])
    else:
        playlist_label = "Unknown"

    user_db[discord_id]["last_wins"] = current_wins
    user_db[discord_id]["last_kills"] = current_kills
    user_db[discord_id]["last_mode_wins"] = current_mode_wins
    user_db[discord_id]["last_win_ts"] = win_ts
    save_db()

    member = None
    for guild in bot.guilds:
        try:
            m = await guild.fetch_member(int(discord_id))
        except discord.NotFound:
            continue
        except discord.HTTPException:
            continue
        if m:
            member = m
            break

    if not member or not member.guild:
        return

    guild_id = str(member.guild.id)
    config = guild_config.get(guild_id)
    if not config or not config.get("win_channel_id"):
        return

    channel = member.guild.get_channel(config["win_channel_id"])
    if not channel:
        return

    dot = color_to_emoji(member.color)
    win_word = f"{win_count} matches" if win_count > 1 else "a match"

    embed = discord.Embed(
        description=(
            f"# {dot} Victory Royale!\n"
            f"**{display_name}** just won {win_word}\n"
            f"-# 🔫 Playlist: {playlist_label}\n"
            f"-# 🏆 Total Wins: {current_wins}"
        ),
        color=member.color if member.color.value != 0 else discord.Color.gold(),
    )
    skin_icon_url = user_db[discord_id].get("skin_icon_url")
    if skin_icon_url:
        embed.set_thumbnail(url=skin_icon_url)

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
