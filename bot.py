import os
import json
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from aiohttp import web

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ===== Web server for Railway =====
async def health_handler(request):
    return web.Response(text="iPond Top Duck bot online")

async def start_webserver():
    app = web.Application()
    app.router.add_get("/", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get('PORT', 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Web server running on port {port}")

# ===== Discord bot setup =====
TOKEN = os.environ["DISCORD_TOKEN"]
DATA_FILE = "data.json"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ===== Data helpers =====
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_guild_data(data, guild_id):
    gid = str(guild_id)
    if gid not in data:
        data[gid] = {
            "message_counts": {},
            "current_champion_id": None,
            "champion_role_id": None,
            "champion_vc_id": None,
            "announce_channel_id": None,
            "last_reset_date": None,
            "all_time_wins": {},
            "timezone_str": "America/Toronto",
            "reset_hour": 0,
            "reset_minute": 0,
        }
    return data[gid]

def get_guild_tz(guild_data):
    try:
        return ZoneInfo(guild_data.get("timezone_str", "America/Toronto"))
    except ZoneInfoNotFoundError:
        return ZoneInfo("America/Toronto")

def get_top_user(guild_data):
    """Returns user_id of winner. Tie = first to hit count based on dict order."""
    counts = guild_data["message_counts"]
    if not counts:
        return None
    max_count = max(counts.values())
    if max_count == 0:
        return None
    # First key with max value wins
    for uid, count in counts.items():
        if count == max_count:
            return int(uid)
    return None

# ===== Crown champion logic =====
async def crown_champion(guild, guild_data):
    top_user_id = get_top_user(guild_data)
    if not top_user_id:
        return None # No messages, don't crown

    member = guild.get_member(top_user_id)
    if not member:
        return None

    # Update all_time_wins
    guild_data["all_time_wins"][str(top_user_id)] = guild_data["all_time_wins"].get(str(top_user_id), 0) + 1
    guild_data["current_champion_id"] = top_user_id

    # Role handling
    role_id = guild_data.get("champion_role_id")
    if role_id:
        role = guild.get_role(int(role_id))
        old_champ_id = guild_data.get("current_champion_id")
        if role:
            # Remove from old champ
            if old_champ_id and old_champ_id!= top_user_id:
                old_member = guild.get_member(int(old_champ_id))
                if old_member and role in old_member.roles:
                    await old_member.remove_roles(role)
            # Add to new champ + rename
            if role not in member.roles:
                await member.add_roles(role)
            await role.edit(name=f"ðŸ‘‘ {member.display_name}")

    # VC handling
    vc_id = guild_data.get("champion_vc_id")
    if vc_id:
        vc = guild.get_channel(int(vc_id))
        if vc and isinstance(vc, discord.VoiceChannel):
            await vc.edit(name=f"ðŸ‘‘: {member.display_name}")
        else:
            # VC deleted, create new one
            overwrites = {guild.default_role: discord.PermissionOverwrite(connect=True, view_channel=True)}
            new_vc = await guild.create_voice_channel(f"ðŸ‘‘: {member.display_name}", overwrites=overwrites)
            guild_data["champion_vc_id"] = str(new_vc.id)

    # Bot status
    await bot.change_presence(activity=discord.Game(name=f"ðŸ‘‘ {member.display_name}"))

    return member

# ===== Events =====
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands")
    except Exception as e:
        print(e)

    # Check for missed reset on startup
    daily_reset.start()

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return
    data = load_data()
    guild_data = get_guild_data(data, message.guild.id)
    uid = str(message.author.id)
    guild_data["message_counts"][uid] = guild_data["message_counts"].get(uid, 0) + 1
    save_data(data)
    await bot.process_commands(message)

@bot.event
async def on_member_remove(member):
    """Delete user data if they leave"""
    data = load_data()
    guild_data = get_guild_data(data, member.guild.id)
    uid = str(member.id)
    if uid in guild_data["message_counts"]:
        del guild_data["message_counts"][uid]
    if uid in guild_data["all_time_wins"]:
        del guild_data["all_time_wins"][uid]
    if guild_data.get("current_champion_id") == member.id:
        guild_data["current_champion_id"] = None
    save_data(data)

# ===== Commands =====
@bot.tree.command(name="duck", description="Show current Top Duck")
async def duck(interaction: discord.Interaction):
    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    champ_id = guild_data.get("current_champion_id")

    if not champ_id:
        await interaction.response.send_message("No Top Duck yet!", ephemeral=True)
        return

    member = interaction.guild.get_member(int(champ_id))
    if not member:
        await interaction.response.send_message("No Top Duck yet!", ephemeral=True)
        return

    count = guild_data["message_counts"].get(str(champ_id), 0)
    embed = discord.Embed(description=f"**{member.display_name}** is the Top Duck!", color=member.color)
    embed.add_field(name="Messages Today", value=str(count))
    embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="leaderboard", description="Show top 10 chatters today")
async def leaderboard(interaction: discord.Interaction):
    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    counts = guild_data["message_counts"]

    if not counts:
        await interaction.response.send_message("No messages tracked yet today!", ephemeral=True)
        return

    sorted_counts = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]
    medals = ["ðŸ¥‡", "ðŸ¥ˆ", "ðŸ¥‰"]

    desc = ""
    for i, (uid, count) in enumerate(sorted_counts):
        member = interaction.guild.get_member(int(uid))
        name = member.display_name if member else f"User {uid}"
        medal = medals[i] if i < 3 else f"`{i+1}.`"
        desc += f"{medal} **{name}** â€” {count} messages\n"

    embed = discord.Embed(title="Today's Leaderboard", description=desc, color=0xFFD700)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="overall", description="Show all-time Top Duck wins")
async def overall(interaction: discord.Interaction):
    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    wins = guild_data["all_time_wins"]

    if not wins:
        await interaction.response.send_message("Nobody has won yet!", ephemeral=True)
        return

    sorted_wins = sorted(wins.items(), key=lambda x: x[1], reverse=True)

    # Pagination logic - 20 per page
    pages = [sorted_wins[i:i+20] for i in range(0, len(sorted_wins), 20)]
    page = 0

    def get_page_embed(page_num):
        medals = ["ðŸ¥‡", "ðŸ¥ˆ", "ðŸ¥‰"]
        desc = ""
        start_idx = page_num * 20
        for i, (uid, count) in enumerate(pages[page_num]):
            member = interaction.guild.get_member(int(uid))
            name = member.display_name if member else f"User {uid}"
            idx = start_idx + i
            medal = medals[idx] if idx < 3 else f"`{idx+1}.`"
            desc += f"{medal} **{name}** â€” {count} wins\n"
        embed = discord.Embed(title="All-Time Top Ducks", description=desc, color=0xFFD700)
        embed.set_footer(text=f"Page {page_num+1}/{len(pages)}")
        return embed

    await interaction.response.send_message(embed=get_page_embed(0))
    if len(pages) == 1:
        return

    msg = await interaction.original_response()
    await msg.add_reaction("â—€ï¸")
    await msg.add_reaction("â–¶ï¸")

    def check(reaction, user):
        return user == interaction.user and str(reaction.emoji) in ["â—€ï¸", "â–¶ï¸"] and reaction.message.id == msg.id

    while True:
        try:
            reaction, user = await bot.wait_for("reaction_add", timeout=60.0, check=check)
            if str(reaction.emoji) == "â–¶ï¸" and page < len(pages) - 1:
                page += 1
                await msg.edit(embed=get_page_embed(page))
            elif str(reaction.emoji) == "â—€ï¸" and page > 0:
                page -= 1
                await msg.edit(embed=get_page_embed(page))
            await msg.remove_reaction(reaction, user)
        except asyncio.TimeoutError:
            break

@bot.tree.command(name="stats", description="Show your Top Duck wins")
async def stats(interaction: discord.Interaction):
    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    wins = guild_data["all_time_wins"].get(str(interaction.user.id), 0)

    embed = discord.Embed(color=interaction.user.color)
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.add_field(name=interaction.user.display_name, value=f"**{wins}** total wins")
    await interaction.response.send_message(embed=embed)

# Admin commands
@bot.tree.command(name="setchannel", description="Set announcement channel")
@app_commands.default_permissions(administrator=True)
async def setchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    guild_data["announce_channel_id"] = str(channel.id)
    save_data(data)
    await interaction.response.send_message(f"âœ… Set to {channel.mention}", ephemeral=True)

@bot.tree.command(name="setchamprole", description="Set the champion role")
@app_commands.default_permissions(administrator=True)
async def setchamprole(interaction: discord.Interaction, role: discord.Role):
    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    guild_data["champion_role_id"] = str(role.id)
    save_data(data)
    await interaction.response.send_message(f"âœ… {role.mention} set!", ephemeral=True)

@bot.tree.command(name="setchampchannel", description="Set the champion voice channel")
@app_commands.default_permissions(administrator=True)
async def setchampchannel(interaction: discord.Interaction, channel: discord.VoiceChannel):
    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    guild_data["champion_vc_id"] = str(channel.id)
    save_data(data)
    await interaction.response.send_message(f"âœ… {channel.mention} set", ephemeral=True)

@bot.tree.command(name="settimezone", description="Set server timezone")
@app_commands.default_permissions(administrator=True)
async def settimezone(interaction: discord.Interaction, timezone: str):
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        await interaction.response.send_message("âŒ Invalid timezone. Use format like `America/Toronto`", ephemeral=True)
        return
    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    guild_data["timezone_str"] = timezone
    save_data(data)
    await interaction.response.send_message(f"âœ… Timezone set to {timezone}", ephemeral=True)

@bot.tree.command(name="settime", description="Set daily reset time")
@app_commands.default_permissions(administrator=True)
async def settime(interaction: discord.Interaction, time: str):
    try:
        hour, minute = map(int, time.split(":"))
        assert 0 <= hour <= 23 and 0 <= minute <= 59
    except:
        await interaction.response.send_message("âŒ Use 24-hour format like `00:00`", ephemeral=True)
        return

    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    guild_data["reset_hour"] = hour
    guild_data["reset_minute"] = minute
    tz = guild_data["timezone_str"]
    save_data(data)
    await interaction.response.send_message(f"âœ… Time set to {time} in {tz}", ephemeral=True)

@bot.tree.command(name="forcereset", description="Force daily reset now")
@app_commands.default_permissions(administrator=True)
async def forcereset(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    champ = await crown_champion(interaction.guild, guild_data)
    guild_data["message_counts"] = {}
    tz = get_guild_tz(guild_data)
    guild_data["last_reset_date"] = datetime.now(tz).strftime("%Y-%m-%d")
    save_data(data)

    if champ:
        await interaction.followup.send(f"ðŸ‘‘ Crowned {champ.mention} as Top Duck", ephemeral=True)
        # Announce if channel set
        ch_id = guild_data.get("announce_channel_id")
        if ch_id:
            ch = interaction.guild.get_channel(int(ch_id))
            if ch:
                embed = discord.Embed(color=champ.color)
                embed.description = f"-# All hail the top chatter\n# ðŸ‘‘ {champ.mention}"
                embed.set_thumbnail(url=champ.display_avatar.url)
                await ch.send(embed=embed)
    else:
        await interaction.followup.send("No messages today, no champion crowned.", ephemeral=True)

# ===== Daily reset task =====
@tasks.loop(minutes=1)
async def daily_reset():
    data = load_data()
    now_utc = datetime.now(ZoneInfo("UTC"))

    for gid, guild_data in data.items():
        tz = get_guild_tz(guild_data)
        now_local = now_utc.astimezone(tz)
        reset_hour = guild_data.get("reset_hour", 0)
        reset_minute = guild_data.get("reset_minute", 0)

        # Check if it's reset time and we haven't reset today
        last_reset = guild_data.get("last_reset_date")
        today_str = now_local.strftime("%Y-%m-%d")

        if (now_local.hour == reset_hour and now_local.minute == reset_minute and
            last_reset!= today_str):

            guild = bot.get_guild(int(gid))
            if not guild:
                continue

            champ = await crown_champion(guild, guild_data)
            guild_data["message_counts"] = {}
            guild_data["last_reset_date"] = today_str
            save_data(data)

            # Announce if there was a champ
            if champ:
                ch_id = guild_data.get("announce_channel_id")
                if ch_id:
                    ch = guild.get_channel(int(ch_id))
                    if ch:
                        embed = discord.Embed(color=champ.color)
                        embed.description = f"-# All hail the top chatter\n# ðŸ‘‘ {champ.mention}"
                        embed.set_thumbnail(url=champ.display_avatar.url)
                        await ch.send(embed=embed)

# ===== Entry Point =====
async def main():
    await start_webserver()
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())

# ===== Crown Commands =====
@bot.tree.command(name="curse", description="Curse a user until midnight")
async def curse(interaction: discord.Interaction, user: discord.Member):
    global cursed_user, cursed_until, crown_uses

    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    champ_role_id = guild_data.get("champion_role_id")

    if not champ_role_id or not any(str(r.id) == champ_role_id for r in interaction.user.roles):
        return await interaction.response.send_message("Only the crown can use this.", ephemeral=True)

    if user.bot: 
        return await interaction.response.send_message("Can't curse bots.", ephemeral=True)
    if crown_uses.get("curse") == datetime.now().date():
        return await interaction.response.send_message("Already used /curse today.", ephemeral=True)
    if cursed_user and datetime.now() < cursed_until:
        return await interaction.response.send_message("Someone's already cursed.", ephemeral=True)

    cursed_user = user.id
    cursed_until = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    crown_uses["curse"] = datetime.now().date()

    # Track stats
    victim_id = str(user.id)
    king_id = str(interaction.user.id)

    if "cursed_victims" not in guild_data:
        guild_data["cursed_victims"] = {}
    guild_data["cursed_victims"][victim_id] = guild_data["cursed_victims"].get(victim_id, 0) + 1

    if "crown_uses_count" not in guild_data:
        guild_data["crown_uses_count"] = {}
    guild_data["crown_uses_count"][king_id] = guild_data["crown_uses_count"].get(king_id, 0) + 1

    save_data(data)
    await interaction.response.send_message(f"ðŸ”® {user.mention} has been cursed.")

@bot.tree.command(name="mime", description="Make a user a mime for 10 minutes")
async def mime(interaction: discord.Interaction, user: discord.Member):
    global mimed_user, mime_until, crown_uses

    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    champ_role_id = guild_data.get("champion_role_id")

    if not champ_role_id or not any(str(r.id) == champ_role_id for r in interaction.user.roles):
        return await interaction.response.send_message("Only the crown can use this.", ephemeral=True)

    if user.bot: 
        return await interaction.response.send_message("Can't mime bots.", ephemeral=True)
    if crown_uses.get("mime") == datetime.now().date():
        return await interaction.response.send_message("Already used /mime today.", ephemeral=True)

    mimed_user = user.id
    mime_until = datetime.now() + timedelta(minutes=10)
    crown_uses["mime"] = datetime.now().date()

    # Track stats
    victim_id = str(user.id)
    king_id = str(interaction.user.id)

    if "mimed_victims" not in guild_data:
        guild_data["mimed_victims"] = {}
    guild_data["mimed_victims"][victim_id] = guild_data["mimed_victims"].get(victim_id, 0) + 1

    if "crown_uses_count" not in guild_data:
        guild_data["crown_uses_count"] = {}
    guild_data["crown_uses_count"][king_id] = guild_data["crown_uses_count"].get(king_id, 0) + 1

    save_data(data)
    await interaction.response.send_message(f"ðŸ™Š {user.mention} has been mimed.")

@bot.tree.command(name="jester", description="Make a user the jester until midnight")
async def jester(interaction: discord.Interaction, user: discord.Member):
    global jester_user, jester_until, crown_uses

    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    champ_role_id = guild_data.get("champion_role_id")

    if not champ_role_id or not any(str(r.id) == champ_role_id for r in interaction.user.roles):
        return await interaction.response.send_message("Only the crown can use this.", ephemeral=True)

    if user.bot: 
        return await interaction.response.send_message("Can't jester bots.", ephemeral=True)
    if crown_uses.get("jester") == datetime.now().date():
        return await interaction.response.send_message("Already used /jester today.", ephemeral=True)
    if jester_user and datetime.now() < jester_until:
        return await interaction.response.send_message("Only one jester allowed.", ephemeral=True)

    try: 
        await user.edit(nick=f"ðŸ¤¡ {user.display_name}"[:32])
    except: 
        return await interaction.response.send_message("Can't edit that user's nickname.", ephemeral=True)

    jester_user = user.id
    jester_until = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    crown_uses["jester"] = datetime.now().date()

    # Track stats
    victim_id = str(user.id)
    king_id = str(interaction.user.id)

    if "jester_victims" not in guild_data:
        guild_data["jester_victims"] = {}
    guild_data["jester_victims"][victim_id] = guild_data["jester_victims"].get(victim_id, 0) + 1

    if "crown_uses_count" not in guild_data:
        guild_data["crown_uses_count"] = {}
    guild_data["crown_uses_count"][king_id] = guild_data["crown_uses_count"].get(king_id, 0) + 1

    save_data(data)
    await interaction.response.send_message(f"ðŸ¤¡ {user.mention} has been jestered.")
