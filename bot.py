###############################################
#                IMPORTS & SETUP             #
###############################################

import os
import sys
import json
import time
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

# Railway (and most container platforms) buffer stdout by default, which can
# make print() output show up late or not at all in the log viewer. Force
# line-buffering so every print() is visible immediately.
sys.stdout.reconfigure(line_buffering=True)

import discord
from discord import app_commands
from discord.ext import commands, tasks
import requests
import asyncpg
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
FORTNITE_API_KEY = os.getenv("FORTNITE_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# In-memory caches, backed by Postgres. Shape of each value:
#
# user_db[discord_id] = {
#   "guild_id": str,                 # the server they linked from (single-server assumption)
#   "display_name": str | None,
#   "account_id": str | None,
#   "locked": bool,
#   "last_wins": int | None,         # lifetime total, refreshed every poll
#   "last_kills": int | None,        # lifetime total, refreshed every poll
#   "last_mode_wins": dict | None,   # {"solo": 10, "duo": 3, ...} snapshot for playlist detection
#   "last_win_ts": int | None,       # unix timestamp of the last detected win
#   "weekly_baseline_wins": int,     # lifetime wins as of the most recent Friday reset
#   "weekly_baseline_kills": int,    # lifetime kills as of the most recent Friday reset
#   "skin_name": str | None,         # cosmetic the member picked for their card
#   "skin_icon_url": str | None
# }
user_db = {}

# guild_config[guild_id] = {
#   "win_channel_id": int | None,
#   "fortboard_role_id": int | None,
#   "fortboard_role_holders": [discord_id, ...],  # who currently holds the weekly role
#   "linked_role_id": int | None                  # granted on link, removed on /resetuser
# }
guild_config = {}

# weekly_state = {"last_reset_week": "2026-W31"}  # guards against double-firing the reset
weekly_state = {}

API_BASE = "https://fortnite-api.com"
ET = ZoneInfo("America/New_York")


###############################################
#           POSTGRES PERSISTENCE             #
###############################################
# Simple key/value table — one JSONB blob per top-level piece of state.
# This keeps the rest of the bot's code (which reads/writes user_db and
# guild_config as plain dicts) essentially unchanged from the JSON-file
# version; only save_db()/load_db() know Postgres exists.

db_pool = None


async def init_db_pool():
    global db_pool
    if db_pool is not None:
        return
    if not DATABASE_URL:
        print("[db] DATABASE_URL is not set — the bot will not remember anything between restarts.")
        return
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value JSONB NOT NULL
            )
            """
        )


async def save_db() -> bool:
    if db_pool is None:
        print("[db] save skipped — no database connection")
        return False
    try:
        async with db_pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO bot_state (key, value) VALUES ($1, $2::jsonb)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                [
                    ("users", json.dumps(user_db)),
                    ("guilds", json.dumps(guild_config)),
                    ("weekly", json.dumps(weekly_state)),
                ],
            )
        return True
    except Exception as e:
        print(f"[db] save failed: {e}")
        return False


async def load_db():
    global user_db, guild_config, weekly_state
    if db_pool is None:
        return
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM bot_state")
            data = {row["key"]: json.loads(row["value"]) for row in rows}
            user_db = data.get("users", {})
            guild_config = data.get("guilds", {})
            weekly_state = data.get("weekly", {})
    except Exception as e:
        print(f"[db] load failed: {e}")
        user_db = {}
        guild_config = {}
        weekly_state = {}


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


async def lookup_stats_by_name(name: str):
    """Resolve a display name AND fetch stats in a single call.
    Returns the 'data' object (contains data['account'] and data['stats']), or None.
    Runs the blocking HTTP call in a worker thread so it never freezes the
    bot's event loop while waiting on a slow API response.
    """
    url = f"{API_BASE}/v2/stats/br/v2"
    try:
        response = await asyncio.to_thread(
            requests.get,
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


async def lookup_stats_by_account_id(account_id: str):
    """Fetch stats directly by account ID. Returns the 'data' object, or None."""
    url = f"{API_BASE}/v2/stats/br/v2/{account_id}"
    try:
        response = await asyncio.to_thread(
            requests.get,
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
    skipping the 'overall' aggregate. Used both for playlist detection on new
    wins and for the per-mode breakdown on the /fortnite card.
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
PLAYLIST_ORDER = ["solo", "duo", "squad"]


def format_playlist_label(mode_key: str) -> str:
    return PLAYLIST_LABELS.get(mode_key.lower(), "Limited")


async def search_outfit_icon(name: str):
    """Search the BR cosmetics catalog for an outfit and return (name, icon_url)
    for the best match, or None if nothing was found. No API key required.
    """
    url = f"{API_BASE}/v2/cosmetics/br/search/all"
    try:
        response = await asyncio.to_thread(
            requests.get,
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


def weekly_wins_for(info: dict) -> int:
    baseline = info.get("weekly_baseline_wins")
    total = info.get("last_wins") or 0
    if baseline is None:
        return 0
    return max(total - baseline, 0)


def weekly_kills_for(info: dict) -> int:
    baseline = info.get("weekly_baseline_kills")
    total = info.get("last_kills") or 0
    if baseline is None:
        return 0
    return max(total - baseline, 0)


###############################################
#                BOT READY EVENT             #
###############################################

_startup_done = False


@bot.event
async def on_interaction(interaction: discord.Interaction):
    cmd_name = getattr(interaction.command, "name", None)
    print(f"[interaction] received — command={cmd_name}, user={interaction.user}, type={interaction.type}")


@bot.event
async def on_ready():
    global _startup_done
    if not _startup_done:
        print("[startup] connecting to database...")
        await init_db_pool()
        print("[startup] database connected, loading state...")
        await load_db()
        print(f"[startup] loaded {len(user_db)} user(s), {len(guild_config)} guild config(s)")
        _startup_done = True
    print("[startup] syncing slash commands...")
    synced = await bot.tree.sync()
    print(f"[startup] synced {len(synced)} command(s)")
    print(f"Bot is online as {bot.user}")
    print(f"[startup] Application ID: {bot.application_id}")
    if not check_wins.is_running():
        check_wins.start()
    if not weekly_reset_loop.is_running():
        weekly_reset_loop.start()


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
        stats_data = await lookup_stats_by_account_id(user_input)
        if stats_data is None:
            return await interaction.followup.send(
                "I couldn't find stats for that Account ID. Make sure it's correct "
                "and that you've played at least one BR match."
            )
        account_id = stats_data["account"]["id"]
        display_name = stats_data["account"]["name"]
    else:
        stats_data = await lookup_stats_by_name(user_input)
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
        "guild_id": str(interaction.guild.id) if interaction.guild else None,
        "display_name": display_name,
        "account_id": account_id,
        "locked": True,
        "last_wins": totals["wins"],
        "last_kills": totals["kills"],
        "last_mode_wins": mode_wins,
        "last_win_ts": None,
        "weekly_baseline_wins": totals["wins"],
        "weekly_baseline_kills": totals["kills"],
        "skin_name": None,
        "skin_icon_url": None,
    }
    saved = await save_db()
    if not saved:
        del user_db[discord_id]
        return await interaction.followup.send(
            "I found your account, but couldn't save the link due to a database error. "
            "Please try again in a moment — if it keeps failing, let staff know."
        )

    if interaction.guild:
        role_id = guild_config.get(str(interaction.guild.id), {}).get("linked_role_id")
        if role_id:
            role = interaction.guild.get_role(role_id)
            if role:
                try:
                    await interaction.user.add_roles(role)
                except Exception:
                    pass

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

    await interaction.response.defer()

    record = user_db[discord_id]
    display_name = record["display_name"]
    account_id = record["account_id"]

    stats_data = await lookup_stats_by_account_id(account_id)
    if stats_data is None:
        return await interaction.followup.send(
            "Could not fetch stats. This usually means **Public Game Stats** got turned off "
            "in Fortnite (Settings → Account and Privacy → Gameplay Privacy) — it needs to "
            "stay on for this bot to read your stats. Could also just be the Fortnite API "
            "being temporarily down.",
            ephemeral=True
        )

    totals = extract_all_stats(stats_data)
    mode_wins = extract_mode_wins(stats_data)

    last_win_ts = record.get("last_win_ts")
    last_win_str = f"<t:{last_win_ts}:R>" if last_win_ts else "Not tracked yet"

    role_color = target.color if target.color.value != 0 else discord.Color.blurple()
    dot = color_to_emoji(target.color)

    lines = [
        f"## {dot} {display_name}",
        f"🕹️ Played: {totals['matches']}",
        f"🔫 Kills: {totals['kills']}",
        f"⏳ Last win: {last_win_str}",
        f"🏆 Wins: {totals['wins']}",
    ]
    for mode in PLAYLIST_ORDER:
        wins = mode_wins.get(mode, 0)
        if wins > 0:
            lines.append(f"-# ▪️ {format_playlist_label(mode)}: {wins}")

    # Everything that isn't solo/duo/squad (trios, arena, LTMs, etc.) gets
    # bucketed together, since the API doesn't track most of it separately.
    core_wins = sum(mode_wins.get(mode, 0) for mode in PLAYLIST_ORDER)
    limited_wins = max(totals["wins"] - core_wins, 0)
    if limited_wins > 0:
        lines.append(f"-# ▪️ Limited: {limited_wins}")

    embed = discord.Embed(description="\n".join(lines), color=role_color)

    skin_icon_url = record.get("skin_icon_url")
    if skin_icon_url:
        embed.set_thumbnail(url=skin_icon_url)

    await interaction.followup.send(embed=embed)


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

    result = await search_outfit_icon(outfit_name)
    if result is None:
        return await interaction.followup.send(
            f"Couldn't find an outfit matching **{outfit_name}**. Try a different spelling."
        )

    matched_name, icon_url = result
    user_db[discord_id]["skin_name"] = matched_name
    user_db[discord_id]["skin_icon_url"] = icon_url
    await save_db()

    preview = discord.Embed(
        description=f"Thumbnail set to **{matched_name}**",
        color=discord.Color.blurple(),
    )
    preview.set_thumbnail(url=icon_url)

    await interaction.followup.send(embed=preview)


###############################################
#             /fortboard COMMAND             #
###############################################

MEDALS = ["🥇", "🥈", "🥉"]


def format_leaderboard_section(title: str, rows: list) -> str:
    lines = [f"# 🏆 {title}"]
    if not rows:
        lines.append("-# No one has any yet this week.")
        return "\n".join(lines)
    for i, (display_name, value) in enumerate(rows):
        if i < 3:
            lines.append(f"{MEDALS[i]} - {display_name}: {value}")
        else:
            lines.append(f"-# ▪️ - {display_name}: {value}")
    return "\n".join(lines)


@bot.tree.command(
    name="fortboard",
    description="This week's Fortnite wins and kills leaderboard."
)
async def fortboard(interaction: discord.Interaction):

    if not interaction.guild:
        return await interaction.response.send_message(
            "This command only works inside a server.", ephemeral=True
        )

    guild_id = str(interaction.guild.id)

    entries = []
    for discord_id, info in user_db.items():
        if info.get("guild_id") != guild_id or not info.get("account_id"):
            continue
        entries.append({
            "display_name": info.get("display_name") or "Unknown",
            "wins": weekly_wins_for(info),
            "kills": weekly_kills_for(info),
        })

    top_wins = sorted(
        [(e["display_name"], e["wins"]) for e in entries if e["wins"] > 0],
        key=lambda pair: pair[1], reverse=True
    )[:10]
    top_kills = sorted(
        [(e["display_name"], e["kills"]) for e in entries if e["kills"] > 0],
        key=lambda pair: pair[1], reverse=True
    )[:10]

    description = (
        format_leaderboard_section("Wins", top_wins)
        + "\n\n"
        + format_leaderboard_section("Kills", top_kills)
    )

    embed = discord.Embed(description=description, color=discord.Color.gold())
    embed.set_footer(text="Resets every Friday at 12:00 AM ET")

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

    role_id = guild_config.get(str(interaction.guild.id), {}).get("linked_role_id")
    if role_id:
        role = interaction.guild.get_role(role_id)
        if role and role in member.roles:
            try:
                await member.remove_roles(role)
            except Exception:
                pass

    user_db[discord_id] = {
        "guild_id": None,
        "display_name": None,
        "account_id": None,
        "locked": False,
        "last_wins": None,
        "last_kills": None,
        "last_mode_wins": None,
        "last_win_ts": None,
        "weekly_baseline_wins": None,
        "weekly_baseline_kills": None,
        "skin_name": None,
        "skin_icon_url": None,
    }
    await save_db()

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
#              /settings COMMAND             #
###############################################
# One consolidated staff command with dropdown pickers instead of three
# separate slash commands. Each dropdown saves immediately on selection.

def build_settings_embed(guild_id: str) -> discord.Embed:
    config = guild_config.get(guild_id, {})

    win_channel_id = config.get("win_channel_id")
    fb_role_id = config.get("fortboard_role_id")
    linked_role_id = config.get("linked_role_id")

    win_channel_str = f"<#{win_channel_id}>" if win_channel_id else "*Not set*"
    fb_role_str = f"<@&{fb_role_id}>" if fb_role_id else "*Not set*"
    linked_role_str = f"<@&{linked_role_id}>" if linked_role_id else "*Not set*"

    embed = discord.Embed(
        title="⚙️ Fortnite Bot Settings",
        description=(
            f"🏆 **Win Announcement Channel:** {win_channel_str}\n"
            f"🥇 **Weekly Leaderboard Champion Role:** {fb_role_str}\n"
            f"🔗 **Linked Account Role:** {linked_role_str}"
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Pick an option below to update a setting.")
    return embed


class SettingsView(discord.ui.View):
    def __init__(self, guild_id: str):
        super().__init__(timeout=300)
        self.guild_id = guild_id

        self.win_channel_select = discord.ui.ChannelSelect(
            placeholder="🏆 Set win announcement channel",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
        )
        self.win_channel_select.callback = self.on_win_channel
        self.add_item(self.win_channel_select)

        self.fb_role_select = discord.ui.RoleSelect(
            placeholder="🥇 Set weekly leaderboard champion role",
            min_values=1,
            max_values=1,
        )
        self.fb_role_select.callback = self.on_fb_role
        self.add_item(self.fb_role_select)

        self.linked_role_select = discord.ui.RoleSelect(
            placeholder="🔗 Set linked account role",
            min_values=1,
            max_values=1,
        )
        self.linked_role_select.callback = self.on_linked_role
        self.add_item(self.linked_role_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "You do not have permission to change these settings.",
                ephemeral=True,
            )
            return False
        return True

    async def _refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=build_settings_embed(self.guild_id), view=self
        )

    async def on_win_channel(self, interaction: discord.Interaction):
        channel = self.win_channel_select.values[0]
        guild_config.setdefault(self.guild_id, {})["win_channel_id"] = channel.id
        await save_db()
        await self._refresh(interaction)

    async def on_fb_role(self, interaction: discord.Interaction):
        role = self.fb_role_select.values[0]
        guild_config.setdefault(self.guild_id, {})
        guild_config[self.guild_id]["fortboard_role_id"] = role.id
        guild_config[self.guild_id].setdefault("fortboard_role_holders", [])
        await save_db()
        await self._refresh(interaction)

    async def on_linked_role(self, interaction: discord.Interaction):
        role = self.linked_role_select.values[0]
        guild_config.setdefault(self.guild_id, {})["linked_role_id"] = role.id
        await save_db()
        await self._refresh(interaction)


@bot.tree.command(
    name="settings",
    description="Staff: configure the bot's channel and role settings."
)
@app_commands.checks.has_permissions(manage_guild=True)
async def settings_cmd(interaction: discord.Interaction):
    guild_id = str(interaction.guild.id)
    view = SettingsView(guild_id)
    embed = build_settings_embed(guild_id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@settings_cmd.error
async def settings_cmd_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "You do not have permission to use this command.",
            ephemeral=True
        )


###############################################
#          /fortnitedebug COMMAND            #
###############################################
# Staff-only diagnostic tool: shows exactly what's stored for a member,
# plus makes a fresh, direct API call using their stored account_id so you
# can see the raw status code and API response instead of just "it doesn't
# work" — separates a bad stored value from an Epic-side issue.

@bot.tree.command(
    name="fortnitedebug",
    description="Staff: inspect a member's stored Fortnite data and test the live API."
)
@app_commands.describe(member="The member to inspect")
@app_commands.checks.has_permissions(manage_guild=True)
async def fortnitedebug(interaction: discord.Interaction, member: discord.Member):

    await interaction.response.defer(ephemeral=True)
    discord_id = str(member.id)
    record = user_db.get(discord_id)

    if not record:
        return await interaction.followup.send(
            "No stored record at all for this member — they've never run `/fortniteuser`."
        )

    account_id = record.get("account_id")
    lines = [
        "**Stored data**",
        f"Discord ID: `{discord_id}`",
        f"Account ID: `{account_id}`",
        f"Display name: `{record.get('display_name')}`",
        f"Guild ID stored: `{record.get('guild_id')}`",
        f"Locked: `{record.get('locked')}`",
        f"Last wins/kills seen: `{record.get('last_wins')}` / `{record.get('last_kills')}`",
        "",
        "**Live API test (using the stored account_id)**",
    ]

    if not account_id:
        lines.append("No account_id stored — nothing to test against.")
    else:
        url = f"{API_BASE}/v2/stats/br/v2/{account_id}"
        try:
            response = await asyncio.to_thread(
                requests.get,
                url,
                headers=api_headers(),
                params={"timeWindow": "lifetime"},
                timeout=10,
            )
            lines.append(f"HTTP status code: `{response.status_code}`")
            try:
                payload = response.json()
                lines.append(f"API 'status' field: `{payload.get('status')}`")
                if payload.get("status") != 200:
                    lines.append(f"API error message: `{payload.get('error', 'none given')}`")
            except ValueError:
                lines.append("Response body was not valid JSON.")
        except requests.RequestException as e:
            lines.append(f"Request itself failed: `{e}`")

    await interaction.followup.send("\n".join(lines))


@fortnitedebug.error
async def fortnitedebug_error(interaction: discord.Interaction, error):
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
# watching the "wins" counter go up.

@tasks.loop(seconds=120)
async def check_wins():
    winners_this_cycle = []
    for discord_id, info in list(user_db.items()):
        try:
            winner_name = await check_wins_for_user(discord_id, info)
            if winner_name:
                winners_this_cycle.append(winner_name)
        except Exception as e:
            # Never let one user's bad data or a flaky API response kill the
            # whole loop for everyone else.
            print(f"[check_wins] error processing {discord_id}: {e}")
            continue

    if winners_this_cycle:
        await update_status(winners_this_cycle)


@check_wins.error
async def check_wins_error(error):
    # Belt-and-suspenders: if something still slips past the try/except
    # above and the loop dies, log it and restart it instead of going silent.
    print(f"[check_wins] loop crashed, restarting: {error}")
    if not check_wins.is_running():
        check_wins.start()


async def update_status(winner_names: list):
    names = list(dict.fromkeys(winner_names))  # de-dupe, keep order
    if len(names) == 1:
        status_text = f"🏆 {names[0]}"
    elif len(names) == 2:
        status_text = f"🏆 {names[0]} & {names[1]}"
    else:
        status_text = f"🏆 {', '.join(names[:-1])} & {names[-1]}"

    try:
        await bot.change_presence(activity=discord.CustomActivity(name=status_text))
    except Exception as e:
        print(f"[status] failed to update presence: {e}")


async def check_wins_for_user(discord_id, info):
    """Returns the display name if a new win was detected this poll, else None."""
    account_id = info.get("account_id")
    display_name = info.get("display_name")
    if not account_id:
        return None

    stats_data = await lookup_stats_by_account_id(account_id)
    if stats_data is None:
        return None

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
        await save_db()
        return None

    if current_wins <= last_wins:
        user_db[discord_id]["last_wins"] = current_wins
        user_db[discord_id]["last_kills"] = current_kills
        user_db[discord_id]["last_mode_wins"] = current_mode_wins
        await save_db()
        return None

    win_count = current_wins - last_wins
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
    await save_db()

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
        return display_name  # no Discord member found — fall back to the Epic name

    guild_id = str(member.guild.id)
    config = guild_config.get(guild_id)
    if not config or not config.get("win_channel_id"):
        return member.display_name

    channel = member.guild.get_channel(config["win_channel_id"])
    if not channel:
        return member.display_name

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

    return member.display_name


@check_wins.before_loop
async def before_check_wins():
    await bot.wait_until_ready()


###############################################
#         WEEKLY LEADERBOARD RESET           #
###############################################
# Fires once, at the first minute that's Friday 00:00 in America/New_York.
# Guarded by weekly_state["last_reset_week"] (an ISO year-week string) so a
# restart or a slightly-late tick can't trigger it twice for the same week.

@tasks.loop(minutes=1)
async def weekly_reset_loop():
    now = datetime.now(ET)
    if now.weekday() != 4 or now.hour != 0 or now.minute != 0:
        return

    current_week_id = now.strftime("%G-W%V")
    if weekly_state.get("last_reset_week") == current_week_id:
        return

    weekly_state["last_reset_week"] = current_week_id
    await run_weekly_reset()
    await save_db()


@weekly_reset_loop.before_loop
async def before_weekly_reset_loop():
    await bot.wait_until_ready()


@weekly_reset_loop.error
async def weekly_reset_loop_error(error):
    print(f"[weekly_reset] loop crashed, restarting: {error}")
    if not weekly_reset_loop.is_running():
        weekly_reset_loop.start()


async def run_weekly_reset():
    by_guild = {}
    for discord_id, info in user_db.items():
        gid = info.get("guild_id")
        if not gid or not info.get("account_id"):
            continue
        by_guild.setdefault(gid, []).append((discord_id, info))

    for guild_id, members in by_guild.items():
        config = guild_config.get(guild_id, {})
        role_id = config.get("fortboard_role_id")
        guild = bot.get_guild(int(guild_id))

        if guild and role_id:
            role = guild.get_role(role_id)
            if role:
                wins_ranked = sorted(members, key=lambda kv: weekly_wins_for(kv[1]), reverse=True)
                kills_ranked = sorted(members, key=lambda kv: weekly_kills_for(kv[1]), reverse=True)

                winners = set()
                if wins_ranked and weekly_wins_for(wins_ranked[0][1]) > 0:
                    winners.add(wins_ranked[0][0])
                if kills_ranked and weekly_kills_for(kills_ranked[0][1]) > 0:
                    winners.add(kills_ranked[0][0])

                previous_holders = config.get("fortboard_role_holders", [])
                for old_id in previous_holders:
                    if old_id in winners:
                        continue
                    try:
                        m = await guild.fetch_member(int(old_id))
                        await m.remove_roles(role)
                    except Exception:
                        pass

                for win_id in winners:
                    try:
                        m = await guild.fetch_member(int(win_id))
                        await m.add_roles(role)
                    except Exception:
                        pass

                guild_config.setdefault(guild_id, {})["fortboard_role_holders"] = list(winners)

    # Reset every linked user's weekly baseline, regardless of whether their
    # guild has a role configured.
    for discord_id, info in user_db.items():
        if info.get("last_wins") is None:
            continue
        info["weekly_baseline_wins"] = info.get("last_wins", 0)
        info["weekly_baseline_kills"] = info.get("last_kills", 0)


###############################################
#                BOT RUNNER                  #
###############################################

bot.run(TOKEN)
