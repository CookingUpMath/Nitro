###############################################
#         DUCK COLLECTION SYSTEM             #
###############################################
# A standalone gacha/collection feature, unrelated to the Fortnite side of
# the bot. Lives entirely in this file and is loaded into the main bot as a
# Cog. Shares the main bot's Postgres connection pool (see fortnite_bot.py)
# but keeps its own state and its own three storage keys in the same
# key/value bot_state table: 'duck_index', 'duck_users', 'duck_config'.
#
# HOW A DROP WORKS (worth understanding before touching this file):
# A normal chat message can never produce a truly private ("ephemeral")
# Discord message — that only exists as a reply to an interaction. So a
# passive chat-triggered drop can't be private on its own. Instead:
#   1. On a successful roll, the bot posts a normal PUBLIC message
#      ("🥚 Alex found an egg!") with a "Claim" button restricted to Alex.
#   2. When Alex clicks it, THAT click is a fresh interaction — so the bot
#      can respond to it with a genuinely private (ephemeral) Hatch/Store
#      prompt that only Alex can see.
# This gives a public "something happened" moment plus a private outcome,
# without needing DMs.

import re
import json
import time
import random

import discord
from discord import app_commands
from discord.ext import commands, tasks


###############################################
#                CONSTANTS                   #
###############################################

RARITY_ORDER = ["common", "rare", "legendary", "mythic", "secret", "ghost"]

RARITY_WEIGHTS = {
    "common": 75.0,
    "rare": 18.0,
    "legendary": 5.0,
    "mythic": 1.5,
    "secret": 0.45,
    "ghost": 0.05,
}

RARITY_DISPLAY = {
    "common": "Common",
    "rare": "Rare",
    "legendary": "Legendary",
    "mythic": "Mythic",
    "secret": "Secret",
    "ghost": "Ghost",
}

DROP_CHANCE = 0.02          # 2% per eligible message
DUPLICATE_BONUS_CHANCE = 0.01  # 1% extra egg on a duplicate hatch
DROP_COOLDOWN_SECONDS = 120  # 2 minutes between roll attempts, per user
CLAIM_TIMEOUT_SECONDS = 600  # 10 minutes to claim a spawned egg


###############################################
#          STATE (Postgres-backed)           #
###############################################
# duck_index[duck_id] = {
#   "title": str, "emoji": str, "rarity": str,
#   "active": bool, "limited_until": float | None (unix timestamp)
# }
duck_index = {}

# duck_users[discord_id] = {
#   "inventory": int, "collection": [duck_id, ...], "last_roll_check_ts": float
# }
duck_users = {}

# duck_config[guild_id] = {"hatching_enabled": bool}
duck_config = {}


def default_user_record():
    return {"inventory": 0, "collection": [], "last_roll_check_ts": 0}


# Set once in DuckCog.cog_load() from bot.db_pool — avoids any circular
# import back into the main bot file.
_pool = None


async def load_duck_state():
    global duck_index, duck_users, duck_config
    if _pool is None:
        return
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value FROM bot_state WHERE key IN ('duck_index','duck_users','duck_config')"
            )
            data = {row["key"]: json.loads(row["value"]) for row in rows}
            duck_index = data.get("duck_index", {})
            duck_users = data.get("duck_users", {})
            duck_config = data.get("duck_config", {})
            print(f"[duck_db] loaded {len(duck_index)} duck(s), {len(duck_users)} user record(s)")
    except Exception as e:
        print(f"[duck_db] load failed: {e}")


async def save_duck_state() -> bool:
    if _pool is None:
        return False
    try:
        async with _pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO bot_state (key, value) VALUES ($1, $2::jsonb)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                [
                    ("duck_index", json.dumps(duck_index)),
                    ("duck_users", json.dumps(duck_users)),
                    ("duck_config", json.dumps(duck_config)),
                ],
            )
        return True
    except Exception as e:
        print(f"[duck_db] save failed: {e}")
        return False



###############################################
#              HELPER FUNCTIONS              #
###############################################

def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")


def get_active_ducks_by_rarity():
    by_rarity = {r: [] for r in RARITY_ORDER}
    for duck_id, duck in duck_index.items():
        if duck.get("active"):
            by_rarity[duck["rarity"]].append(duck_id)
    return by_rarity


def roll_duck_id():
    """Pick one active duck, weighted by rarity. Rarity tiers with zero
    currently-active ducks are excluded and the remaining weights are used
    as-is (random.choices renormalizes automatically). Returns None if the
    pool is completely empty.
    """
    by_rarity = get_active_ducks_by_rarity()
    available_tiers = [r for r in RARITY_ORDER if by_rarity[r]]
    if not available_tiers:
        return None
    weights = [RARITY_WEIGHTS[r] for r in available_tiers]
    chosen_tier = random.choices(available_tiers, weights=weights, k=1)[0]
    return random.choice(by_rarity[chosen_tier])


def resolve_hatch(discord_id: str):
    """Roll and apply one hatch for a user. Returns a result dict, or None
    if the pool is currently empty. Does NOT touch inventory count — the
    caller decides whether an egg was 'spent' (batch open) or never stored
    (immediate hatch from a fresh drop).
    """
    duck_id = roll_duck_id()
    if duck_id is None:
        return None

    rec = duck_users.setdefault(discord_id, default_user_record())
    duck = duck_index[duck_id]
    is_duplicate = duck_id in rec["collection"]
    bonus_egg = False

    if is_duplicate:
        if random.random() < DUPLICATE_BONUS_CHANCE:
            rec["inventory"] += 1
            bonus_egg = True
    else:
        rec["collection"].append(duck_id)

    return {
        "duck_id": duck_id,
        "title": duck["title"],
        "emoji": duck["emoji"],
        "rarity": duck["rarity"],
        "duplicate": is_duplicate,
        "bonus_egg": bonus_egg,
    }


def group_by_rarity(duck_ids):
    grouped = {r: [] for r in RARITY_ORDER}
    for duck_id in duck_ids:
        duck = duck_index.get(duck_id)
        if duck:
            grouped[duck["rarity"]].append(duck)
    return grouped


def format_grouped(grouped, show_title: bool = True) -> str:
    lines = []
    for r in RARITY_ORDER:
        ducks = grouped.get(r, [])
        if not ducks:
            continue
        lines.append(f"-# {RARITY_DISPLAY[r]}")
        for duck in ducks:
            lines.append(f"# {duck['emoji']} {duck['title']}" if show_title else f"# {duck['emoji']}")
        lines.append("")
    return "\n".join(lines).strip()


###############################################
#                 UI: VIEWS                   #
###############################################

class DuckHatchStoreView(discord.ui.View):
    """Ephemeral — only the person who claimed the egg ever sees this."""

    def __init__(self, owner_id: int):
        super().__init__(timeout=300)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="Hatch Now", style=discord.ButtonStyle.success, emoji="🐣")
    async def hatch_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        discord_id = str(interaction.user.id)
        result = resolve_hatch(discord_id)

        if result is None:
            await save_duck_state()
            return await interaction.response.edit_message(
                content="The pool is empty right now — nothing to hatch. Sorry, egg's gone!",
                view=None,
            )

        await save_duck_state()

        if result["duplicate"]:
            text = f"{result['emoji']} **{result['title']}** — you already have this one, duplicate!"
            if result["bonus_egg"]:
                inv = duck_users[discord_id]["inventory"]
                text += f"\n🍀 Lucky! You got a bonus egg for the trouble. Inventory: **{inv}**"
        else:
            text = (
                f"{result['emoji']} **{result['title']}** ({RARITY_DISPLAY[result['rarity']]}) "
                f"— new duck added to your collection!"
            )

        await interaction.response.edit_message(content=text, view=None)

    @discord.ui.button(label="Store in Inventory", style=discord.ButtonStyle.secondary, emoji="🎒")
    async def store(self, interaction: discord.Interaction, button: discord.ui.Button):
        discord_id = str(interaction.user.id)
        rec = duck_users.setdefault(discord_id, default_user_record())
        rec["inventory"] += 1
        await save_duck_state()
        await interaction.response.edit_message(
            content=f"🥚 Stored! You now have **{rec['inventory']}** egg(s) in your inventory.",
            view=None,
        )


class DuckClaimView(discord.ui.View):
    """Public message, but only the owner can actually claim it."""

    def __init__(self, owner_id: int):
        super().__init__(timeout=CLAIM_TIMEOUT_SECONDS)
        self.owner_id = owner_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This egg isn't yours to claim!", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Claim Egg", style=discord.ButtonStyle.success, emoji="🥚")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "You found an egg! What would you like to do with it?",
            view=DuckHatchStoreView(self.owner_id),
            ephemeral=True,
        )
        try:
            await interaction.message.edit(content=f"~~{interaction.message.content}~~ (claimed)", view=None)
        except Exception:
            pass
        self.stop()

    async def on_timeout(self):
        if self.message is None:
            return
        try:
            await self.message.edit(content=f"~~{self.message.content}~~ (expired, unclaimed)", view=None)
        except Exception:
            pass


class RaritySelectView(discord.ui.View):
    def __init__(self, title: str, emoji: str):
        super().__init__(timeout=180)
        self.title_text = title
        self.emoji = emoji

        select = discord.ui.Select(
            placeholder="Choose a rarity",
            options=[discord.SelectOption(label=RARITY_DISPLAY[r], value=r) for r in RARITY_ORDER],
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        rarity = interaction.data["values"][0]
        duck_id = slugify(self.title_text)

        if duck_id in duck_index:
            return await interaction.response.edit_message(
                content=f"A duck titled **{self.title_text}** already exists in the index.",
                view=None,
            )

        duck_index[duck_id] = {
            "title": self.title_text,
            "emoji": self.emoji,
            "rarity": rarity,
            "active": False,
            "limited_until": None,
        }
        await save_duck_state()

        await interaction.response.edit_message(
            content=(
                f"{self.emoji} **{self.title_text}** added to the index as {RARITY_DISPLAY[rarity]}.\n"
                f"Use `/duckon title:{self.title_text}` to move it into the earnable pool."
            ),
            view=None,
        )


class DuckAddModal(discord.ui.Modal, title="Add a New Duck"):
    duck_title = discord.ui.TextInput(label="Duck Title", placeholder="e.g. Golden Duck", max_length=100)
    duck_emoji = discord.ui.TextInput(
        label="Emoji",
        placeholder="🦆 or <:customduck:123456789012345678>",
        max_length=100,
    )

    async def on_submit(self, interaction: discord.Interaction):
        view = RaritySelectView(str(self.duck_title), str(self.duck_emoji))
        await interaction.response.send_message(
            f"Pick a rarity for **{self.duck_title}** {self.duck_emoji}:",
            view=view,
            ephemeral=True,
        )


###############################################
#                   COG                      #
###############################################

class DuckCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        global _pool
        _pool = self.bot.db_pool
        await load_duck_state()
        self.check_expired_limited.start()

    async def cog_unload(self):
        self.check_expired_limited.cancel()

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You do not have permission to use this command.", ephemeral=True
            )
        else:
            print(f"[duck_system] command error: {error}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Something went wrong running that command.", ephemeral=True
                )

    # ---------- passive chat drop ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        guild_id = str(message.guild.id)
        if not duck_config.get(guild_id, {}).get("hatching_enabled", True):
            return

        discord_id = str(message.author.id)
        rec = duck_users.setdefault(discord_id, default_user_record())

        now = time.time()
        if now - rec.get("last_roll_check_ts", 0) < DROP_COOLDOWN_SECONDS:
            return
        rec["last_roll_check_ts"] = now  # kept in-memory only — not worth a DB write on every message

        if random.random() >= DROP_CHANCE:
            return
        if roll_duck_id() is None:
            return  # pool is empty, nothing to give right now

        view = DuckClaimView(message.author.id)
        try:
            sent = await message.channel.send(
                content=f"🥚 {message.author.mention} found an egg on the ground!",
                view=view,
            )
            view.message = sent
        except Exception as e:
            print(f"[duck_system] failed to post egg drop: {e}")

    # ---------- staff: toggle hatching ----------

    @app_commands.command(name="hatchtoggle", description="Staff: turn egg drops on or off.")
    @app_commands.describe(state="Turn drops on or off")
    @app_commands.choices(state=[
        app_commands.Choice(name="On", value="on"),
        app_commands.Choice(name="Off", value="off"),
    ])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def hatchtoggle(self, interaction: discord.Interaction, state: app_commands.Choice[str]):
        guild_id = str(interaction.guild.id)
        duck_config.setdefault(guild_id, {})["hatching_enabled"] = (state.value == "on")
        await save_duck_state()
        await interaction.response.send_message(f"Egg drops are now **{state.name}**.", ephemeral=True)

    # ---------- staff: add a duck to the index ----------

    @app_commands.command(name="duckadd", description="Staff: add a new duck to the index.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def duckadd(self, interaction: discord.Interaction):
        await interaction.response.send_modal(DuckAddModal())

    # ---------- staff: activate / deactivate ----------

    @app_commands.command(name="duckon", description="Staff: move a duck into the earnable pool.")
    @app_commands.describe(title="The exact title of the duck")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def duckon(self, interaction: discord.Interaction, title: str):
        duck_id = slugify(title)
        duck = duck_index.get(duck_id)
        if not duck:
            return await interaction.response.send_message(f"No duck found matching **{title}**.", ephemeral=True)
        duck["active"] = True
        duck["limited_until"] = None
        await save_duck_state()
        await interaction.response.send_message(f"{duck['emoji']} **{duck['title']}** is now earnable!", ephemeral=True)

    @app_commands.command(name="duckoff", description="Staff: remove a duck from the earnable pool.")
    @app_commands.describe(title="The exact title of the duck")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def duckoff(self, interaction: discord.Interaction, title: str):
        duck_id = slugify(title)
        duck = duck_index.get(duck_id)
        if not duck:
            return await interaction.response.send_message(f"No duck found matching **{title}**.", ephemeral=True)
        duck["active"] = False
        duck["limited_until"] = None
        await save_duck_state()
        await interaction.response.send_message(f"{duck['emoji']} **{duck['title']}** is no longer earnable.", ephemeral=True)

    # ---------- staff: limited-time availability ----------

    @app_commands.command(name="ducklimited", description="Staff: make a duck earnable for a limited number of hours.")
    @app_commands.describe(title="The exact title of the duck", hours="How many hours it stays earnable")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ducklimited(self, interaction: discord.Interaction, title: str, hours: app_commands.Range[int, 1, 8760]):
        duck_id = slugify(title)
        duck = duck_index.get(duck_id)
        if not duck:
            return await interaction.response.send_message(f"No duck found matching **{title}**.", ephemeral=True)
        duck["active"] = True
        duck["limited_until"] = time.time() + hours * 3600
        await save_duck_state()
        await interaction.response.send_message(
            f"{duck['emoji']} **{duck['title']}** is earnable for the next **{hours} hour(s)**.",
            ephemeral=True,
        )

    @tasks.loop(minutes=1)
    async def check_expired_limited(self):
        now = time.time()
        changed = False
        for duck in duck_index.values():
            if duck.get("active") and duck.get("limited_until") and now >= duck["limited_until"]:
                duck["active"] = False
                duck["limited_until"] = None
                changed = True
        if changed:
            await save_duck_state()

    @check_expired_limited.before_loop
    async def before_check_expired_limited(self):
        await self.bot.wait_until_ready()

    # ---------- public: view pool / index / collection ----------

    @app_commands.command(name="hatchpool", description="View the current earnable duck pool.")
    async def hatchpool(self, interaction: discord.Interaction):
        active_ids = [d for d, v in duck_index.items() if v["active"]]
        content = format_grouped(group_by_rarity(active_ids)) or "The pool is currently empty."
        embed = discord.Embed(title="🥚 Current Hatch Pool", description=content, color=discord.Color.gold())
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="index", description="View every duck that has ever been added.")
    async def index_cmd(self, interaction: discord.Interaction):
        active_ids = [d for d, v in duck_index.items() if v["active"]]
        inactive_ids = [d for d, v in duck_index.items() if not v["active"]]

        embed = discord.Embed(title="📖 Duck Index", color=discord.Color.blurple())
        if not duck_index:
            embed.description = "No ducks have been added yet."
        if active_ids:
            embed.add_field(name="Currently Earnable", value=format_grouped(group_by_rarity(active_ids)), inline=False)
        if inactive_ids:
            embed.add_field(name="Not Currently Active", value=format_grouped(group_by_rarity(inactive_ids)), inline=False)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="collection", description="View your (or someone else's) duck collection.")
    @app_commands.describe(member="Whose collection to view")
    async def collection_cmd(self, interaction: discord.Interaction, member: discord.Member = None):
        target = member or interaction.user
        discord_id = str(target.id)
        rec = duck_users.get(discord_id, default_user_record())

        content = format_grouped(group_by_rarity(rec["collection"]), show_title=False) or "No ducks collected yet."
        embed = discord.Embed(title=f"🦆 {target.display_name}'s Collection", description=content, color=discord.Color.teal())
        embed.set_footer(text=f"{len(rec['collection'])}/{len(duck_index)} collected")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="duckinventory", description="Check how many eggs you have in storage.")
    async def duckinventory_cmd(self, interaction: discord.Interaction):
        discord_id = str(interaction.user.id)
        rec = duck_users.get(discord_id, default_user_record())
        await interaction.response.send_message(f"🥚 You have **{rec['inventory']}** egg(s) stored.", ephemeral=True)

    # ---------- open eggs from inventory ----------

    @app_commands.command(name="open", description="Open eggs from your inventory.")
    @app_commands.describe(amount="How many eggs to open")
    async def open_eggs(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 1000]):
        await interaction.response.defer()
        discord_id = str(interaction.user.id)
        rec = duck_users.setdefault(discord_id, default_user_record())

        if rec["inventory"] < amount:
            return await interaction.followup.send(
                f"You only have **{rec['inventory']}** egg(s) — you can't open {amount}."
            )

        results = []
        for _ in range(amount):
            result = resolve_hatch(discord_id)
            if result is None:
                break  # pool went empty mid-batch
            rec["inventory"] -= 1
            results.append(result)

        await save_duck_state()

        if not results:
            return await interaction.followup.send("The pool is currently empty — nothing could be hatched.")

        lines = [f"🥚 Opened {len(results)} egg(s):"]
        for r in results:
            tag = " (duplicate)" if r["duplicate"] else " (**NEW**)"
            bonus = " 🍀+1 bonus egg" if r["bonus_egg"] else ""
            lines.append(f"{r['emoji']} **{r['title']}**{tag}{bonus}")

        await interaction.followup.send("\n".join(lines))


async def setup(bot: commands.Bot):
    await bot.add_cog(DuckCog(bot))
