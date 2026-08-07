###############################################
#         DUCK COLLECTION SYSTEM             #
###############################################
# A standalone gacha/collection feature, unrelated to the Fortnite side of
# the bot. Lives entirely in this file and is loaded into the main bot as a
# Cog. Shares the main bot's Postgres connection pool (see fortnite_bot.py)
# but keeps its own state and its own storage keys in the same key/value
# bot_state table: 'duck_index', 'duck_users', 'duck_config', 'duck_stats'.
#
# HOW A DROP WORKS:
# A drop is fully public — the bot posts a normal message in the channel
# ("🥚 Alex found an egg!") with "Hatch" and "Inventory" buttons attached
# right away. Only the person who found it can press them (everyone else
# gets a quiet ephemeral "not yours" notice). Whichever button they press
# edits that same message in place with the outcome — no private/ephemeral
# step, no DMs.
#
# ALL staff/config actions live behind the single /editor command (a
# dropdown menu), mirroring the Fortnite side's /settings command. There
# are no separate /duckadd, /duckon, etc. slash commands anymore.

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

DROP_CHANCE = 0.06             # 6% per eligible message
DUPLICATE_BONUS_CHANCE = 0.05  # 5% extra egg on a duplicate hatch
DROP_COOLDOWN_SECONDS = 120    # 2 minutes between roll attempts, per user
CLAIM_TIMEOUT_SECONDS = 600    # 10 minutes before an unclaimed drop expires
EGG_COUNTER_UPDATE_MINUTES = 5  # how often the VC name is allowed to refresh

# --- Karma ---
HEART_EMOJI_ID = 1295255068483784786  # <:D_ZLove:...>
KARMA_PER_EGG = 10
REACTION_KARMA_WINDOW_SECONDS = 60 * 60   # heart reaction must land within 1 hour of the post
WELCOME_WINDOW_SECONDS = 30 * 60          # welcome must happen within 30 min of the join
GM_PATTERN = re.compile(r"\b(gm|good\s?morning)\b", re.IGNORECASE)
WELCOME_PATTERN = re.compile(r"\bwelcome\b", re.IGNORECASE)


def format_rarity_percent(r: str) -> str:
    w = RARITY_WEIGHTS[r]
    return f"{int(w)}%" if w == int(w) else f"{w}%"


def rarity_header(r: str) -> str:
    return f"{RARITY_DISPLAY[r]} ({format_rarity_percent(r)})"


# How much of a "win" a hatch announcement should feel like, scaled to how
# hard the rarity actually is to get. Common barely registers; Ghost gets
# the full fanfare treatment.
HYPE_STYLE = {
    "common": {
        "color": discord.Color.light_grey(),
        "banner": "{emoji} {mention} hatched **{title}**.",
    },
    "rare": {
        "color": discord.Color.blue(),
        "banner": "✨ {emoji} {mention} hatched **{title}**!",
    },
    "legendary": {
        "color": discord.Color.gold(),
        "banner": "🌟 **{emoji} {mention} hatched a {title}!** 🌟",
    },
    "mythic": {
        "color": discord.Color.purple(),
        "banner": "💥 **{emoji} {mention} HATCHED A {title}!!** 💥",
    },
    "secret": {
        "color": discord.Color.red(),
        "banner": "🎇🎇 **{emoji} {mention} UNCOVERED THE SECRET {title}!!** 🎇🎇",
    },
    "ghost": {
        "color": discord.Color.dark_purple(),
        "banner": (
            "👻═══════════👻\n"
            "**{emoji} {mention} HATCHED THE GHOST DUCK — {title}!!!**\n"
            "👻═══════════👻"
        ),
    },
}


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

# duck_config[guild_id] = {
#   "hatching_enabled": bool, "egg_counter_channel_id": int | None
# }
duck_config = {}

# duck_stats = {"total_dropped": int}  — global count of eggs that have
# ever spawned in the pond (i.e. successful passive-chat drops).
duck_stats = {"total_dropped": 0}

# pending_invite_karma[str(new_member_id)] = str(inviter_id)  — set on join,
# consumed (and karma awarded) the moment that new member sends their first
# message. Persisted so a restart between join and first message doesn't
# lose the credit.
pending_invite_karma = {}

# --- In-memory only (short-lived windows, fine to lose on restart) ---
# reaction_karma_granted[str(message_id)] = set of user_ids who have
# already earned a point for reacting to that message — lets every
# distinct reactor earn their own point, while still stopping any single
# person from farming it by removing/re-adding their own reaction.
reaction_karma_granted = {}
# recent_joins[member_id] = {"joined_at": float, "welcomed_by": set()}
recent_joins = {}
# invite_cache[guild_id] = {invite_code: uses} — snapshot used to detect
# which invite incremented on a new join.
invite_cache = {}


def default_user_record():
    return {
        "inventory": 0,
        "collection": [],
        "last_roll_check_ts": 0,
        "last_daily_egg_date": None,
        "last_gm_date": None,
        "karma": 0,
    }


def get_user_record(discord_id: str) -> dict:
    """Like duck_users.setdefault(), but also backfills new fields onto
    older records that were saved before this feature existed.
    """
    rec = duck_users.setdefault(discord_id, default_user_record())
    rec.setdefault("last_daily_egg_date", None)
    rec.setdefault("last_gm_date", None)
    rec.setdefault("karma", 0)
    return rec


def award_karma(discord_id: str, points: int = 1):
    rec = get_user_record(discord_id)
    rec["karma"] = rec.get("karma", 0) + points
    while rec["karma"] >= KARMA_PER_EGG:
        rec["karma"] -= KARMA_PER_EGG
        rec["inventory"] += 1


def remove_karma(discord_id: str, points: int = 1):
    rec = get_user_record(discord_id)
    rec["karma"] = max(rec.get("karma", 0) - points, 0)


# Set once in DuckCog.cog_load() from bot.db_pool — avoids any circular
# import back into the main bot file.
_pool = None


async def load_duck_state():
    global duck_index, duck_users, duck_config, duck_stats, pending_invite_karma
    if _pool is None:
        return
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value FROM bot_state WHERE key IN "
                "('duck_index','duck_users','duck_config','duck_stats','duck_pending_invites')"
            )
            data = {row["key"]: json.loads(row["value"]) for row in rows}
            duck_index = data.get("duck_index", {})
            duck_users = data.get("duck_users", {})
            duck_config = data.get("duck_config", {})
            duck_stats = data.get("duck_stats", {"total_dropped": 0})
            pending_invite_karma = data.get("duck_pending_invites", {})
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
                    ("duck_stats", json.dumps(duck_stats)),
                    ("duck_pending_invites", json.dumps(pending_invite_karma)),
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


def rename_duck(old_id: str, new_title: str) -> str:
    """Rename a duck, moving its index entry to a new key if the slug
    changes, and migrating every owner's collection reference so nobody's
    existing collection silently breaks. Raises ValueError if the new
    title collides with a different existing duck.
    """
    new_id = slugify(new_title)
    if new_id == old_id:
        duck_index[old_id]["title"] = new_title
        return old_id
    if new_id in duck_index:
        raise ValueError(f"A duck titled '{new_title}' already exists.")

    duck_index[new_id] = duck_index.pop(old_id)
    duck_index[new_id]["title"] = new_title

    for rec in duck_users.values():
        collection = rec.get("collection", [])
        if old_id in collection:
            rec["collection"] = [new_id if d == old_id else d for d in collection]

    return new_id


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


def format_flat_row(duck_ids) -> str:
    """All ducks together on one '#' line, no rarity grouping/labels at
    all. Used by /collection.
    """
    ducks = [duck_index[d] for d in duck_ids if d in duck_index]
    if not ducks:
        return ""
    return "# " + " ".join(duck["emoji"] for duck in ducks)


def format_grouped_row(grouped) -> str:
    """Large-emoji, emoji-only style, but all ducks in a rarity tier share
    ONE '#' line instead of one line each — compresses the display
    horizontally. Used by /index.
    """
    lines = []
    for r in RARITY_ORDER:
        ducks = grouped.get(r, [])
        if not ducks:
            continue
        lines.append(f"-# {rarity_header(r)}")
        lines.append("# " + " ".join(duck["emoji"] for duck in ducks))
        lines.append("")
    return "\n".join(lines).strip()


def format_grouped_plain(grouped) -> str:
    """Regular-sized style: bold 'Rarity (x%)' label, normal-size
    'emoji title' lines underneath. Used by /hatchpool.
    """
    lines = []
    for r in RARITY_ORDER:
        ducks = grouped.get(r, [])
        if not ducks:
            continue
        lines.append(f"**{rarity_header(r)}**")
        for duck in ducks:
            lines.append(f"{duck['emoji']} {duck['title']}")
        lines.append("")
    return "\n".join(lines).strip()


def format_egg_channel_name(count: int) -> str:
    return f"🥚: {count}"


###############################################
#                 UI: VIEWS                   #
###############################################

class EggDropView(discord.ui.View):
    """Fully public — the drop message itself has the Hatch/Inventory
    buttons. Only the person who found it can press them; anyone else gets
    a quiet ephemeral 'not yours' notice. Whichever button is pressed edits
    the original public message in place with the outcome.
    """

    def __init__(self, owner_id: int):
        super().__init__(timeout=CLAIM_TIMEOUT_SECONDS)
        self.owner_id = owner_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This egg isn't yours!", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Hatch", style=discord.ButtonStyle.success, emoji="🐣")
    async def hatch(self, interaction: discord.Interaction, button: discord.ui.Button):
        discord_id = str(interaction.user.id)
        result = resolve_hatch(discord_id)

        if result is None:
            await save_duck_state()
            return await interaction.response.edit_message(
                content="The pool is empty right now — nothing to hatch. Sorry, egg's gone!",
                embed=None,
                view=None,
            )

        await save_duck_state()
        self.stop()

        if result["duplicate"]:
            text = f"{result['emoji']} {interaction.user.mention} hatched **{result['title']}** — already owned, duplicate!"
            if result["bonus_egg"]:
                inv = duck_users[discord_id]["inventory"]
                text += f"\n🍀 Lucky! A bonus egg was awarded. Inventory: **{inv}**"
            await interaction.response.edit_message(content=text, embed=None, view=None)
        else:
            style = HYPE_STYLE[result["rarity"]]
            banner = style["banner"].format(
                emoji=result["emoji"], mention=interaction.user.mention, title=result["title"]
            )
            embed = discord.Embed(description=banner, color=style["color"])
            embed.set_footer(text=rarity_header(result["rarity"]))
            await interaction.response.edit_message(content=None, embed=embed, view=None)

    @discord.ui.button(label="Inventory", style=discord.ButtonStyle.secondary, emoji="🎒")
    async def store(self, interaction: discord.Interaction, button: discord.ui.Button):
        discord_id = str(interaction.user.id)
        rec = duck_users.setdefault(discord_id, default_user_record())
        rec["inventory"] += 1
        await save_duck_state()
        self.stop()
        await interaction.response.edit_message(
            content=f"🎒 {interaction.user.mention} stored the egg! Inventory: **{rec['inventory']}**",
            embed=None,
            view=None,
        )

    async def on_timeout(self):
        if self.message is None:
            return
        try:
            await self.message.edit(content=f"~~{self.message.content}~~ (expired, unclaimed)", view=None)
        except Exception:
            pass


class RaritySelectView(discord.ui.View):
    """Used by both Add and Edit flows — on_result is called with the
    chosen rarity value instead of hardcoding what happens next.
    """

    def __init__(self, on_result):
        super().__init__(timeout=180)
        self.on_result = on_result
        select = discord.ui.Select(
            placeholder="Choose a rarity",
            options=[discord.SelectOption(label=rarity_header(r), value=r) for r in RARITY_ORDER],
        )
        select.callback = self._callback
        self.add_item(select)

    async def _callback(self, interaction: discord.Interaction):
        rarity = interaction.data["values"][0]
        await self.on_result(interaction, rarity)


class DuckAddModal(discord.ui.Modal, title="Add a New Duck"):
    duck_title = discord.ui.TextInput(label="Duck Title", placeholder="e.g. Golden Duck", max_length=100)
    duck_emoji = discord.ui.TextInput(
        label="Emoji",
        placeholder="🦆 or <:customduck:123456789012345678>",
        max_length=100,
    )

    async def on_submit(self, interaction: discord.Interaction):
        title_text = str(self.duck_title)
        emoji_text = str(self.duck_emoji)

        async def finish(inner_interaction: discord.Interaction, rarity: str):
            duck_id = slugify(title_text)
            if duck_id in duck_index:
                return await inner_interaction.response.edit_message(
                    content=f"A duck titled **{title_text}** already exists in the index.",
                    view=None,
                )
            duck_index[duck_id] = {
                "title": title_text,
                "emoji": emoji_text,
                "rarity": rarity,
                "active": False,
                "limited_until": None,
            }
            await save_duck_state()
            await inner_interaction.response.edit_message(
                content=(
                    f"{emoji_text} **{title_text}** added to the index as {RARITY_DISPLAY[rarity]}.\n"
                    f"Use `/editor` → Activate Duck to move it into the earnable pool."
                ),
                view=None,
            )

        await interaction.response.send_message(
            f"Pick a rarity for **{title_text}** {emoji_text}:",
            view=RaritySelectView(finish),
            ephemeral=True,
        )


class RarityEditSelectView(discord.ui.View):
    """Final step of editing a duck — rarity change is optional, so this
    includes a 'keep current' choice alongside the six tiers.
    """

    def __init__(self, duck_id: str, pending_title: str | None, pending_emoji: str | None):
        super().__init__(timeout=180)
        self.duck_id = duck_id
        self.pending_title = pending_title
        self.pending_emoji = pending_emoji

        options = [discord.SelectOption(label="Keep Current Rarity", value="_keep", emoji="↩️")]
        options += [discord.SelectOption(label=rarity_header(r), value=r) for r in RARITY_ORDER]
        select = discord.ui.Select(placeholder="Change rarity? (optional)", options=options)
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        duck = duck_index.get(self.duck_id)
        if not duck:
            return await interaction.response.edit_message(content="That duck no longer exists.", view=None)

        rarity_choice = interaction.data["values"][0]
        summary = []
        current_id = self.duck_id

        if self.pending_title and self.pending_title != duck["title"]:
            try:
                current_id = rename_duck(current_id, self.pending_title)
            except ValueError as e:
                return await interaction.response.edit_message(content=str(e), view=None)
            duck = duck_index[current_id]
            summary.append(f"title → **{duck['title']}**")

        if self.pending_emoji and self.pending_emoji != duck["emoji"]:
            old_emoji = duck["emoji"]
            duck["emoji"] = self.pending_emoji
            summary.append(f"emoji {old_emoji} → {duck['emoji']}")

        if rarity_choice != "_keep" and rarity_choice != duck["rarity"]:
            duck["rarity"] = rarity_choice
            summary.append(f"rarity → {rarity_header(rarity_choice)}")

        await save_duck_state()

        if not summary:
            content = f"No changes made to **{duck['title']}**."
        else:
            content = (
                f"Updated **{duck['title']}**: " + ", ".join(summary) +
                "\nThis applies everywhere instantly, including everyone's existing collections."
            )
        await interaction.response.edit_message(content=content, view=None)


class DuckEditModal(discord.ui.Modal, title="Edit Duck"):
    duck_title = discord.ui.TextInput(label="Duck Title (exact, to find it)", max_length=100)
    new_title = discord.ui.TextInput(label="New Title (optional)", required=False, max_length=100)
    new_emoji = discord.ui.TextInput(label="New Emoji (optional)", required=False, max_length=100)

    async def on_submit(self, interaction: discord.Interaction):
        duck_id = slugify(str(self.duck_title))
        duck = duck_index.get(duck_id)
        if not duck:
            return await interaction.response.send_message(
                f"No duck found matching **{self.duck_title}**.", ephemeral=True
            )

        pending_title = str(self.new_title).strip() or None
        pending_emoji = str(self.new_emoji).strip() or None

        view = RarityEditSelectView(duck_id, pending_title, pending_emoji)
        await interaction.response.send_message(
            f"Optionally change the rarity for **{duck['title']}** (currently {rarity_header(duck['rarity'])}):",
            view=view,
            ephemeral=True,
        )


class ConfirmRemoveView(discord.ui.View):
    def __init__(self, duck_id: str, owner_id: int):
        super().__init__(timeout=60)
        self.duck_id = duck_id
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="Confirm Delete", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        duck = duck_index.pop(self.duck_id, None)
        if not duck:
            return await interaction.response.edit_message(content="That duck no longer exists.", view=None)

        affected = 0
        for rec in duck_users.values():
            if self.duck_id in rec.get("collection", []):
                rec["collection"].remove(self.duck_id)
                affected += 1
        await save_duck_state()

        await interaction.response.edit_message(
            content=(
                f"🗑️ **{duck['title']}** has been permanently removed from the system, "
                f"including {affected} member collection(s) that had it."
            ),
            view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled — nothing was removed.", view=None)


class DuckRemoveModal(discord.ui.Modal, title="Remove a Duck"):
    duck_title = discord.ui.TextInput(label="Duck Title (exact)", max_length=100)

    async def on_submit(self, interaction: discord.Interaction):
        duck_id = slugify(str(self.duck_title))
        duck = duck_index.get(duck_id)
        if not duck:
            return await interaction.response.send_message(
                f"No duck found matching **{self.duck_title}**.", ephemeral=True
            )
        await interaction.response.send_message(
            f"Are you sure you want to permanently delete {duck['emoji']} **{duck['title']}**?\n"
            f"This removes it from the index **and** from every member's collection who owns it. "
            f"This can't be undone.",
            view=ConfirmRemoveView(duck_id, interaction.user.id),
            ephemeral=True,
        )


class ConfirmClearPoolView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=60)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="Confirm Clear Pool", style=discord.ButtonStyle.danger, emoji="🧹")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        count = 0
        for duck in duck_index.values():
            if duck.get("active"):
                duck["active"] = False
                duck["limited_until"] = None
                count += 1
        await save_duck_state()
        await interaction.response.edit_message(
            content=f"🧹 Pool cleared — {count} duck(s) deactivated (still in the index, nothing deleted).",
            view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled — nothing was changed.", view=None)


class DuckActivateModal(discord.ui.Modal):
    duck_titles = discord.ui.TextInput(
        label="Duck Title(s)",
        style=discord.TextStyle.paragraph,
        placeholder="One per line, or comma-separated, e.g.\nGolden Duck\nIce Duck, Fire Duck",
        max_length=2000,
    )

    def __init__(self, activate: bool):
        super().__init__(title="Activate Ducks" if activate else "Deactivate Ducks")
        self.activate = activate

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.duck_titles)
        titles = [t.strip() for line in raw.split("\n") for t in line.split(",")]
        titles = [t for t in titles if t]

        found, missing = [], []
        for title in titles:
            duck = duck_index.get(slugify(title))
            if not duck:
                missing.append(title)
                continue
            duck["active"] = self.activate
            duck["limited_until"] = None
            found.append(duck["title"])

        if found:
            await save_duck_state()

        state = "earnable" if self.activate else "no longer earnable"
        lines = []
        if found:
            lines.append(f"✅ Now {state}: " + ", ".join(found))
        if missing:
            lines.append("⚠️ Not found in the index: " + ", ".join(missing))
        await interaction.response.send_message("\n".join(lines) or "Nothing to do.", ephemeral=True)


class DuckLimitedModal(discord.ui.Modal, title="Limited-Time Duck"):
    duck_title = discord.ui.TextInput(label="Duck Title (exact)", max_length=100)
    hours = discord.ui.TextInput(label="Hours Active", placeholder="e.g. 48", max_length=10)

    async def on_submit(self, interaction: discord.Interaction):
        duck_id = slugify(str(self.duck_title))
        duck = duck_index.get(duck_id)
        if not duck:
            return await interaction.response.send_message(
                f"No duck found matching **{self.duck_title}**.", ephemeral=True
            )
        try:
            hours_val = int(str(self.hours))
            if hours_val < 1:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(
                "Hours must be a whole number of at least 1.", ephemeral=True
            )
        duck["active"] = True
        duck["limited_until"] = time.time() + hours_val * 3600
        await save_duck_state()
        await interaction.response.send_message(
            f"{duck['emoji']} **{duck['title']}** is earnable for the next **{hours_val} hour(s)**.",
            ephemeral=True,
        )


class HatchToggleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Turn On", style=discord.ButtonStyle.success)
    async def turn_on(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = str(interaction.guild.id)
        duck_config.setdefault(guild_id, {})["hatching_enabled"] = True
        await save_duck_state()
        await interaction.response.edit_message(content="Egg drops are now **On**.", view=None)

    @discord.ui.button(label="Turn Off", style=discord.ButtonStyle.danger)
    async def turn_off(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = str(interaction.guild.id)
        duck_config.setdefault(guild_id, {})["hatching_enabled"] = False
        await save_duck_state()
        await interaction.response.edit_message(content="Egg drops are now **Off**.", view=None)


class EggChannelSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.select = discord.ui.ChannelSelect(
            placeholder="Choose a voice channel for the egg counter",
            channel_types=[discord.ChannelType.voice],
            min_values=1,
            max_values=1,
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        channel = self.select.values[0]
        guild_id = str(interaction.guild.id)
        duck_config.setdefault(guild_id, {})["egg_counter_channel_id"] = channel.id
        await save_duck_state()
        try:
            resolved = await channel.fetch()
            await resolved.edit(name=format_egg_channel_name(duck_stats.get("total_dropped", 0)))
        except Exception as e:
            print(f"[duck_system] initial egg counter rename failed: {e}")
        await interaction.response.edit_message(
            content=f"Egg counter channel set to {channel.mention}. It refreshes every "
                    f"{EGG_COUNTER_UPDATE_MINUTES} minutes.",
            view=None,
        )


class KarmaChannelSelectView(discord.ui.View):
    """Pick which text channels count for heart-reaction karma. Selecting
    zero channels means 'allow everywhere' (the default).
    """

    def __init__(self):
        super().__init__(timeout=180)
        self.select = discord.ui.ChannelSelect(
            placeholder="Channels where heart reactions grant karma (none = all channels)",
            channel_types=[discord.ChannelType.text],
            min_values=0,
            max_values=25,
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        channels = self.select.values
        guild_id = str(interaction.guild.id)
        duck_config.setdefault(guild_id, {})["karma_reaction_channel_ids"] = [c.id for c in channels]
        await save_duck_state()

        if channels:
            content = "Heart-reaction karma is now limited to: " + ", ".join(c.mention for c in channels)
        else:
            content = "Heart-reaction karma is now allowed in every channel."
        await interaction.response.edit_message(content=content, view=None)


class IndexView(discord.ui.View):
    """Lets anyone pick Currently Earnable vs Not Currently Active instead
    of cramming both into one big embed.
    """

    def __init__(self):
        super().__init__(timeout=180)
        options = [
            discord.SelectOption(label="Currently Earnable", value="active", emoji="✅"),
            discord.SelectOption(label="Not Currently Active", value="inactive", emoji="⛔"),
        ]
        select = discord.ui.Select(placeholder="Choose a category to view", options=options)
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        choice = interaction.data["values"][0]
        if choice == "active":
            ids = [d for d, v in duck_index.items() if v["active"]]
            title = "📖 Duck Index — Currently Earnable"
        else:
            ids = [d for d, v in duck_index.items() if not v["active"]]
            title = "📖 Duck Index — Not Currently Active"

        content = format_grouped_row(group_by_rarity(ids)) or "Nothing in this category."
        embed = discord.Embed(title=title, description=content, color=discord.Color.blurple())
        await interaction.response.edit_message(embed=embed, view=self)


class EditorView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        options = [
            discord.SelectOption(label="Add Duck", value="add", emoji="➕"),
            discord.SelectOption(label="Edit Duck Emoji", value="edit", emoji="✏️"),
            discord.SelectOption(label="Remove Duck", value="remove", emoji="🗑️"),
            discord.SelectOption(label="Activate Duck", value="on", emoji="✅"),
            discord.SelectOption(label="Deactivate Duck", value="off", emoji="⛔"),
            discord.SelectOption(label="Clear Pool", value="clear_pool", emoji="🧹"),
            discord.SelectOption(label="Limited-Time Duck", value="limited", emoji="⏳"),
            discord.SelectOption(label="Toggle Egg Drops", value="toggle", emoji="🥚"),
            discord.SelectOption(label="Set Egg Counter Channel", value="counter", emoji="🔢"),
            discord.SelectOption(label="Set Karma Reaction Channels", value="karma_channels", emoji="😇"),
        ]
        select = discord.ui.Select(placeholder="Choose an action", options=options)
        select.callback = self.on_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "You do not have permission to use this.", ephemeral=True
            )
            return False
        return True

    async def on_select(self, interaction: discord.Interaction):
        choice = interaction.data["values"][0]
        if choice == "add":
            await interaction.response.send_modal(DuckAddModal())
        elif choice == "edit":
            await interaction.response.send_modal(DuckEditModal())
        elif choice == "remove":
            await interaction.response.send_modal(DuckRemoveModal())
        elif choice == "on":
            await interaction.response.send_modal(DuckActivateModal(activate=True))
        elif choice == "off":
            await interaction.response.send_modal(DuckActivateModal(activate=False))
        elif choice == "clear_pool":
            await interaction.response.send_message(
                "Deactivate every currently active duck? They stay in the index, just leave the earnable pool.",
                view=ConfirmClearPoolView(interaction.user.id),
                ephemeral=True,
            )
        elif choice == "limited":
            await interaction.response.send_modal(DuckLimitedModal())
        elif choice == "toggle":
            await interaction.response.send_message(
                "Turn egg drops on or off:", view=HatchToggleView(), ephemeral=True
            )
        elif choice == "counter":
            await interaction.response.send_message(
                "Pick the voice channel to use as the live egg counter:",
                view=EggChannelSelectView(),
                ephemeral=True,
            )
        elif choice == "karma_channels":
            await interaction.response.send_message(
                "Pick which channels count for heart-reaction karma:",
                view=KarmaChannelSelectView(),
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
        self.update_egg_counter_channels.start()

        # Prime the invite-use snapshot BEFORE any joins happen — without
        # this, the first join after every restart would look like it used
        # every invite at once (old count defaults to 0).
        for guild in self.bot.guilds:
            try:
                invites = await guild.invites()
                invite_cache[guild.id] = {inv.code: inv.uses for inv in invites}
            except Exception as e:
                print(f"[duck_system] initial invite cache failed for {guild.name}: {e}")

    async def cog_unload(self):
        self.check_expired_limited.cancel()
        self.update_egg_counter_channels.cancel()

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

    # ---------- daily egg, GM karma, welcome karma, invite karma, passive drop ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        discord_id = str(message.author.id)
        guild_id = str(message.guild.id)
        content = message.content or ""
        rec = get_user_record(discord_id)
        today_str = time.strftime("%Y-%m-%d", time.gmtime())
        state_changed = False

        # --- Daily egg: silent, first message after the 00:00 UTC reset ---
        if rec.get("last_daily_egg_date") != today_str:
            rec["last_daily_egg_date"] = today_str
            rec["inventory"] += 1
            state_changed = True

        # --- GM karma: once per day, "GM"/"good morning" anywhere in the message ---
        if rec.get("last_gm_date") != today_str and GM_PATTERN.search(content):
            rec["last_gm_date"] = today_str
            award_karma(discord_id, 1)
            state_changed = True

        # --- Welcome karma: saying 'welcome' anywhere, within 30 min of a pending join ---
        # No @mention required — just the word. Every distinct person who
        # says it gets their own point (capped per-person-per-join, so the
        # same person can't farm it by repeating "welcome"). If several
        # joins are pending, the oldest one this author hasn't already
        # welcomed gets credited.
        if WELCOME_PATTERN.search(content):
            now = time.time()
            candidate_id = None
            for member_id, info in recent_joins.items():
                if message.author.id in info["welcomed_by"]:
                    continue
                if member_id == message.author.id:
                    continue
                if now - info["joined_at"] > WELCOME_WINDOW_SECONDS:
                    continue
                if candidate_id is None or info["joined_at"] < recent_joins[candidate_id]["joined_at"]:
                    candidate_id = member_id
            if candidate_id is not None:
                recent_joins[candidate_id]["welcomed_by"].add(message.author.id)
                award_karma(discord_id, 1)
                state_changed = True

        # --- Invite karma: this is the new member's first message since joining ---
        pending_inviter = pending_invite_karma.pop(discord_id, None)
        if pending_inviter:
            award_karma(pending_inviter, 1)
            state_changed = True

        if state_changed:
            await save_duck_state()

        # --- Passive egg drop (separate toggle: /editor → Toggle Egg Drops) ---
        if not duck_config.get(guild_id, {}).get("hatching_enabled", True):
            return

        now = time.time()
        if now - rec.get("last_roll_check_ts", 0) < DROP_COOLDOWN_SECONDS:
            return
        rec["last_roll_check_ts"] = now  # kept in-memory only — not worth a DB write on every message

        if random.random() >= DROP_CHANCE:
            return
        if roll_duck_id() is None:
            return  # pool is empty, nothing to give right now

        view = EggDropView(message.author.id)
        try:
            sent = await message.channel.send(
                content=f"🥚 {message.author.mention} found an egg on the ground!",
                view=view,
            )
            view.message = sent
            duck_stats["total_dropped"] = duck_stats.get("total_dropped", 0) + 1
            await save_duck_state()
        except Exception as e:
            print(f"[duck_system] failed to post egg drop: {e}")

    # ---------- welcome/invite tracking: new member joins ----------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return

        recent_joins[member.id] = {"joined_at": time.time(), "welcomed_by": set()}

        guild = member.guild
        try:
            new_invites = await guild.invites()
        except Exception as e:
            print(f"[duck_system] couldn't fetch invites for {guild.name} (needs Manage Server): {e}")
            return

        old_counts = invite_cache.get(guild.id, {})
        inviter_id = None
        for inv in new_invites:
            if inv.uses > old_counts.get(inv.code, 0):
                inviter_id = inv.inviter.id if inv.inviter else None
                break
        invite_cache[guild.id] = {inv.code: inv.uses for inv in new_invites}

        if inviter_id and inviter_id != member.id:
            pending_invite_karma[str(member.id)] = str(inviter_id)
            await save_duck_state()

    # ---------- heart-reaction karma ----------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return
        if payload.member is not None and payload.member.bot:
            return
        if payload.emoji.id != HEART_EMOJI_ID:
            return

        allowed_channels = duck_config.get(str(payload.guild_id), {}).get("karma_reaction_channel_ids")
        if allowed_channels and payload.channel_id not in allowed_channels:
            return

        message_key = str(payload.message_id)
        already_credited = reaction_karma_granted.setdefault(message_key, set())
        if payload.user_id in already_credited:
            return  # this specific person already earned their point here — no farming via remove/re-add

        posted_at = discord.utils.snowflake_time(payload.message_id)
        if (discord.utils.utcnow() - posted_at).total_seconds() > REACTION_KARMA_WINDOW_SECONDS:
            return

        try:
            channel = self.bot.get_channel(payload.channel_id) or await self.bot.fetch_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
        except Exception:
            return

        if message.author.id == payload.user_id:
            return  # no self-karma for reacting to your own post

        already_credited.add(payload.user_id)
        award_karma(str(payload.user_id), 1)  # the REACTOR earns the point
        await save_duck_state()

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.emoji.id != HEART_EMOJI_ID:
            return

        message_key = str(payload.message_id)
        credited = reaction_karma_granted.get(message_key)
        if not credited or payload.user_id not in credited:
            return  # this person never earned a point here

        credited.discard(payload.user_id)
        remove_karma(str(payload.user_id), 1)  # take the point back from the reactor who un-reacted
        await save_duck_state()

    # ---------- staff: single consolidated config/management command ----------

    @app_commands.command(name="editor", description="Staff: manage ducks, activation, and settings from one menu.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def editor(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "**🛠️ Duck System Editor**\nChoose an action below:",
            view=EditorView(),
            ephemeral=True,
        )

    # ---------- background: expire limited-time ducks ----------

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

        # Housekeeping: drop join records once their welcome window has passed.
        stale_joins = [mid for mid, info in recent_joins.items() if now - info["joined_at"] > WELCOME_WINDOW_SECONDS]
        for mid in stale_joins:
            recent_joins.pop(mid, None)

        # Housekeeping: drop reaction-tracking for messages past the karma window
        # (derived from the message's own snowflake timestamp — no extra API calls).
        now_dt = discord.utils.utcnow()
        stale_reactions = [
            mkey for mkey in reaction_karma_granted
            if (now_dt - discord.utils.snowflake_time(int(mkey))).total_seconds() > REACTION_KARMA_WINDOW_SECONDS
        ]
        for mkey in stale_reactions:
            reaction_karma_granted.pop(mkey, None)

    @check_expired_limited.before_loop
    async def before_check_expired_limited(self):
        await self.bot.wait_until_ready()

    # ---------- background: keep the egg counter VC name in sync ----------

    @tasks.loop(minutes=EGG_COUNTER_UPDATE_MINUTES)
    async def update_egg_counter_channels(self):
        desired_name = format_egg_channel_name(duck_stats.get("total_dropped", 0))
        for guild_id, config in duck_config.items():
            channel_id = config.get("egg_counter_channel_id")
            if not channel_id:
                continue
            channel = self.bot.get_channel(channel_id)
            if not channel or channel.name == desired_name:
                continue
            try:
                await channel.edit(name=desired_name)
            except Exception as e:
                print(f"[duck_system] failed to rename egg counter channel: {e}")

    @update_egg_counter_channels.before_loop
    async def before_update_egg_counter_channels(self):
        await self.bot.wait_until_ready()

    # ---------- public: view pool / index / collection ----------

    @app_commands.command(name="hatchpool", description="View the current earnable duck pool.")
    async def hatchpool(self, interaction: discord.Interaction):
        active_ids = [d for d, v in duck_index.items() if v["active"]]
        content = format_grouped_plain(group_by_rarity(active_ids)) or "The pool is currently empty."
        embed = discord.Embed(title="🥚 Current Hatch Pool", description=content, color=discord.Color.gold())
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="index", description="View every duck that has ever been added.")
    async def index_cmd(self, interaction: discord.Interaction):
        active_ids = [d for d, v in duck_index.items() if v["active"]]
        inactive_ids = [d for d, v in duck_index.items() if not v["active"]]

        embed = discord.Embed(title="📖 Duck Index", color=discord.Color.blurple())

        if not duck_index:
            embed.description = "No ducks have been added yet."
        else:
            sections = []
            if active_ids:
                sections.append("### Currently Earnable\n" + format_grouped_row(group_by_rarity(active_ids)))
            if inactive_ids:
                sections.append("### Not Currently Active\n" + format_grouped_row(group_by_rarity(inactive_ids)))
            embed.description = "\n\n".join(sections)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="collection", description="View your (or someone else's) duck collection.")
    @app_commands.describe(member="Whose collection to view")
    async def collection_cmd(self, interaction: discord.Interaction, member: discord.Member = None):
        target = member or interaction.user
        discord_id = str(target.id)
        rec = duck_users.get(discord_id, default_user_record())

        content = format_flat_row(rec["collection"]) or "No ducks collected yet."
        role_color = target.color if target.color.value != 0 else discord.Color.teal()
        embed = discord.Embed(title=f"🦆 {target.display_name}'s Collection", description=content, color=role_color)
        embed.set_footer(text=f"{len(rec['collection'])}/{len(duck_index)} collected")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="inventory", description="Check how many eggs you have in storage.")
    async def duckinventory_cmd(self, interaction: discord.Interaction):
        discord_id = str(interaction.user.id)
        rec = get_user_record(discord_id)
        await interaction.response.send_message(
            f"🥚 You have **{rec['inventory']}** egg(s) stored.\n"
            f"😇 Karma {rec.get('karma', 0)}/{KARMA_PER_EGG}",
            ephemeral=True,
        )

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
