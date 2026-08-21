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
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks


###############################################
#                CONSTANTS                   #
###############################################

RARITY_ORDER = ["common", "rare", "legendary", "divine", "secret", "quackpot"]

RARITY_WEIGHTS = {
    "common": 60.0,
    "rare": 25.0,
    "legendary": 10.0,
    "divine": 3.0,
    "secret": 1.5,
    "quackpot": 0.5,
}

RARITY_DISPLAY = {
    "common": "Common",
    "rare": "Rare",
    "legendary": "Legendary",
    "divine": "Divine",
    "secret": "Secret",
    "quackpot": "Quackpot",
}

DUPLICATE_BONUS_CHANCE = 0.15  # 15% extra egg on a duplicate hatch
DROP_COOLDOWN_SECONDS = 60     # 1 minute between roll attempts, per user
CLAIM_TIMEOUT_SECONDS = 600    # 10 minutes before an unclaimed drop expires
EGG_COUNTER_UPDATE_MINUTES = 5  # how often the VC name is allowed to refresh

# --- Environment: today's drop odds fluctuate, picked once per day ---
ENVIRONMENT_MIN_CHANCE = 6.0
ENVIRONMENT_MAX_CHANCE = 15.0
# Triangular distribution peaked at the low end — most days land near 6%,
# 15% is a genuinely rare high.

# --- Eggs per drop: 1-5, weighted using the same odds as rarity tiers,
# with common+rare combined into one bucket to make exactly 5 groups. ---
EGG_COUNT_WEIGHTS = [
    (1, RARITY_WEIGHTS["common"] + RARITY_WEIGHTS["rare"]),  # 85 — typical
    (2, RARITY_WEIGHTS["legendary"]),                          # 10
    (3, RARITY_WEIGHTS["divine"]),                             # 3
    (4, RARITY_WEIGHTS["secret"]),                             # 1.5
    (5, RARITY_WEIGHTS["quackpot"]),                           # 0.5 — matches "rare 5-egg" chance
]

# --- Karma ---
HEART_EMOJI_ID = 1295255068483784786  # <:D_ZLove:...>
WAVE_EMOJI = "👋"  # unicode, not a custom emoji — matched by name, not ID
KARMA_PER_EGG = 10
REACTION_KARMA_WINDOW_SECONDS = 60 * 60   # heart/wave reactions must land within 1 hour of the post
WELCOME_WINDOW_SECONDS = 30 * 60          # welcome must happen within 30 min of the join
GM_PATTERN = re.compile(r"\b(gm|good\s?morning)\b", re.IGNORECASE)
WELCOME_PATTERN = re.compile(r"\bwelcome\b", re.IGNORECASE)

# Old internal rarity keys, kept only so load_duck_state() can migrate any
# ducks saved before this rename. Nothing else in the file should ever
# reference these two strings again.
LEGACY_RARITY_RENAMES = {"mythic": "divine", "ghost": "quackpot"}


def format_rarity_percent(r: str) -> str:
    w = RARITY_WEIGHTS[r]
    return f"{int(w)}%" if w == int(w) else f"{w}%"


def rarity_header(r: str) -> str:
    return f"{RARITY_DISPLAY[r]} ({format_rarity_percent(r)})"


# How much of a "win" a hatch announcement should feel like, scaled to how
# hard the rarity actually is to get. Common barely registers; Quackpot
# gets the full fanfare treatment.
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
    "divine": {
        "color": discord.Color.purple(),
        "banner": "💥 **{emoji} {mention} HATCHED A {title}!!** 💥",
    },
    "secret": {
        "color": discord.Color.red(),
        "banner": "🎇🎇 **{emoji} {mention} UNCOVERED THE SECRET {title}!!** 🎇🎇",
    },
    "quackpot": {
        "color": discord.Color.dark_purple(),
        "banner": (
            "👻═══════════👻\n"
            "**{emoji} {mention} HATCHED THE QUACKPOT DUCK — {title}!!!**\n"
            "👻═══════════👻"
        ),
    },
}


###############################################
#          STATE (Postgres-backed)           #
###############################################
# duck_index[duck_id] = {
#   "title": str, "emoji": str, "rarity": str,
#   "active": bool, "limited_until": float | None (unix timestamp),
#   "is_error": bool  — ERROR:404 flag. Keeps its normal "rarity" for
#   display purposes (so /collection groups it exactly where it'd
#   otherwise belong), but rolls against a separate hidden pool instead
#   of its assigned tier's odds, and is excluded from /hatchpool and
#   /index entirely. Never settable at creation — only via /error.
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

# redeem_codes[code] = {
#   "eggs": int, "duck_ids": [duck_id, ...],
#   "expires_at": float | None, "redeemed_by": [discord_id, ...]
# }
redeem_codes = {}

# nest_state[guild_id] = {
#   "pool_total": int (always starts at 8),
#   "entries": {discord_id: eggs_donated_this_cycle},
#   "last_reset_week": "2026-W32" | None  — guards against double-firing
# }
NEST_BASELINE = 8
nest_state = {}


def get_nest_state(guild_id: str) -> dict:
    state = nest_state.setdefault(guild_id, {"pool_total": NEST_BASELINE, "entries": {}, "last_reset_week": None})
    state.setdefault("pool_total", NEST_BASELINE)
    state.setdefault("entries", {})
    state.setdefault("last_reset_week", None)
    return state


def format_nest_countdown() -> str:
    """Short-form time remaining until the next nest draw (Saturday 00:00 UTC)."""
    now = datetime.now(timezone.utc)
    days_until_sat = (5 - now.weekday()) % 7  # Saturday = weekday 5
    next_reset = (now + timedelta(days=days_until_sat)).replace(hour=0, minute=0, second=0, microsecond=0)
    if next_reset <= now:
        next_reset += timedelta(days=7)

    delta = next_reset - now
    days = delta.days
    hours = delta.seconds // 3600

    if days > 0:
        return f"{days} day{'s' if days != 1 else ''}, {hours} hour{'s' if hours != 1 else ''}"
    return f"{hours} hour{'s' if hours != 1 else ''}"


def format_limited_remaining(limited_until: float | None) -> str:
    """Simple remaining time for a limited duck, e.g. '(2 days, 5 hours)'."""
    if not limited_until:
        return ""
    remaining = limited_until - time.time()
    if remaining <= 0:
        return " (expired)"
    days = int(remaining // 86400)
    hours = int((remaining % 86400) // 3600)
    if days > 0:
        return f" ({days} day{'s' if days != 1 else ''}, {hours} hour{'s' if hours != 1 else ''})"
    return f" ({hours} hour{'s' if hours != 1 else ''})"


# environment_state = {"date": "2026-08-16", "drop_chance_percent": 8.42, "egg_count": 1}
# Global, not per-guild — one "weather" for the whole bot. Both values are
# re-rolled together, once per UTC day, the first time anything checks
# that day (a message, or someone running /weather).
environment_state = {
    "date": None,
    "drop_chance_percent": ENVIRONMENT_MIN_CHANCE,
    "egg_count": 1,
}


def _roll_egg_count() -> int:
    counts = [c for c, _ in EGG_COUNT_WEIGHTS]
    weights = [w for _, w in EGG_COUNT_WEIGHTS]
    return random.choices(counts, weights=weights, k=1)[0]


async def ensure_environment_for_today() -> dict:
    today_str = time.strftime("%Y-%m-%d", time.gmtime())
    if environment_state.get("date") != today_str:
        chance = round(
            random.triangular(ENVIRONMENT_MIN_CHANCE, ENVIRONMENT_MAX_CHANCE, ENVIRONMENT_MIN_CHANCE), 2
        )
        environment_state["date"] = today_str
        environment_state["drop_chance_percent"] = chance
        environment_state["egg_count"] = _roll_egg_count()
        await save_duck_state()
    return environment_state


# --- In-memory only (short-lived windows, fine to lose on restart) ---
# reaction_karma_granted[str(message_id)] = {
#   "author_id": int,          # message author (receiver of the heart)
#   "reactors": set[user_id],  # who already earned credit for reacting
# }
# Each distinct reactor grants 1 karma to themselves and 1 to the author.
# Re-adding the same reaction after removal does not farm extra points.
reaction_karma_granted = {}
# wave_karma_granted — same shape as reaction_karma_granted, but tracked
# separately since it's a different trigger (👋 in the intro channel).
wave_karma_granted = {}
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
        "favorites": [],
    }


def get_user_record(discord_id: str) -> dict:
    """Like duck_users.setdefault(), but also backfills new fields onto
    older records that were saved before this feature existed.
    """
    rec = duck_users.setdefault(discord_id, default_user_record())
    rec.setdefault("last_daily_egg_date", None)
    rec.setdefault("last_gm_date", None)
    rec.setdefault("karma", 0)
    rec.setdefault("favorites", [])
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


def add_eggs(discord_id: str, amount: int):
    """For external callers (e.g. the Fortnite side rewarding weekly
    leaderboard winners) — doesn't save on its own, caller should
    await save_duck_state() once after making all its changes.
    """
    rec = get_user_record(discord_id)
    rec["inventory"] += amount


# Set once in DuckCog.cog_load() from bot.db_pool — avoids any circular
# import back into the main bot file.
_pool = None


async def load_duck_state():
    global duck_index, duck_users, duck_config, duck_stats, pending_invite_karma, redeem_codes, nest_state, environment_state
    if _pool is None:
        return
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value FROM bot_state WHERE key IN "
                "('duck_index','duck_users','duck_config','duck_stats','duck_pending_invites',"
                "'duck_redeem_codes','duck_nest_state','duck_environment')"
            )
            data = {row["key"]: json.loads(row["value"]) for row in rows}
            duck_index = data.get("duck_index", {})
            duck_users = data.get("duck_users", {})
            duck_config = data.get("duck_config", {})
            duck_stats = data.get("duck_stats", {"total_dropped": 0})
            pending_invite_karma = data.get("duck_pending_invites", {})
            redeem_codes = data.get("duck_redeem_codes", {})
            nest_state = data.get("duck_nest_state", {})
            environment_state = data.get(
                "duck_environment",
                {"date": None, "drop_chance_percent": ENVIRONMENT_MIN_CHANCE, "egg_count": 1},
            )
            environment_state.setdefault("egg_count", 1)  # backfill for saves from before this field existed
            print(f"[duck_db] loaded {len(duck_index)} duck(s), {len(duck_users)} user record(s)")

        # One-time migration: fix any ducks saved under the old rarity
        # keys ('mythic'/'ghost') before this rename.
        migrated = 0
        for duck in duck_index.values():
            old_rarity = duck.get("rarity")
            if old_rarity in LEGACY_RARITY_RENAMES:
                duck["rarity"] = LEGACY_RARITY_RENAMES[old_rarity]
                migrated += 1
        if migrated:
            print(f"[duck_db] migrated {migrated} duck(s) off legacy rarity names")
            await save_duck_state()
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
                    ("duck_redeem_codes", json.dumps(redeem_codes)),
                    ("duck_nest_state", json.dumps(nest_state)),
                    ("duck_environment", json.dumps(environment_state)),
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


async def fav_duck_autocomplete(interaction: discord.Interaction, current: str):
    """Suggests only ducks the requester actually owns, matched against
    what they've typed so far — avoids typos entirely and scales past
    Discord's 25-option select limit since it's computed per-keystroke.
    """
    discord_id = str(interaction.user.id)
    rec = duck_users.get(discord_id, default_user_record())
    owned_ids = rec.get("collection", [])

    current_lower = current.lower()
    matches = []
    for duck_id in owned_ids:
        duck = duck_index.get(duck_id)
        if duck and current_lower in duck["title"].lower():
            matches.append(duck["title"])

    return [app_commands.Choice(name=title, value=title) for title in matches[:25]]


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


ERROR_WEIGHT = 0.05  # ERROR:404's odds — a hidden 7th pool, shared across however many error ducks are active


def get_active_ducks_by_rarity():
    """Active, non-error ducks only, grouped by their display rarity."""
    by_rarity = {r: [] for r in RARITY_ORDER}
    for duck_id, duck in duck_index.items():
        if duck.get("active") and not duck.get("is_error"):
            by_rarity[duck["rarity"]].append(duck_id)
    return by_rarity


def get_active_error_ducks():
    """Active ERROR:404 ducks, regardless of their (hidden) display rarity."""
    return [d for d, duck in duck_index.items() if duck.get("active") and duck.get("is_error")]


def roll_duck_id():
    """Pick one active duck, weighted by rarity. Rarity tiers with zero
    currently-active ducks are excluded and the remaining weights are used
    as-is (random.choices renormalizes automatically). ERROR:404 ducks are
    rolled against a separate hidden pool at ERROR_WEIGHT, layered on top
    of the normal tiers rather than replacing any of them. Returns None if
    the pool is completely empty.
    """
    by_rarity = get_active_ducks_by_rarity()
    error_ducks = get_active_error_ducks()

    available_tiers = [r for r in RARITY_ORDER if by_rarity[r]]
    weights = [RARITY_WEIGHTS[r] for r in available_tiers]

    if error_ducks:
        available_tiers = available_tiers + ["_error"]
        weights = weights + [ERROR_WEIGHT]

    if not available_tiers:
        return None

    chosen = random.choices(available_tiers, weights=weights, k=1)[0]
    if chosen == "_error":
        return random.choice(error_ducks)
    return random.choice(by_rarity[chosen])


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


def build_collection_body(owned, favorites, choice: str = "_all") -> str:
    """Splits the current filtered view into a Favorites section (if any)
    and the rest, so a favorited duck never shows twice.
    """
    if choice == "_all":
        ids = owned
        section_label = "Collection"
    else:
        ids = [d for d in owned if duck_index.get(d, {}).get("rarity") == choice]
        section_label = RARITY_DISPLAY.get(choice, choice)

    if not ids:
        return "No ducks in this view yet."

    fav_ids = [d for d in favorites if d in ids]
    rest_ids = [d for d in ids if d not in fav_ids]

    parts = []
    if fav_ids:
        parts.append("### Favorites\n" + format_flat_row(fav_ids))
    if rest_ids:
        parts.append(f"### {section_label}\n" + format_flat_row(rest_ids))

    return "\n\n".join(parts)


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


def format_grouped_plain(active_ids, owned_ids: set | None = None) -> str:
    """Single-column style for /hatchpool: bold rarity header, one
    'emoji title' line per duck. Limited ducks append a short remaining-time tag.

    Owned ducks are shown as small text (-#); missing ducks are bolded.
    """
    if owned_ids is None:
        owned_ids = set()

    grouped_ids = {r: [] for r in RARITY_ORDER}
    for duck_id in active_ids:
        duck = duck_index.get(duck_id)
        if duck:
            grouped_ids[duck["rarity"]].append(duck_id)

    lines = []
    for r in RARITY_ORDER:
        ids = grouped_ids[r]
        if not ids:
            continue
        lines.append(f"**{rarity_header(r)}**")
        for duck_id in ids:
            duck = duck_index[duck_id]
            entry = f"{duck['emoji']} {duck['title']}"
            entry += format_limited_remaining(duck.get("limited_until"))
            if duck_id in owned_ids:
                lines.append(f"-# {entry}")
            else:
                lines.append(f"**{entry}**")
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

    def __init__(self, owner_id: int, egg_count: int = 1):
        super().__init__(timeout=CLAIM_TIMEOUT_SECONDS)
        self.owner_id = owner_id
        self.egg_count = egg_count
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This egg isn't yours!", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Hatch", style=discord.ButtonStyle.success, emoji="🐣")
    async def hatch(self, interaction: discord.Interaction, button: discord.ui.Button):
        discord_id = str(interaction.user.id)

        if self.egg_count == 1:
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
            return

        # Multiple eggs at once — grouped bulk-style list, same as /hatch.
        pre_existing = set(get_user_record(discord_id)["collection"])
        results = []
        for _ in range(self.egg_count):
            r = resolve_hatch(discord_id)
            if r is None:
                break
            results.append(r)

        await save_duck_state()
        self.stop()

        if not results:
            return await interaction.response.edit_message(
                content="The pool is empty right now — nothing to hatch. Sorry, eggs gone!",
                embed=None,
                view=None,
            )

        grouped = {}
        for r in results:
            g = grouped.setdefault(r["duck_id"], {
                "emoji": r["emoji"], "title": r["title"], "rarity": r["rarity"], "count": 0,
            })
            g["count"] += 1

        lines = [f"🥚 {interaction.user.mention} hatched {len(results)} egg(s):"]
        for duck_id, g in grouped.items():
            status_tag = " (duplicate)" if duck_id in pre_existing else " (**NEW**)"
            lines.append(
                f"{g['emoji']} **{g['title']}** (x{g['count']}) - "
                f"{RARITY_DISPLAY[g['rarity']]}{status_tag}"
            )

        await interaction.response.edit_message(content="\n".join(lines), embed=None, view=None)

    @discord.ui.button(label="Inventory", style=discord.ButtonStyle.secondary, emoji="🎒")
    async def store(self, interaction: discord.Interaction, button: discord.ui.Button):
        discord_id = str(interaction.user.id)
        rec = duck_users.setdefault(discord_id, default_user_record())
        rec["inventory"] += self.egg_count
        await save_duck_state()
        self.stop()
        egg_word = "egg" if self.egg_count == 1 else "eggs"
        await interaction.response.edit_message(
            content=f"🎒 {interaction.user.mention} stored {self.egg_count} {egg_word}! Inventory: **{rec['inventory']}**",
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
                "is_error": False,
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


class DuckToggleActiveModal(discord.ui.Modal, title="Toggle Duck Activation"):
    """One combined toggle, like /fav — each listed duck flips based on
    ITS OWN current state independently, not a single batch direction.
    Type 3 titles where 1 is already active and 2 aren't: the active one
    deactivates, the two inactive ones activate, all in the same submit.
    The optional Hours field only applies to ducks that go inactive→active
    in this action; deactivating always clears any limited-time expiry.
    """
    duck_titles = discord.ui.TextInput(
        label="Duck Title(s)",
        style=discord.TextStyle.paragraph,
        placeholder="One per line, or comma-separated, e.g.\nGolden Duck\nIce Duck, Fire Duck",
        max_length=2000,
    )
    hours = discord.ui.TextInput(
        label="Limited Hours (optional)",
        required=False,
        max_length=10,
        placeholder="Leave blank for a normal, non-expiring activation",
    )

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.duck_titles)
        titles = [t.strip() for line in raw.split("\n") for t in line.split(",")]
        titles = [t for t in titles if t]

        hours_raw = str(self.hours).strip()
        hours_val = None
        if hours_raw:
            try:
                hours_val = float(hours_raw)
                if hours_val <= 0:
                    raise ValueError
            except ValueError:
                return await interaction.response.send_message(
                    "❌ Hours must be a positive number.", ephemeral=True
                )

        activated, deactivated, missing = [], [], []
        for title in titles:
            duck = duck_index.get(slugify(title))
            if not duck:
                missing.append(title)
                continue

            if duck["active"]:
                duck["active"] = False
                duck["limited_until"] = None
                deactivated.append(duck["title"])
            else:
                duck["active"] = True
                duck["limited_until"] = time.time() + hours_val * 3600 if hours_val else None
                activated.append(duck["title"])

        if activated or deactivated:
            await save_duck_state()

        lines = []
        if activated:
            suffix = f" (limited, {hours_val}h)" if hours_val else ""
            lines.append(f"✅ Activated{suffix}: " + ", ".join(activated))
        if deactivated:
            lines.append("⛔ Deactivated: " + ", ".join(deactivated))
        if missing:
            lines.append("⚠️ Not found in the index: " + ", ".join(missing))
        await interaction.response.send_message("\n".join(lines) or "Nothing to do.", ephemeral=True)


class ErrorToggleModal(discord.ui.Modal, title="Toggle ERROR:404 Status"):
    """Converts an existing duck to/from ERROR:404. Not creatable directly
    at /duckadd — this is the only place is_error ever gets set.
    """
    duck_title = discord.ui.TextInput(label="Duck Title (exact)", max_length=100)

    async def on_submit(self, interaction: discord.Interaction):
        duck_id = slugify(str(self.duck_title))
        duck = duck_index.get(duck_id)
        if not duck:
            return await interaction.response.send_message(
                f"No duck found matching **{self.duck_title}**.", ephemeral=True
            )

        duck["is_error"] = not duck.get("is_error", False)
        await save_duck_state()

        state_text = "marked as" if duck["is_error"] else "removed from"
        await interaction.response.send_message(
            f"{duck['emoji']} **{duck['title']}** ({RARITY_DISPLAY[duck['rarity']]}) has been "
            f"{state_text} ERROR:404.",
            ephemeral=True,
        )


class ErrorAdminView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Nothing to see here.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Toggle Error Status", style=discord.ButtonStyle.danger, emoji="🚫")
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ErrorToggleModal())


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


class NestPrizeDuckModal(discord.ui.Modal, title="Set Nest Prize Duck"):
    duck_title = discord.ui.TextInput(label="Duck Title (exact)", max_length=100)

    async def on_submit(self, interaction: discord.Interaction):
        duck_id = slugify(str(self.duck_title))
        duck = duck_index.get(duck_id)
        if not duck:
            return await interaction.response.send_message(
                f"No duck found matching **{self.duck_title}**.", ephemeral=True
            )

        guild_id = str(interaction.guild.id)
        duck_config.setdefault(guild_id, {})["nest_prize_duck_id"] = duck_id
        await save_duck_state()
        await interaction.response.send_message(
            f"{duck['emoji']} **{duck['title']}** is now the weekly Nest prize duck.",
            ephemeral=True,
        )


class NestConfirmView(discord.ui.View):
    def __init__(self, owner_id: int, amount: int):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.amount = amount

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger, emoji="🪺")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        discord_id = str(interaction.user.id)
        rec = get_user_record(discord_id)

        if rec["inventory"] < self.amount:
            return await interaction.response.edit_message(
                content=f"You only have **{rec['inventory']}** egg(s) now — donation cancelled.",
                view=None,
            )

        rec["inventory"] -= self.amount
        guild_id = str(interaction.guild.id)
        state = get_nest_state(guild_id)
        state["pool_total"] += self.amount * 2
        state["entries"][discord_id] = state["entries"].get(discord_id, 0) + self.amount
        await save_duck_state()

        my_entries = state["entries"][discord_id]
        await interaction.response.edit_message(
            content=(
                f"🪺 You donated **{self.amount}** egg(s) to the nest! The pool doubled your "
                f"contribution — it's now at **{state['pool_total']}** eggs.\n"
                f"You have **{my_entries}** entr{'y' if my_entries == 1 else 'ies'} in this week's draw."
            ),
            view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled — no eggs were donated.", view=None)


# --- Gift eggs: transit loss flavor ---
# Loss fraction is triangular on [0, 0.30] with the peak away from 0,
# so a clean 0% delivery is possible but less common than a small spill.

GIFT_LOSS_SENDER_FLAVOR = [
    "You got startled and dropped **{lost}** egg(s).",
    "A few slipped out of your hands — **{lost}** egg(s) lost.",
    "You tripped and **{lost}** egg(s) hit the ground too hard.",
    "You lost your grip; **{lost}** egg(s) didn’t make it.",
    "You sneezed mid-handoff and **{lost}** egg(s) went flying.",
    "You stacked them poorly and **{lost}** egg(s) fell off.",
    "You got distracted and **{lost}** egg(s) slipped away.",
    "Your bag tore open — **{lost}** egg(s) gone.",
]

GIFT_LOSS_RECEIVER_FLAVOR = [
    "They flinched and dropped **{lost}** egg(s).",
    "They couldn’t hold them all — **{lost}** egg(s) slipped through.",
    "They got startled and lost **{lost}** egg(s).",
    "They fumbled the catch; **{lost}** egg(s) didn’t survive.",
    "They were careless and **{lost}** egg(s) broke.",
    "They tripped while carrying them and lost **{lost}** egg(s).",
    "Their hands were full and **{lost}** egg(s) fell.",
    "They miscounted and **{lost}** egg(s) never got secured.",
]

GIFT_SAFE_FLAVOR = [
    "Everything arrived in one piece.",
    "Clean handoff — nothing lost.",
    "Full amount delivered safely.",
    "No drops, no breaks. All good.",
]


def roll_gift_loss(amount: int) -> int:
    """Return how many eggs break/sink in transit (0 .. amount)."""
    if amount <= 0:
        return 0
    # Mode at 0.15 so pure 0% is possible but not the typical result.
    frac = random.triangular(0.0, 0.30, 0.15)
    lost = int(amount * frac)
    return min(max(lost, 0), amount)


def format_gift_loss_flavor(lost: int) -> str:
    if lost <= 0:
        return random.choice(GIFT_SAFE_FLAVOR)
    template = random.choice(
        GIFT_LOSS_SENDER_FLAVOR if random.random() < 0.5 else GIFT_LOSS_RECEIVER_FLAVOR
    )
    return template.format(lost=lost)


class GiftConfirmView(discord.ui.View):
    """Giver confirms sending eggs to another member. Some may be dropped in transit."""

    def __init__(self, owner_id: int, target_id: int, amount: int):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.target_id = target_id
        self.amount = amount

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="✅ Send", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        giver_id = str(interaction.user.id)
        target_id = str(self.target_id)
        giver_rec = get_user_record(giver_id)

        if giver_rec["inventory"] < self.amount:
            return await interaction.response.edit_message(
                content=f"You only have **{giver_rec['inventory']}** egg(s) now — gift cancelled.",
                view=None,
            )

        lost = roll_gift_loss(self.amount)
        received = self.amount - lost

        giver_rec["inventory"] -= self.amount
        if received > 0:
            target_rec = get_user_record(target_id)
            target_rec["inventory"] += received

        await save_duck_state()

        flavor = format_gift_loss_flavor(lost)
        egg_word = "egg" if received == 1 else "eggs"
        sent_word = "egg" if self.amount == 1 else "eggs"

        if lost == 0:
            result = (
                f"🎁 You sent **{self.amount}** {sent_word} to <@{self.target_id}>.\n"
                f"{flavor}"
            )
        elif received == 0:
            result = (
                f"🎁 You tried to send **{self.amount}** {sent_word} to <@{self.target_id}>, "
                f"but none made it through.\n{flavor}"
            )
        else:
            result = (
                f"🎁 You sent **{self.amount}** {sent_word} to <@{self.target_id}> — "
                f"they received **{received}** {egg_word}.\n{flavor}"
            )

        await interaction.response.edit_message(content=result, view=None)

        # DM the target (best-effort)
        if received > 0:
            target = interaction.guild.get_member(self.target_id) if interaction.guild else None
            if target is None:
                try:
                    target = await interaction.client.fetch_user(self.target_id)
                except Exception:
                    target = None

            if target is not None:
                color = discord.Color.blurple()
                if isinstance(interaction.user, discord.Member) and interaction.user.color.value != 0:
                    color = interaction.user.color

                dm_body = (
                    f"# 🎁 ||{interaction.user.mention} just gifted you **{received}** {egg_word}||\n"
                    f"||{flavor}||"
                )
                embed = discord.Embed(description=dm_body, color=color)
                embed.set_footer(text="❤️ Good luck on the hatches!")
                try:
                    await target.send(embed=embed)
                except Exception:
                    pass  # DMs closed — gift still delivered to inventory

    @discord.ui.button(label="❌ Change Mind", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled — no eggs were sent.", view=None)


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


class EggBlockedChannelSelectView(discord.ui.View):
    """Pick channels where the passive egg drop can never trigger — a
    block-list, not an allow-list. Stored under a separate config key from
    karma_reaction_channel_ids, so blocking a channel here has zero effect
    on heart/wave-reaction karma in that same channel.
    """

    def __init__(self):
        super().__init__(timeout=180)
        self.select = discord.ui.ChannelSelect(
            placeholder="Channels where eggs can NEVER drop (none = allowed everywhere)",
            channel_types=[discord.ChannelType.text],
            min_values=0,
            max_values=25,
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        channels = self.select.values
        guild_id = str(interaction.guild.id)
        duck_config.setdefault(guild_id, {})["egg_blocked_channel_ids"] = [c.id for c in channels]
        await save_duck_state()

        if channels:
            content = "Egg drops are now blocked in: " + ", ".join(c.mention for c in channels)
        else:
            content = "No channels are blocked — eggs can drop anywhere."
        await interaction.response.edit_message(content=content, view=None)


class IntroChannelSelectView(discord.ui.View):
    """Pick the single channel where 👋 reactions grant karma."""

    def __init__(self):
        super().__init__(timeout=180)
        self.select = discord.ui.ChannelSelect(
            placeholder="Choose the introduction channel",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        channel = self.select.values[0]
        guild_id = str(interaction.guild.id)
        duck_config.setdefault(guild_id, {})["intro_channel_id"] = channel.id
        await save_duck_state()
        await interaction.response.edit_message(
            content=f"Introduction channel set to {channel.mention}. 👋 reactions there now grant karma.",
            view=None,
        )


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
            ids = [d for d, v in duck_index.items() if v["active"] and not v.get("is_error")]
            title = "📖 Duck Index — Currently Earnable"
        else:
            ids = [d for d, v in duck_index.items() if not v["active"] and not v.get("is_error")]
            title = "📖 Duck Index — Not Currently Active"

        content = format_grouped_row(group_by_rarity(ids)) or "Nothing in this category."
        embed = discord.Embed(title=title, description=content, color=discord.Color.blurple())
        await interaction.response.edit_message(embed=embed, view=self)


class RedeemCodeModal(discord.ui.Modal, title="Create Redeem Code"):
    code_input = discord.ui.TextInput(label="Redeem Code", max_length=50)
    expiration_input = discord.ui.TextInput(
        label="Expires In (hours, optional)",
        required=False,
        max_length=10,
        placeholder="Leave blank for no expiration",
    )
    eggs_input = discord.ui.TextInput(
        label="Egg Amount (optional)", required=False, max_length=10
    )
    ducks_input = discord.ui.TextInput(
        label="Duck Title(s) (optional)",
        required=False,
        style=discord.TextStyle.paragraph,
        placeholder="One per line, or comma-separated",
    )

    async def on_submit(self, interaction: discord.Interaction):
        code_key = str(self.code_input).strip().upper()
        if not code_key:
            return await interaction.response.send_message("❌ Code cannot be empty.", ephemeral=True)

        eggs_raw = str(self.eggs_input).strip()
        eggs_amount = 0
        if eggs_raw:
            try:
                eggs_amount = int(eggs_raw)
                if eggs_amount < 1:
                    raise ValueError
            except ValueError:
                return await interaction.response.send_message(
                    "❌ Egg amount must be a whole number of at least 1.", ephemeral=True
                )

        ducks_raw = str(self.ducks_input).strip()
        duck_ids = []
        if ducks_raw:
            titles = [t.strip() for line in ducks_raw.split("\n") for t in line.split(",")]
            titles = [t for t in titles if t]
            missing = []
            for title in titles:
                duck_id = slugify(title)
                if duck_id not in duck_index:
                    missing.append(title)
                elif duck_id not in duck_ids:
                    duck_ids.append(duck_id)
            if missing:
                return await interaction.response.send_message(
                    "❌ These duck titles weren't found in the index: " + ", ".join(missing),
                    ephemeral=True,
                )

        if eggs_amount < 1 and not duck_ids:
            return await interaction.response.send_message(
                "❌ You must set at least an egg amount or one duck as the reward.", ephemeral=True
            )

        expires_raw = str(self.expiration_input).strip()
        expires_at = None
        if expires_raw:
            try:
                hours = float(expires_raw)
                if hours <= 0:
                    raise ValueError
            except ValueError:
                return await interaction.response.send_message(
                    "❌ Expiration must be a positive number of hours.", ephemeral=True
                )
            expires_at = time.time() + hours * 3600

        redeem_codes[code_key] = {
            "eggs": eggs_amount,
            "duck_ids": duck_ids,
            "expires_at": expires_at,
            "redeemed_by": [],
        }
        await save_duck_state()

        reward_parts = []
        if eggs_amount:
            reward_parts.append(f"🥚 {eggs_amount} egg(s)")
        if duck_ids:
            names = ", ".join(duck_index[d]["title"] for d in duck_ids)
            reward_parts.append(f"🦆 {names}")

        expiry_text = f"in {expires_raw} hour(s)" if expires_at else "never"
        await interaction.response.send_message(
            f"✅ Code **{code_key}** created — grants {' + '.join(reward_parts)}. Expires: {expiry_text}\n"
            f"-# Creating a code with the same text again overwrites it, including its redemption history.",
            ephemeral=True,
        )


class RedeemModal(discord.ui.Modal, title="Redeem a Code"):
    code_input = discord.ui.TextInput(label="Enter your code", max_length=50)

    async def on_submit(self, interaction: discord.Interaction):
        code_key = str(self.code_input).strip().upper()
        entry = redeem_codes.get(code_key)

        if not entry:
            return await interaction.response.send_message("❌ That code isn't valid.", ephemeral=True)

        if entry["expires_at"] and time.time() > entry["expires_at"]:
            return await interaction.response.send_message("❌ That code has expired.", ephemeral=True)

        discord_id = str(interaction.user.id)
        if discord_id in entry["redeemed_by"]:
            return await interaction.response.send_message(
                "❌ You've already redeemed this code.", ephemeral=True
            )

        rec = get_user_record(discord_id)
        gained = []

        if entry["eggs"]:
            rec["inventory"] += entry["eggs"]
            gained.append(f"🥚 {entry['eggs']} egg(s)")

        new_ducks = []
        already_owned = []
        for duck_id in entry["duck_ids"]:
            duck = duck_index.get(duck_id)
            if not duck:
                continue  # duck was removed from the system since the code was made
            if duck_id in rec["collection"]:
                already_owned.append(duck["title"])
            else:
                rec["collection"].append(duck_id)
                new_ducks.append(f"{duck['emoji']} {duck['title']}")

        if new_ducks:
            gained.append("🦆 " + ", ".join(new_ducks))

        entry["redeemed_by"].append(discord_id)
        await save_duck_state()

        if not gained and not already_owned:
            return await interaction.response.send_message(
                "⚠️ This code's reward is no longer available (the duck(s) may have been removed).",
                ephemeral=True,
            )

        lines = ["✅ Code redeemed!"]
        if gained:
            lines.append("You received: " + " + ".join(gained))
        if already_owned:
            lines.append("(Already owned, skipped: " + ", ".join(already_owned) + ")")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)


class CollectionFilterView(discord.ui.View):
    """Attached to /collection — lets anyone viewing it filter the embed
    down to one rarity, with 'View All' as the default. Purely a display
    toggle, so it's open to anyone, not just the person who ran the command.
    """

    def __init__(self, target_id: int, target_display_name: str, color: discord.Color):
        super().__init__(timeout=300)
        self.target_id = target_id
        self.target_display_name = target_display_name
        self.color = color

        options = [discord.SelectOption(label="View All", value="_all", emoji="🦆", default=True)]
        options += [discord.SelectOption(label=rarity_header(r), value=r) for r in RARITY_ORDER]
        select = discord.ui.Select(placeholder="Filter by rarity", options=options)
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        choice = interaction.data["values"][0]
        discord_id = str(self.target_id)
        rec = get_user_record(discord_id)
        owned = rec["collection"]

        content = build_collection_body(owned, rec.get("favorites", []), choice)
        embed = discord.Embed(description=content, color=self.color)
        embed.set_footer(text=f"🎒 {self.target_display_name}'s Collection - {len(owned)}/{len(duck_index)} indexed")

        # Reflect the current choice as the visibly-selected option next time.
        for item in self.children:
            if isinstance(item, discord.ui.Select):
                for opt in item.options:
                    opt.default = (opt.value == choice)

        await interaction.response.edit_message(embed=embed, view=self)


class EditorView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        options = [
            discord.SelectOption(label="Add Duck", value="add", emoji="➕"),
            discord.SelectOption(label="Edit Duck Emoji", value="edit", emoji="✏️"),
            discord.SelectOption(label="Remove Duck", value="remove", emoji="🗑️"),
            discord.SelectOption(label="Toggle Duck Activation", value="toggle_active", emoji="🔁"),
            discord.SelectOption(label="Clear Pool", value="clear_pool", emoji="🧹"),
            discord.SelectOption(label="Toggle Egg Drops", value="toggle", emoji="🥚"),
            discord.SelectOption(label="Set Egg Counter Channel", value="counter", emoji="🔢"),
            discord.SelectOption(label="Set Karma Reaction Channels", value="karma_channels", emoji="😇"),
            discord.SelectOption(label="Block Egg-Drop Channels", value="egg_blocked_channels", emoji="🚫"),
            discord.SelectOption(label="Set Introduction Channel", value="intro_channel", emoji="👋"),
            discord.SelectOption(label="Create Redeem Code", value="redeem_code", emoji="🎟️"),
            discord.SelectOption(label="Set Nest Prize Duck", value="nest_prize_duck", emoji="🎁"),
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
        elif choice == "toggle_active":
            await interaction.response.send_modal(DuckToggleActiveModal())
        elif choice == "clear_pool":
            await interaction.response.send_message(
                "Deactivate every currently active duck? They stay in the index, just leave the earnable pool.",
                view=ConfirmClearPoolView(interaction.user.id),
                ephemeral=True,
            )
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
        elif choice == "egg_blocked_channels":
            await interaction.response.send_message(
                "Pick channels where eggs can never drop (this doesn't affect karma reactions there):",
                view=EggBlockedChannelSelectView(),
                ephemeral=True,
            )
        elif choice == "intro_channel":
            await interaction.response.send_message(
                "Pick the introduction channel for 👋 karma:",
                view=IntroChannelSelectView(),
                ephemeral=True,
            )
        elif choice == "redeem_code":
            await interaction.response.send_modal(RedeemCodeModal())
        elif choice == "nest_prize_duck":
            await interaction.response.send_modal(NestPrizeDuckModal())


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
        self.nest_reset_loop.start()

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
        self.nest_reset_loop.cancel()

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

        blocked_channels = duck_config.get(guild_id, {}).get("egg_blocked_channel_ids", [])
        if message.channel.id in blocked_channels:
            return  # karma (reactions, GM, welcome, invite) still works here — only the drop itself is blocked

        now = time.time()
        if now - rec.get("last_roll_check_ts", 0) < DROP_COOLDOWN_SECONDS:
            return
        rec["last_roll_check_ts"] = now  # kept in-memory only — not worth a DB write on every message

        env = await ensure_environment_for_today()
        if random.random() >= (env["drop_chance_percent"] / 100):
            return
        if roll_duck_id() is None:
            return  # pool is empty, nothing to give right now

        egg_count = env.get("egg_count", 1)
        view = EggDropView(message.author.id, egg_count)
        egg_word = "egg" if egg_count == 1 else "eggs"
        try:
            sent = await message.channel.send(
                content=f"🥚 {message.author.mention} found {egg_count} {egg_word} on the ground!",
                view=view,
            )
            view.message = sent
            duck_stats["total_dropped"] = duck_stats.get("total_dropped", 0) + egg_count
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
        entry = reaction_karma_granted.setdefault(message_key, {"author_id": None, "reactors": set()})
        if payload.user_id in entry["reactors"]:
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

        entry["author_id"] = message.author.id
        entry["reactors"].add(payload.user_id)

        award_karma(str(payload.user_id), 1)  # the REACTOR earns a point
        if not message.author.bot:
            award_karma(str(message.author.id), 1)  # the RECEIVER earns a point too
        await save_duck_state()

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.emoji.id != HEART_EMOJI_ID:
            return

        message_key = str(payload.message_id)
        entry = reaction_karma_granted.get(message_key)
        if not entry or payload.user_id not in entry.get("reactors", ()):
            return  # this person never earned a point here

        entry["reactors"].discard(payload.user_id)
        remove_karma(str(payload.user_id), 1)  # take the point back from the reactor
        author_id = entry.get("author_id")
        if author_id and author_id != payload.user_id:
            remove_karma(str(author_id), 1)  # take the point back from the receiver
        await save_duck_state()

    # ---------- wave-reaction karma (introduction channel) ----------
    # Same rules as heart-reaction karma — 1 hour window, one point per
    # distinct reactor per message, no self-karma — just a different
    # trigger emoji and scoped to a single configured channel instead of
    # an optional allow-list.

    @commands.Cog.listener(name="on_raw_reaction_add")
    async def on_wave_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return
        if payload.member is not None and payload.member.bot:
            return
        if payload.emoji.name != WAVE_EMOJI:
            return

        intro_channel_id = duck_config.get(str(payload.guild_id), {}).get("intro_channel_id")
        if not intro_channel_id or payload.channel_id != intro_channel_id:
            return

        message_key = str(payload.message_id)
        already_credited = wave_karma_granted.setdefault(message_key, set())
        if payload.user_id in already_credited:
            return

        posted_at = discord.utils.snowflake_time(payload.message_id)
        if (discord.utils.utcnow() - posted_at).total_seconds() > REACTION_KARMA_WINDOW_SECONDS:
            return

        try:
            channel = self.bot.get_channel(payload.channel_id) or await self.bot.fetch_channel(payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
        except Exception:
            return

        if message.author.id == payload.user_id:
            return  # no self-karma for waving at your own intro

        already_credited.add(payload.user_id)
        award_karma(str(payload.user_id), 1)
        await save_duck_state()

    @commands.Cog.listener(name="on_raw_reaction_remove")
    async def on_wave_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.emoji.name != WAVE_EMOJI:
            return

        message_key = str(payload.message_id)
        credited = wave_karma_granted.get(message_key)
        if not credited or payload.user_id not in credited:
            return

        credited.discard(payload.user_id)
        remove_karma(str(payload.user_id), 1)
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

    # ---------- staff only, hidden from everyone else: ERROR:404 ----------
    # default_permissions restricts this command's visibility in the slash
    # command picker itself — non-admins won't even see /error exists,
    # unless a server admin manually overrides that in Integration settings.
    # The one thing that can't be hidden: Discord always shows "X used
    # /error" as a channel system message, even for ephemeral responses.

    @app_commands.command(name="error", description="Staff only.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def error_cmd(self, interaction: discord.Interaction):
        error_ducks = [(d, duck) for d, duck in duck_index.items() if duck.get("is_error")]
        lines = [
            f"{duck['emoji']} **{duck['title']}** — {RARITY_DISPLAY[duck['rarity']]} (Error)"
            for _, duck in error_ducks
        ]
        content = "\n".join(lines) or "No ERROR:404 ducks exist yet."

        embed = discord.Embed(title="🚫 ERROR:404", description=content, color=discord.Color.dark_red())
        await interaction.response.send_message(embed=embed, view=ErrorAdminView(), ephemeral=True)

    # ---------- public: redeem a code ----------

    @app_commands.command(name="redeem", description="Redeem a code for eggs or ducks.")
    async def redeem_cmd(self, interaction: discord.Interaction):
        await interaction.response.send_modal(RedeemModal())

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

        stale_wave_reactions = [
            mkey for mkey in wave_karma_granted
            if (now_dt - discord.utils.snowflake_time(int(mkey))).total_seconds() > REACTION_KARMA_WINDOW_SECONDS
        ]
        for mkey in stale_wave_reactions:
            wave_karma_granted.pop(mkey, None)

    @check_expired_limited.before_loop
    async def before_check_expired_limited(self):
        await self.bot.wait_until_ready()

    # ---------- background: keep the egg counter + nest VC names in sync ----------

    @tasks.loop(minutes=EGG_COUNTER_UPDATE_MINUTES)
    async def update_egg_counter_channels(self):
        desired_egg_name = format_egg_channel_name(duck_stats.get("total_dropped", 0))
        for guild_id, config in duck_config.items():
            channel_id = config.get("egg_counter_channel_id")
            if channel_id:
                channel = self.bot.get_channel(channel_id)
                if channel and channel.name != desired_egg_name:
                    try:
                        await channel.edit(name=desired_egg_name)
                    except Exception as e:
                        print(f"[duck_system] failed to rename egg counter channel: {e}")

    @update_egg_counter_channels.before_loop
    async def before_update_egg_counter_channels(self):
        await self.bot.wait_until_ready()

    # ---------- background: weekly nest draw (Saturday 00:00 UTC) ----------
    # Uses catch-up logic (last_reset_week) rather than an exact-minute
    # match, and wraps each guild in its own try/except with an .error
    # restart handler — the same hardening pattern applied to the daily
    # champion reset after it silently missed a day from an unhandled
    # exception with no retry.

    @tasks.loop(minutes=1)
    async def nest_reset_loop(self):
        now = datetime.now(timezone.utc)
        if now.weekday() != 5:  # only consider Saturdays (Mon=0 ... Sat=5)
            return
        week_id = now.strftime("%G-W%V")

        for guild in list(self.bot.guilds):
            guild_id_str = str(guild.id)
            try:
                state = get_nest_state(guild_id_str)
                if state.get("last_reset_week") == week_id:
                    continue

                await self.run_nest_reset(guild, guild_id_str, state)
                state["last_reset_week"] = week_id
                await save_duck_state()
            except Exception as e:
                print(f"[nest] reset failed for guild {guild_id_str}: {e}")
                continue

    @nest_reset_loop.before_loop
    async def before_nest_reset_loop(self):
        await self.bot.wait_until_ready()

    @nest_reset_loop.error
    async def nest_reset_loop_error(self, error):
        print(f"[nest] loop crashed, restarting: {error}")
        if not self.nest_reset_loop.is_running():
            self.nest_reset_loop.start()

    async def run_nest_reset(self, guild: discord.Guild, guild_id_str: str, state: dict):
        entries = state.get("entries", {})
        pool_total = state.get("pool_total", NEST_BASELINE)

        # Only members still in the guild are eligible to win — if someone
        # left, their donated eggs stay in the pool but their entries are
        # dropped from the draw.
        weighted_entries = []
        for discord_id, ticket_count in entries.items():
            if guild.get_member(int(discord_id)) is not None:
                weighted_entries.extend([discord_id] * ticket_count)

        winner_id = random.choice(weighted_entries) if weighted_entries else None
        prize_duck_id = duck_config.get(guild_id_str, {}).get("nest_prize_duck_id")

        if winner_id:
            winner_rec = get_user_record(winner_id)
            winner_rec["inventory"] += pool_total

            duck_text = ""
            if prize_duck_id and prize_duck_id in duck_index:
                duck = duck_index[prize_duck_id]
                if prize_duck_id in winner_rec["collection"]:
                    duck_text = f"\nYou also won {duck['emoji']} **{duck['title']}** — (duplicate), already owned."
                else:
                    winner_rec["collection"].append(prize_duck_id)
                    duck_text = f"\nYou also won {duck['emoji']} **{duck['title']}**!"

            winner_member = guild.get_member(int(winner_id))
            if winner_member:
                try:
                    await winner_member.send(
                        f"🪺 You won the Nest! You received **{pool_total}** egg(s)!{duck_text}"
                    )
                except Exception:
                    pass

            for discord_id in entries:
                if discord_id == winner_id:
                    continue
                loser_member = guild.get_member(int(discord_id))
                if loser_member:
                    try:
                        await loser_member.send("You lost the nest, try again next time!")
                    except Exception:
                        pass

        # Reset for the next cycle regardless of whether anyone entered.
        state["pool_total"] = NEST_BASELINE
        state["entries"] = {}

    # ---------- public: donate to the nest ----------

    @app_commands.command(name="nest", description="Donate eggs to the community nest for a chance to win it all.")
    @app_commands.describe(amount="How many eggs to donate")
    async def nest_cmd(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 1000]):
        discord_id = str(interaction.user.id)
        rec = get_user_record(discord_id)

        if rec["inventory"] < amount:
            return await interaction.response.send_message(
                f"You only have **{rec['inventory']}** egg(s) — you can't donate {amount}.", ephemeral=True
            )

        await interaction.response.send_message(
            f"Are you sure you wish to put **{amount}** eggs into the nest? You might lose these for good.",
            view=NestConfirmView(interaction.user.id, amount),
            ephemeral=True,
        )

    @app_commands.command(name="gift", description="Send eggs from your inventory to another member. Some may get dropped along the way.")
    @app_commands.describe(member="Who should receive the eggs", amount="How many eggs to send")
    async def gift_cmd(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1, 10000],
    ):
        if member.bot:
            return await interaction.response.send_message(
                "Bots can't receive eggs.", ephemeral=True
            )
        if member.id == interaction.user.id:
            return await interaction.response.send_message(
                "You can't send eggs to yourself.", ephemeral=True
            )

        discord_id = str(interaction.user.id)
        rec = get_user_record(discord_id)
        if rec["inventory"] < amount:
            return await interaction.response.send_message(
                f"You only have **{rec['inventory']}** egg(s) — you can't send {amount}.", ephemeral=True
            )

        egg_word = "egg" if amount == 1 else "eggs"
        await interaction.response.send_message(
            f"Send **{amount}** {egg_word} to {member.mention}?\n"
            f"-# Some may get dropped along the way (up to about 30%). Nothing moves until you confirm.",
            view=GiftConfirmView(interaction.user.id, member.id, amount),
            ephemeral=True,
        )

    # ---------- public: view pool / index / collection ----------

    @app_commands.command(name="weather", description="Check today's egg-drop odds and eggs per drop.")
    async def weather_cmd(self, interaction: discord.Interaction):
        env = await ensure_environment_for_today()
        chance = env["drop_chance_percent"]
        egg_count = env.get("egg_count", 1)

        if chance <= 8:
            label = "🌦️ Calm"
        elif chance <= 11:
            label = "🌤️ Active"
        elif chance <= 13:
            label = "⚡ Energetic"
        else:
            label = "🌩️ Frenzy"

        egg_word = "egg" if egg_count == 1 else "eggs"
        lines = [
            f"# {label}",
            f"🥚 Drop Chance: **{chance}%** per check",
            f"🎁 Eggs per Drop Today: **{egg_count}** {egg_word}",
        ]

        embed = discord.Embed(description="\n".join(lines), color=discord.Color.blue())
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="hatchpool", description="View the current earnable duck pool.")
    async def hatchpool(self, interaction: discord.Interaction):
        active_ids = [d for d, v in duck_index.items() if v["active"] and not v.get("is_error")]
        owned_ids = set(get_user_record(str(interaction.user.id)).get("collection", []))
        content = format_grouped_plain(active_ids, owned_ids) or "The pool is currently empty."
        embed = discord.Embed(title="🥚 Current Hatch Pool", description=content, color=discord.Color.gold())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="index", description="View every duck that has ever been added.")
    async def index_cmd(self, interaction: discord.Interaction):
        if not duck_index:
            embed = discord.Embed(
                title="📖 Duck Index", description="No ducks have been added yet.", color=discord.Color.blurple()
            )
            return await interaction.response.send_message(embed=embed)

        embed = discord.Embed(
            title="📖 Duck Index", description="Choose a category below to view.", color=discord.Color.blurple()
        )
        await interaction.response.send_message(embed=embed, view=IndexView())

    @app_commands.command(name="collection", description="View your (or someone else's) duck collection.")
    @app_commands.describe(member="Whose collection to view")
    async def collection_cmd(self, interaction: discord.Interaction, member: discord.Member = None):
        target = member or interaction.user
        discord_id = str(target.id)
        rec = get_user_record(discord_id)

        content = build_collection_body(rec["collection"], rec.get("favorites", []))
        role_color = target.color if target.color.value != 0 else discord.Color.teal()
        embed = discord.Embed(description=content, color=role_color)
        embed.set_footer(text=f"🎒 {target.display_name}'s Collection - {len(rec['collection'])}/{len(duck_index)} indexed")

        view = CollectionFilterView(target.id, target.display_name, role_color)
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="fav", description="Favorite or unfavorite a duck from your collection.")
    @app_commands.describe(duck="Start typing — only ducks you own will show up")
    @app_commands.autocomplete(duck=fav_duck_autocomplete)
    async def fav_cmd(self, interaction: discord.Interaction, duck: str):
        discord_id = str(interaction.user.id)
        rec = get_user_record(discord_id)

        duck_id = slugify(duck)
        if duck_id not in rec.get("collection", []):
            return await interaction.response.send_message(
                f"You don't own a duck matching **{duck}** — pick one of the suggestions as you type.",
                ephemeral=True,
            )

        duck_info = duck_index.get(duck_id)
        duck_title = duck_info["title"] if duck_info else duck
        duck_emoji = duck_info["emoji"] if duck_info else "🦆"

        favorites = rec.setdefault("favorites", [])
        if duck_id in favorites:
            favorites.remove(duck_id)
            await save_duck_state()
            return await interaction.response.send_message(
                f"💔 Removed {duck_emoji} **{duck_title}** from your favorites.", ephemeral=True
            )

        favorites.append(duck_id)
        await save_duck_state()
        await interaction.response.send_message(
            f"⭐ Added {duck_emoji} **{duck_title}** to your favorites.", ephemeral=True
        )

    @app_commands.command(name="inventory", description="Check how many eggs you have in storage.")
    async def duckinventory_cmd(self, interaction: discord.Interaction):
        discord_id = str(interaction.user.id)
        rec = get_user_record(discord_id)

        description = (
            f"## 🥚 You have **{rec['inventory']}** egg(s) stored\n"
            f"> **😇 Karma: {rec.get('karma', 0)}/{KARMA_PER_EGG}**\n"
        )

        if interaction.guild:
            guild_id = str(interaction.guild.id)
            nest = get_nest_state(guild_id)
            description += f"> -# 🪺 Nest: {nest['pool_total']}\n"

            my_entries = nest["entries"].get(discord_id, 0)
            if my_entries > 0:
                description += f"> -# 🎟️ Entries: {my_entries}\n"
                description += f"> -# 🗓️ Deadline: {format_nest_countdown()}\n"

        user = interaction.user
        role_color = user.color if isinstance(user, discord.Member) and user.color.value != 0 else discord.Color.blurple()
        embed = discord.Embed(description=description.strip(), color=role_color)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------- open eggs from inventory ----------

    @app_commands.command(name="hatch", description="Open eggs from your inventory.")
    @app_commands.describe(amount="How many eggs to open")
    async def open_eggs(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 1000]):
        await interaction.response.defer()
        discord_id = str(interaction.user.id)
        rec = duck_users.setdefault(discord_id, default_user_record())

        if rec["inventory"] < amount:
            return await interaction.followup.send(
                f"You only have **{rec['inventory']}** egg(s) — you can't open {amount}."
            )

        pre_existing = set(rec["collection"])  # ownership snapshot BEFORE this batch

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

        # Aggregate identical ducks into one line each, in first-seen order,
        # so a big batch doesn't turn into dozens of near-identical rows.
        grouped = {}
        for r in results:
            g = grouped.setdefault(r["duck_id"], {
                "emoji": r["emoji"], "title": r["title"], "rarity": r["rarity"],
                "count": 0, "bonus_eggs": 0,
            })
            g["count"] += 1
            if r["bonus_egg"]:
                g["bonus_eggs"] += 1

        lines = [f"🥚 Opened {len(results)} egg(s):"]
        for duck_id, g in grouped.items():
            status_tag = " (duplicate)" if duck_id in pre_existing else " (**NEW**)"
            bonus = f" 🍀+{g['bonus_eggs']} bonus egg(s)" if g["bonus_eggs"] else ""
            lines.append(
                f"{g['emoji']} **{g['title']}** (x{g['count']}) - "
                f"{RARITY_DISPLAY[g['rarity']]}{status_tag}{bonus}"
            )

        await interaction.followup.send("\n".join(lines))


async def setup(bot: commands.Bot):
    await bot.add_cog(DuckCog(bot))
