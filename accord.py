from __future__ import annotations

import datetime
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from dotenv import load_dotenv

from dm_logic import DM_ROLE_NAMES, ROLE_DM_ASK, ROLE_DM_CLOSED, ROLE_DM_OPEN, resolve_mode

# ==============================
# Configuration
# ==============================
logging.basicConfig(
    level=logging.INFO,
)

log = logging.getLogger("accord")

load_dotenv()


def _get_int_env(name: str) -> int | None:
    raw_value = os.getenv(name)
    if raw_value in {None, ""}:
        return None

    try:
        return int(raw_value)
    except ValueError:
        log.warning("Ignoring invalid integer environment variable %s=%r", name, raw_value)
        return None


def _get_bool_env(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value in {None, ""}:
        return default

    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False

    log.warning("Invalid boolean for %s=%r, using default %s", name, raw_value, default)
    return default


TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = _get_int_env("GUILD_ID")
DEBUG = _get_bool_env("DEBUG", False)

BYPASS_ROLE_IDS = set()
DB_FILE = Path(os.getenv("ACCORD_DB_FILE", "accord.db"))

# Relationship metadata (symmetric per pair)
RELATIONSHIPS: dict[int, dict[str, dict[str, Any]]] = {}
REQUEST_CHANNELS: dict[int, int] = {}


# ==============================
# Intents
# ==============================
intents = discord.Intents.default()
intents.members = True

# Interaction Consent State
INTERACTION_PAIRS: dict[int, set[tuple[int, int]]] = {}
DM_REQUESTS: dict[int, dict[tuple[int, int], dict[str, Any]]] = {}
CONSENT_MESSAGES: dict[int, dict[str, dict[str, int]]] = {}

AUDIT_LOG_CHANNEL_ID = None
AUDIT_LOG_CHANNELS: dict[int, int] = {}
PANEL_SETTINGS: dict[int, dict[str, int | None]] = {}
DM_REQUEST_PANEL_VIEW_ID = "dm_request:open_modal"
DM_REQUEST_PANEL_BUMP_GUARD: dict[int, datetime.datetime] = {}


def _connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def _get_metadata(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    return str(row["value"])


def _set_metadata(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO metadata(key, value)
        VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def _iter_unique_pair_rows(pairs_by_guild: dict[int, set[tuple[int, int]]]):
    for guild_id, pairs in pairs_by_guild.items():
        seen: set[tuple[int, int]] = set()
        for a, b in pairs:
            if a == b:
                continue
            lo, hi = (a, b) if a < b else (b, a)
            if (lo, hi) in seen:
                continue
            seen.add((lo, hi))
            yield guild_id, lo, hi

# ==============================
# Bot Class
# ==============================
class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        ensure_database()
        self.add_view(DmRequestPanelView())

        if DEBUG and GUILD_ID is not None:
            guild = discord.Object(id=GUILD_ID)
            self.tree.clear_commands(guild=guild)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced to dev guild.")
        else:
            if DEBUG and GUILD_ID is None:
                log.warning("DEBUG is enabled but GUILD_ID is not set. Syncing commands globally.")
            await self.tree.sync()
            log.info("Synced globally.")

bot = Bot()

# ==============================
# Events
# ==============================
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    load_consent()
    load_dm_requests()
    load_relationships()
    reconcile_relationship_defaults()
    load_request_channels()
    load_audit_channels()
    load_panel_settings()

    # Restore configured DM request panel message on startup.
    for guild in bot.guilds:
        settings = PANEL_SETTINGS.get(guild.id)
        if not isinstance(settings, dict):
            continue
        panel_channel_id = settings.get("panel_channel_id")
        if panel_channel_id is None:
            continue
        await ensure_dm_request_panel_message(guild, int(panel_channel_id), force_repost=False)

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    after_dm = [r for r in after.roles if r.name in DM_ROLE_NAMES]

    # If more than one DM role exists after update
    if len(after_dm) > 1:

        # Keep the highest role in hierarchy
        keep = max(after_dm, key=lambda r: r.position)

        remove = [r for r in after_dm if r != keep]

        try:
            await after.remove_roles(*remove)
        except discord.Forbidden:
            pass

@bot.event
async def on_disconnect():
    save_consent()
    save_dm_requests()
    save_relationships()
    save_request_channels()
    save_audit_channels()
    save_panel_settings()


@bot.event
async def on_message(message):
    await bump_dm_request_panel_if_needed(message)

# ==============================
# Logic
# ==============================
async def safe_dm_user(user: Any, embed: discord.Embed):
    sender = getattr(user, "send", None)
    if sender is None:
        return

    try:
        await sender(embed=embed)
    except discord.Forbidden:
        # User has DMs closed or blocked the bot
        pass
    except discord.HTTPException:
        pass


def load_audit_log(
    guild_id: int | None = None,
    user_id: int | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    ensure_database()

    query = [
        """
        SELECT timestamp, guild_id, action, message, actor_id, user1_id, user2_id, request_type
        FROM audit_log
        """
    ]
    params: list[Any] = []
    clauses: list[str] = []

    if guild_id is not None:
        clauses.append("guild_id = ?")
        params.append(guild_id)

    if user_id is not None:
        clauses.append("(actor_id = ? OR user1_id = ? OR user2_id = ?)")
        params.extend([user_id, user_id, user_id])

    if clauses:
        query.append("WHERE " + " AND ".join(clauses))

    if limit is not None:
        query.append("ORDER BY id DESC LIMIT ?")
        params.append(limit)
        sql = "SELECT * FROM (" + " ".join(query) + ") ORDER BY timestamp ASC"
    else:
        query.append("ORDER BY timestamp ASC")
        sql = " ".join(query)

    with _connect_db() as conn:
        rows = conn.execute(sql, params).fetchall()

    return [dict(row) for row in rows]


def is_mutual(guild_id: int, user1: int, user2: int) -> bool:
    pairs = INTERACTION_PAIRS.get(guild_id, set())
    return (
        (user1, user2) in pairs and
        (user2, user1) in pairs
    )

def add_mutual_pair(pair_set: set, a: int, b: int):
    pair_set.add((a, b))
    pair_set.add((b, a))

def _normalize_request_type(value: str | None) -> str:
    v = (value or "").strip().lower()
    if v in {"friend", "friend_request", "fr", "friendrequest"}:
        return "friend"
    return "dm"

def _request_type_label(value: str | None) -> str:
    v = _normalize_request_type(value)
    return "Friend Request" if v == "friend" else "Direct Message"

def _relationship_key(a: int, b: int) -> str:
    lo, hi = (a, b) if a < b else (b, a)
    return f"{lo}-{hi}"


def ensure_database() -> None:
    with _connect_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS consent_pairs (
                guild_id INTEGER NOT NULL,
                user_low INTEGER NOT NULL,
                user_high INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_low, user_high)
            );

            CREATE TABLE IF NOT EXISTS relationships (
                guild_id INTEGER NOT NULL,
                pair_key TEXT NOT NULL,
                request_type TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT,
                source_channel_id INTEGER,
                source_message_id INTEGER,
                PRIMARY KEY (guild_id, pair_key)
            );

            CREATE TABLE IF NOT EXISTS dm_requests (
                guild_id INTEGER NOT NULL,
                requester_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                request_type TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT,
                PRIMARY KEY (guild_id, requester_id, target_id)
            );

            CREATE TABLE IF NOT EXISTS request_channels (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_channels (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS confession_settings (
                guild_id INTEGER PRIMARY KEY,
                panel_channel_id INTEGER,
                panel_message_id INTEGER,
                target_channel_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                guild_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                message TEXT NOT NULL,
                actor_id INTEGER,
                user1_id INTEGER,
                user2_id INTEGER,
                request_type TEXT
            );

            -- Performance indexes
            CREATE INDEX IF NOT EXISTS idx_consent_pairs_guild
                ON consent_pairs(guild_id);

            CREATE INDEX IF NOT EXISTS idx_relationships_guild_pair
                ON relationships(guild_id, pair_key);

            CREATE INDEX IF NOT EXISTS idx_dm_requests_guild
                ON dm_requests(guild_id);

            CREATE INDEX IF NOT EXISTS idx_audit_log_guild
                ON audit_log(guild_id);

            CREATE INDEX IF NOT EXISTS idx_audit_log_users
                ON audit_log(guild_id, user1_id, user2_id);

            CREATE INDEX IF NOT EXISTS idx_audit_log_actor
                ON audit_log(guild_id, actor_id);
            """
        )


def load_relationships():
    """Load relationship metadata (symmetric) from SQLite."""
    global RELATIONSHIPS
    ensure_database()
    out: dict[int, dict[str, dict[str, Any]]] = {}

    with _connect_db() as conn:
        rows = conn.execute(
            """
            SELECT guild_id, pair_key, request_type, reason, created_at, source_channel_id, source_message_id
            FROM relationships
            """
        ).fetchall()

    for row in rows:
        guild_id = int(row["guild_id"])
        out.setdefault(guild_id, {})
        out[guild_id][str(row["pair_key"])] = {
            "type": _normalize_request_type(row["request_type"]),
            "reason": (row["reason"] or "").strip(),
            "created_at": row["created_at"],
            "source_channel_id": row["source_channel_id"],
            "source_message_id": row["source_message_id"],
        }

    RELATIONSHIPS = out
    rebuild_consent_messages()

def save_relationships():
    ensure_database()
    rows = []
    for guild_id, pairs in RELATIONSHIPS.items():
        for pair_key, meta in pairs.items():
            rows.append(
                (
                    guild_id,
                    pair_key,
                    _normalize_request_type(meta.get("type")),
                    (meta.get("reason") or "").strip(),
                    meta.get("created_at"),
                    meta.get("source_channel_id"),
                    meta.get("source_message_id"),
                )
            )

    with _connect_db() as conn:
        conn.execute("DELETE FROM relationships")
        if rows:
            conn.executemany(
                """
                INSERT INTO relationships(
                    guild_id, pair_key, request_type, reason, created_at, source_channel_id, source_message_id
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    rebuild_consent_messages()

def set_relationship_meta(
    guild_id: int,
    a: int,
    b: int,
    request_type: str,
    reason: str | None,
    *,
    source_channel_id: int | None = None,
    source_message_id: int | None = None,
):
    """Set (or update) symmetric metadata for a relationship."""
    key = _relationship_key(a, b)
    RELATIONSHIPS.setdefault(guild_id, {})
    existing = RELATIONSHIPS[guild_id].get(key, {})
    created_at = existing.get("created_at") or datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    RELATIONSHIPS[guild_id][key] = {
        "type": _normalize_request_type(request_type),
        "reason": (reason or "").strip(),
        "created_at": created_at,
        "source_channel_id": source_channel_id if source_channel_id is not None else existing.get("source_channel_id"),
        "source_message_id": source_message_id if source_message_id is not None else existing.get("source_message_id"),
    }

def get_relationship_meta(guild_id: int, a: int, b: int) -> dict:
    """Get symmetric metadata; returns defaults if missing (does not persist)."""
    key = _relationship_key(a, b)
    meta = RELATIONSHIPS.get(guild_id, {}).get(key)
    if not isinstance(meta, dict):
        return {
            "type": "dm",
            "reason": "",
            "created_at": None,
            "source_channel_id": None,
            "source_message_id": None,
        }
    return {
        "type": _normalize_request_type(meta.get("type")),
        "reason": (meta.get("reason") or "").strip(),
        "created_at": meta.get("created_at"),
        "source_channel_id": meta.get("source_channel_id"),
        "source_message_id": meta.get("source_message_id"),
    }

def delete_relationship_meta(guild_id: int, a: int, b: int):
    key = _relationship_key(a, b)
    if guild_id in RELATIONSHIPS and key in RELATIONSHIPS[guild_id]:
        del RELATIONSHIPS[guild_id][key]
        if not RELATIONSHIPS[guild_id]:
            del RELATIONSHIPS[guild_id]

def reconcile_relationship_defaults():
    """
    Ensure every mutual relationship has a metadata entry.
    Also, for older entries missing keys, default to DM.
    """
    changed = False
    for guild_id, pairs in INTERACTION_PAIRS.items():
        seen = set()
        for a, b in pairs:
            if (b, a) not in pairs:
                continue
            key = _relationship_key(a, b)
            if key in seen:
                continue
            seen.add(key)

            meta = RELATIONSHIPS.get(guild_id, {}).get(key)
            if not isinstance(meta, dict):
                set_relationship_meta(guild_id, a, b, "dm", "")
                changed = True
                continue

            t = meta.get("type")
            r = meta.get("reason")
            ca = meta.get("created_at")
            if t is None or r is None or ca is None:
                set_relationship_meta(guild_id, a, b, t or "dm", r or "")
                changed = True

    if changed:
        save_relationships()

def load_dm_requests():
    """Load pending DM request records from SQLite."""
    global DM_REQUESTS
    ensure_database()
    out: dict[int, dict[tuple[int, int], dict[str, Any]]] = {}

    with _connect_db() as conn:
        rows = conn.execute(
            """
            SELECT guild_id, requester_id, target_id, message_id, request_type, reason, created_at
            FROM dm_requests
            """
        ).fetchall()

    for row in rows:
        guild_id = int(row["guild_id"])
        out.setdefault(guild_id, {})
        out[guild_id][(int(row["requester_id"]), int(row["target_id"]))] = {
            "message_id": int(row["message_id"]),
            "request_type": _normalize_request_type(row["request_type"]),
            "reason": (row["reason"] or "").strip(),
            "created_at": row["created_at"],
        }

    DM_REQUESTS = out

def save_dm_requests():
    ensure_database()
    rows = []
    for guild_id, pairs in DM_REQUESTS.items():
        for (requester_id, target_id), record in pairs.items():
            message_id = int(record.get("message_id") or 0)
            if message_id <= 0:
                continue
            rows.append(
                (
                    guild_id,
                    requester_id,
                    target_id,
                    message_id,
                    _normalize_request_type(record.get("request_type")),
                    (record.get("reason") or "").strip(),
                    record.get("created_at"),
                )
            )

    with _connect_db() as conn:
        conn.execute("DELETE FROM dm_requests")
        if rows:
            conn.executemany(
                """
                INSERT INTO dm_requests(
                    guild_id, requester_id, target_id, message_id, request_type, reason, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )


async def log_audit_event(
    guild: discord.Guild,
    message: str,
    *,
    action: str = "generic",
    actor_id: int | None = None,
    user1_id: int | None = None,
    user2_id: int | None = None,
    request_type: str | None = None,
):
    global AUDIT_LOG_CHANNEL_ID

    timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    ensure_database()

    with _connect_db() as conn:
        conn.execute(
            """
            INSERT INTO audit_log(timestamp, guild_id, action, message, actor_id, user1_id, user2_id, request_type)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp,
                guild.id,
                action,
                message,
                actor_id,
                user1_id,
                user2_id,
                _normalize_request_type(request_type) if request_type else None,
            ),
        )

    log.info(
        {
            "timestamp": timestamp,
            "guild_id": guild.id,
            "action": action,
            "message": message,
            "actor_id": actor_id,
            "user1_id": user1_id,
            "user2_id": user2_id,
        }
    )

    # Send to audit channel if configured
    channel_id = AUDIT_LOG_CHANNELS.get(guild.id) or AUDIT_LOG_CHANNEL_ID
    if channel_id:
        channel = guild.get_channel(channel_id)
        if channel:
            embed = discord.Embed(
                title="📜 DM Permission Audit",
                description=message,
                color=discord.Color.blurple()
            )
            embed.set_footer(text=timestamp)
            await channel.send(embed=embed)


def load_request_channels():
    global REQUEST_CHANNELS
    ensure_database()
    with _connect_db() as conn:
        rows = conn.execute("SELECT guild_id, channel_id FROM request_channels").fetchall()
    REQUEST_CHANNELS = {int(row["guild_id"]): int(row["channel_id"]) for row in rows}

def save_request_channels():
    ensure_database()
    rows = [(guild_id, channel_id) for guild_id, channel_id in REQUEST_CHANNELS.items()]
    with _connect_db() as conn:
        conn.execute("DELETE FROM request_channels")
        if rows:
            conn.executemany(
                "INSERT INTO request_channels(guild_id, channel_id) VALUES(?, ?)",
                rows,
            )


def load_audit_channels():
    global AUDIT_LOG_CHANNELS
    global AUDIT_LOG_CHANNEL_ID

    ensure_database()
    with _connect_db() as conn:
        rows = conn.execute("SELECT guild_id, channel_id FROM audit_channels").fetchall()

    AUDIT_LOG_CHANNELS = {int(row["guild_id"]): int(row["channel_id"]) for row in rows}
    AUDIT_LOG_CHANNEL_ID = next(iter(AUDIT_LOG_CHANNELS.values()), None)


def save_audit_channels():
    ensure_database()
    rows = [(guild_id, channel_id) for guild_id, channel_id in AUDIT_LOG_CHANNELS.items()]
    with _connect_db() as conn:
        conn.execute("DELETE FROM audit_channels")
        if rows:
            conn.executemany(
                "INSERT INTO audit_channels(guild_id, channel_id) VALUES(?, ?)",
                rows,
            )


def load_consent():
    global INTERACTION_PAIRS
    ensure_database()
    INTERACTION_PAIRS = {}

    with _connect_db() as conn:
        rows = conn.execute("SELECT guild_id, user_low, user_high FROM consent_pairs").fetchall()

    for row in rows:
        guild_id = int(row["guild_id"])
        a = int(row["user_low"])
        b = int(row["user_high"])
        INTERACTION_PAIRS.setdefault(guild_id, set())
        INTERACTION_PAIRS[guild_id].add((a, b))
        INTERACTION_PAIRS[guild_id].add((b, a))

    if DEBUG:
        log.info("=== CONSENT STATE AFTER LOAD ===")
        log.info("Loaded pairs: %s", INTERACTION_PAIRS)


def save_consent():
    ensure_database()
    rows = list(_iter_unique_pair_rows(INTERACTION_PAIRS))

    with _connect_db() as conn:
        conn.execute("DELETE FROM consent_pairs")
        if rows:
            conn.executemany(
                """
                INSERT INTO consent_pairs(guild_id, user_low, user_high)
                VALUES(?, ?, ?)
                """,
                rows,
            )


def rebuild_consent_messages():
    global CONSENT_MESSAGES
    out: dict[int, dict[str, dict[str, int]]] = {}
    for guild_id, pairs in RELATIONSHIPS.items():
        for pair_key, meta in pairs.items():
            channel_id = meta.get("source_channel_id")
            message_id = meta.get("source_message_id")
            if not channel_id or not message_id:
                continue

            try:
                a_str, b_str = pair_key.split("-")
                requester_id = int(a_str)
                target_id = int(b_str)
            except (AttributeError, TypeError, ValueError):
                continue

            out.setdefault(guild_id, {})[f"{requester_id}:{target_id}"] = {
                "channel_id": int(channel_id),
                "message_id": int(message_id),
                "requester_id": requester_id,
                "target_id": target_id,
            }

    CONSENT_MESSAGES = out


def load_consent_messages():
    rebuild_consent_messages()


def save_consent_messages():
    rebuild_consent_messages()


def _default_panel_settings() -> dict[str, int | None]:
    return {
        "panel_channel_id": None,
        "panel_message_id": None,
    }


def _get_panel_settings(guild_id: int) -> dict[str, int | None]:
    current = PANEL_SETTINGS.get(guild_id)
    if not isinstance(current, dict):
        current = _default_panel_settings()
        PANEL_SETTINGS[guild_id] = current
        return current

    defaults = _default_panel_settings()
    for key, value in defaults.items():
        current.setdefault(key, value)
    return current


def load_panel_settings():
    global PANEL_SETTINGS
    ensure_database()
    out: dict[int, dict[str, int | None]] = {}

    with _connect_db() as conn:
        rows = conn.execute(
            """
            SELECT guild_id, panel_channel_id, panel_message_id, target_channel_id
            FROM confession_settings
            """
        ).fetchall()

    for row in rows:
        guild_id = int(row["guild_id"])
        out[guild_id] = {
            "panel_channel_id": int(row["panel_channel_id"]) if row["panel_channel_id"] is not None else None,
            "panel_message_id": int(row["panel_message_id"]) if row["panel_message_id"] is not None else None,
        }

    PANEL_SETTINGS = out


def save_panel_settings():
    ensure_database()
    rows = []
    for guild_id, settings in PANEL_SETTINGS.items():
        rows.append(
            (
                int(guild_id),
                settings.get("panel_channel_id"),
                settings.get("panel_message_id"),
                None,
            )
        )

    with _connect_db() as conn:
        conn.execute("DELETE FROM confession_settings")
        if rows:
            conn.executemany(
                """
                INSERT INTO confession_settings(guild_id, panel_channel_id, panel_message_id, target_channel_id)
                VALUES(?, ?, ?, ?)
                """,
                rows,
            )


def _build_dm_request_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="DM Request Panel",
        description=(
            "Click the button below to open a DM request modal.\n"
            "This uses the same options as `/dm_ask` (user, request type, reason)."
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="This panel is kept as the latest message in this channel.")
    return embed


async def ensure_dm_request_panel_message(
    guild: discord.Guild,
    panel_channel_id: int,
    *,
    force_repost: bool = False,
) -> int | None:
    channel = guild.get_channel(panel_channel_id)
    if channel is None:
        return None

    settings = _get_panel_settings(guild.id)
    old_message_id = settings.get("panel_message_id")

    # If panel is already the newest message, just refresh it.
    if force_repost and old_message_id and hasattr(channel, "history"):
        try:
            latest = None
            async for msg in channel.history(limit=1):
                latest = msg
            if latest and int(latest.id) == int(old_message_id):
                force_repost = False
        except (discord.Forbidden, discord.HTTPException):
            pass

    if old_message_id and not force_repost:
        try:
            existing = await channel.fetch_message(int(old_message_id))
            await existing.edit(embed=_build_dm_request_panel_embed(), view=DmRequestPanelView())
            settings["panel_channel_id"] = int(panel_channel_id)
            PANEL_SETTINGS[guild.id] = settings
            save_panel_settings()
            return int(existing.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            settings["panel_message_id"] = None

    try:
        new_message = await channel.send(embed=_build_dm_request_panel_embed(), view=DmRequestPanelView())
    except (discord.Forbidden, discord.HTTPException):
        return None

    new_message_id = int(new_message.id)
    settings["panel_channel_id"] = int(panel_channel_id)
    settings["panel_message_id"] = new_message_id
    PANEL_SETTINGS[guild.id] = settings
    save_panel_settings()

    if old_message_id and int(old_message_id) != new_message_id:
        try:
            old_message = await channel.fetch_message(int(old_message_id))
            await old_message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
            pass

    return new_message_id


async def bump_dm_request_panel_if_needed(message: Any):
    guild = getattr(message, "guild", None)
    channel = getattr(message, "channel", None)
    if guild is None or channel is None:
        return

    settings = PANEL_SETTINGS.get(guild.id)
    if not isinstance(settings, dict):
        return

    panel_channel_id = settings.get("panel_channel_id")
    panel_message_id = settings.get("panel_message_id")
    if panel_channel_id is None:
        return
    if getattr(channel, "id", None) != panel_channel_id:
        return
    if panel_message_id is not None and getattr(message, "id", None) == panel_message_id:
        return

    # Prevent rapid repost loops.
    now = datetime.datetime.utcnow()
    last = DM_REQUEST_PANEL_BUMP_GUARD.get(guild.id)
    if last and (now - last).total_seconds() < 2:
        return

    DM_REQUEST_PANEL_BUMP_GUARD[guild.id] = now
    await ensure_dm_request_panel_message(guild, int(panel_channel_id), force_repost=True)


def _resolve_member_from_text(guild: discord.Guild, raw: str) -> discord.Member | None:
    text = str(raw or "").strip()
    if not text:
        return None

    mention_match = re.fullmatch(r"<@!?(\d+)>", text)
    if mention_match:
        user_id = int(mention_match.group(1))
        return guild.get_member(user_id)

    if text.isdigit():
        return guild.get_member(int(text))

    return None


def _precheck_dm_request(
    guild: discord.Guild,
    requester: discord.Member,
    target: discord.Member,
) -> tuple[str | None, discord.TextChannel | None]:
    if target.id == requester.id and not DEBUG:
        return "You cannot request permission with yourself.", None

    if target.bot:
        return "You cannot request permission from bots.", None

    mode = resolve_mode(target)
    if mode == "closed":
        return f"{target.display_name} has DMs set to CLOSED and is not accepting requests.", None

    if mode == "open" and not DEBUG:
        return f"{target.display_name} has DMs set to OPEN. No request required.", None

    if is_mutual(guild.id, requester.id, target.id):
        return "A permission relationship already exists.", None

    request_channel_id = REQUEST_CHANNELS.get(guild.id)
    if not request_channel_id:
        return "No DM request channel has been configured. Use `/dm_request_channel_set` first.", None

    request_channel = guild.get_channel(request_channel_id)
    if not request_channel:
        return "Configured DM request channel is invalid.", None

    return None, request_channel


async def _submit_dm_request(
    interaction: discord.Interaction,
    user: discord.Member,
    request_type: str | None,
    reason: str | None,
):
    guild = interaction.guild
    guild_id = guild.id
    requester = interaction.user

    req_type = _normalize_request_type(request_type or "dm")
    reason_clean = str(reason or "").strip()
    if len(reason_clean) > 256:
        reason_clean = reason_clean[:253] + "..."

    error_message, request_channel = _precheck_dm_request(guild, requester, user)
    if error_message:
        await interaction.response.send_message(error_message, ephemeral=True)
        return

    # Defensive fallback (precheck should always resolve this).
    if request_channel is None:
        await interaction.response.send_message(
            "Configured DM request channel is invalid.",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="📨 Permission Request",
        description=(
            f"{user.mention}\n\n"
            f"You have a connection request.\n\n"
            "This request will time out in 24 hours."
        ),
        color=discord.Color.gold()
    )

    embed.set_author(
        name=interaction.user.display_name,
        icon_url=interaction.user.display_avatar.url
    )

    embed.set_footer(text="Permission can be revoked at any time with /dm_revoke")

    embed.add_field(
        name="Request Type",
        value=_request_type_label(req_type),
        inline=True
    )
    embed.add_field(
        name="Reason",
        value=reason_clean if reason_clean else "—",
        inline=False
    )

    view = AskConsentView(
        requester_id=requester.id,
        target_id=user.id,
        guild_id=guild_id,
        request_type=req_type,
        reason=reason_clean
    )

    try:
        message = await request_channel.send(
            content=user.mention,
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions(users=[user])
        )

        await message.edit(content=None)
        view.message = message

        DM_REQUESTS.setdefault(guild_id, {})
        DM_REQUESTS[guild_id][(requester.id, user.id)] = {
            "message_id": message.id,
            "request_type": req_type,
            "reason": reason_clean,
            "created_at": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        }
        save_dm_requests()

    except discord.Forbidden:
        await interaction.response.send_message(
            "I do not have permission to send messages in the configured DM request channel.",
            ephemeral=True
        )
        return

    await log_audit_event(
        interaction.guild,
        f"DM request asked: {interaction.user.display_name} ➝ {user.display_name} ({_request_type_label(req_type)})"
    )

    await interaction.response.send_message(
        f"📨 DM request sent to {request_channel.mention}.",
        ephemeral=True
    )


def _build_picker_prompt(selected_user_id: int | None, request_type: str) -> str:
    user_line = f"<@{selected_user_id}>" if selected_user_id is not None else "No user selected yet."
    type_line = _request_type_label(request_type)
    return (
        "**DM Request Builder**\n"
        f"User: {user_line}\n"
        f"Request Type: {type_line}\n\n"
        "Pick a user from the list, choose a request type, then press Continue."
    )


class DmRequestReasonModal(discord.ui.Modal):
    def __init__(self, target_user_id: int, request_type: str):
        super().__init__(title="Send DM Request")
        self.target_user_id = target_user_id
        self.request_type = _normalize_request_type(request_type)
        self.reason_input = discord.ui.TextInput(
            label="Reason (optional)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=256,
            placeholder="Optional context shown to recipient",
        )
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This modal can only be used in a server.",
                ephemeral=True,
            )
            return

        target_user = guild.get_member(self.target_user_id)
        if target_user is None:
            await interaction.response.send_message(
                "Could not resolve that user in this server.",
                ephemeral=True,
            )
            return

        await _submit_dm_request(
            interaction,
            target_user,
            self.request_type,
            str(self.reason_input.value or ""),
        )


class DmRequestUserSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(
            placeholder="Select a user...",
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, DmRequestLookupView):
            return

        selected = self.values[0]
        view.selected_user_id = selected.id
        await interaction.response.edit_message(
            content=_build_picker_prompt(view.selected_user_id, view.request_type),
            view=view,
        )


class DmRequestLookupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.selected_user_id: int | None = None
        self.request_type: str = "dm"
        self.add_item(DmRequestUserSelect())

    @discord.ui.button(label="Type: DM", style=discord.ButtonStyle.secondary)
    async def pick_dm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.request_type = "dm"
        await interaction.response.edit_message(
            content=_build_picker_prompt(self.selected_user_id, self.request_type),
            view=self,
        )

    @discord.ui.button(label="Type: Friend", style=discord.ButtonStyle.secondary)
    async def pick_friend(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.request_type = "friend"
        await interaction.response.edit_message(
            content=_build_picker_prompt(self.selected_user_id, self.request_type),
            view=self,
        )

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.primary)
    async def continue_to_reason(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.selected_user_id is None:
            await interaction.response.send_message(
                "Pick a user first.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This control can only be used in a server channel.",
                ephemeral=True,
            )
            return

        target_user = guild.get_member(self.selected_user_id)
        if target_user is None:
            await interaction.response.send_message(
                "Could not resolve that user in this server.",
                ephemeral=True,
            )
            return

        error_message, _ = _precheck_dm_request(guild, interaction.user, target_user)
        if error_message:
            await interaction.response.send_message(error_message, ephemeral=True)
            return

        await interaction.response.send_modal(
            DmRequestReasonModal(
                target_user_id=self.selected_user_id,
                request_type=self.request_type,
            )
        )


class DmRequestPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Open DM Request Form",
        style=discord.ButtonStyle.primary,
        custom_id=DM_REQUEST_PANEL_VIEW_ID,
    )
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message(
                "This button can only be used in a server channel.",
                ephemeral=True,
            )
            return
        picker_view = DmRequestLookupView()
        await interaction.response.send_message(
            _build_picker_prompt(None, "dm"),
            view=picker_view,
            ephemeral=True,
        )


class AskConsentView(discord.ui.View):
    def __init__(
        self,
        requester_id: int,
        target_id: int,
        guild_id: int = 0,
        request_type: str = "dm",
        reason: str = ""
    ):
        super().__init__(timeout=86400)
        self.requester_id = requester_id
        self.target_id = target_id
        self.guild_id = guild_id
        self.request_type = _normalize_request_type(request_type)
        self.reason = (reason or "").strip()
        self.message = None

    def _clear_request_record(self):
        recs = DM_REQUESTS.get(self.guild_id, {})
        if (self.requester_id, self.target_id) in recs:
            del recs[(self.requester_id, self.target_id)]
        if not recs and self.guild_id in DM_REQUESTS:
            del DM_REQUESTS[self.guild_id]

    async def on_timeout(self):
        if self.message:
            for child in self.children:
                child.disabled = True

            timeout_embed = discord.Embed(
                title="⌛ DM Request Expired",
                description="This DM request expired after 24 hours.",
                color=discord.Color.orange()
            )
            timeout_embed.add_field(
                name="Request Type",
                value=_request_type_label(self.request_type),
                inline=True
            )
            timeout_embed.add_field(
                name="Reason",
                value=self.reason if self.reason else "—",
                inline=False
            )

            await self.message.edit(embed=timeout_embed, view=self)

            # Remove stored pending request record
            self._clear_request_record()
            save_dm_requests()

    # ✅ BUTTONS MUST LIVE INSIDE CLASS

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):

        if interaction.user.id != self.target_id:
            await interaction.response.send_message(
                "You are not the target of this request.",
                ephemeral=True
            )
            return

        guild = interaction.guild
        requester = guild.get_member(self.requester_id)
        target = guild.get_member(self.target_id)

        if not requester or not target:
            await interaction.response.send_message(
                "Could not resolve users.",
                ephemeral=True
            )
            return

        INTERACTION_PAIRS.setdefault(self.guild_id, set())
        pair_set = INTERACTION_PAIRS[self.guild_id]
        add_mutual_pair(pair_set, self.requester_id, self.target_id)
        save_consent()

        # Persist relationship metadata (symmetric)
        set_relationship_meta(
            self.guild_id,
            self.requester_id,
            self.target_id,
            self.request_type,
            self.reason,
            source_channel_id=getattr(getattr(self.message, "channel", None), "id", None),
            source_message_id=getattr(self.message, "id", None),
        )
        save_relationships()
        save_consent_messages()

        self._clear_request_record()
        save_dm_requests()

        for child in self.children:
            child.disabled = True

        success_embed = discord.Embed(
            title="✅ DM Permission Granted",
            description=(
                f"**{requester.display_name}** ↔ **{target.display_name}**\n\n"
                "Both users may now DM each other.\n"
                "Permission can be revoked with `/dm_revoke`."
            ),
            color=discord.Color.green()
        )

        success_embed.description = (
            f"**{requester.display_name}** <-> **{target.display_name}**\n"
            f"Requester: {getattr(requester, 'mention', requester.display_name)}\n"
            f"Target: {getattr(target, 'mention', target.display_name)}\n\n"
            "Both users may now DM each other.\n"
            "Permission can be revoked with `/dm_revoke`."
        )

        success_embed.add_field(
            name="Request Type",
            value=_request_type_label(self.request_type),
            inline=True
        )
        success_embed.add_field(
            name="Reason",
            value=self.reason if self.reason else "—",
            inline=False
        )

        await safe_dm_user(requester, success_embed)
        await safe_dm_user(target, success_embed)

        await interaction.response.edit_message(
            embed=success_embed,
            view=self
        )

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):

        if interaction.user.id != self.target_id:
            await interaction.response.send_message(
                "You are not the target of this request.",
                ephemeral=True
            )
            return

        for child in self.children:
            child.disabled = True

        deny_embed = discord.Embed(
            title="❌ DM Request Denied",
            description="The request was declined.",
            color=discord.Color.red()
        )
        deny_embed.add_field(
            name="Request Type",
            value=_request_type_label(self.request_type),
            inline=True
        )
        deny_embed.add_field(
            name="Reason",
            value=self.reason if self.reason else "—",
            inline=False
        )

        await interaction.response.edit_message(
            embed=deny_embed,
            view=self
        )

        # Remove stored pending request record
        self._clear_request_record()
        save_dm_requests()



# ==============================
# Slash Commands
# ==============================
@bot.tree.command(
    name="dm_help",
    description="Show an overview of the DM request system"
)
async def dm_help(interaction: discord.Interaction):

    guild = interaction.guild

    embed = discord.Embed(
        title="📬 DM Relationship System",
        description="Control how users may request DM access with you.",
        color=discord.Color.gold()
    )

    embed.title = "📬 DM Request System"

    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    embed.add_field(
        name="Your DM Modes",
        value=(
            "**OPEN** — Anyone may DM.\n"
            "**ASK** — You must approve requests.\n"
            "**CLOSED** — DM requests are blocked."
        ),
        inline=False
    )

    embed.add_field(
        name="How DM Requests Work",
        value=(
            "• Use `/dm_ask @user` to send a request.\n"
            "• Requests are sent to the configured request channel.\n"
            "• The recipient may Accept or Deny.\n"
            "• Requests expire after 24 hours.\n"
            "• Relationships persist until revoked."
        ),
        inline=False
    )

    embed.add_field(
        name="Your Commands",
        value=(
            "`/dm_info` — View your full DM status\n"
            "`/dm_set_mode` — Set your DM preference\n"
            "`/dm_ask @user` — Send DM request (type + reason)\n"
            "`/dm_revoke @user` — Revoke relationship\n"
            "`/dm_status @user` — Check relationship status\n"
        ),
        inline=False
    )

    embed.add_field(
        name="Moderator Tools",
        value=(
            "`/debug_permissions_set` — Manually create relationship\n"
            "`/debug_permissions_remove` — Remove relationship\n"
            "`/debug_permissions_list` — View all stored relationships\n"
            "`/dm_set_audit_channel` — Configure audit log channel\n"
            "`/dm_audit_user` — View per-user audit history\n"
            "`/dm_request_panel_set` — Set DM request panel channel\n"
            "`/dm_request_panel_refresh` — Repost DM request panel"
        ),
        inline=False
    )

    embed.set_footer(
        text="DM relationships are logged for audit transparency."
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="dm_info",
    description="Show your DM mode and current permission relationships"
)
async def dm_info(interaction: discord.Interaction):

    guild = interaction.guild
    member = interaction.user
    guild_id = guild.id

    # ==============================
    # Resolve Current Mode
    # ==============================
    role_names = {role.name for role in member.roles}

    if ROLE_DM_CLOSED in role_names:
        mode = "CLOSED"
        mode_desc = "No one may DM you."
    elif ROLE_DM_ASK in role_names:
        mode = "ASK"
        mode_desc = "DM requests require mutual approval."
    else:
        mode = "OPEN"
        mode_desc = "Anyone may DM you."

    # ==============================
    # Gather Permission States
    # ==============================
    pair_set = INTERACTION_PAIRS.get(guild_id, set())

    mutual = set()
    outgoing = set()
    incoming = set()

    for a, b in pair_set:
        if a == member.id:
            if (b, a) in pair_set:
                mutual.add(b)
            else:
                outgoing.add(b)

        elif b == member.id:
            if (a, b) not in pair_set:
                incoming.add(a)

    def _sorted_ids(ids: set[int]) -> list[int]:
        def _name(uid: int) -> str:
            u = guild.get_member(uid)
            return (u.display_name if u else f"Unknown({uid})").lower()
        return sorted(ids, key=_name)

    def _format_line(other_id: int) -> str:
        u = guild.get_member(other_id)
        name = u.display_name if u else f"Unknown({other_id})"

        meta = get_relationship_meta(guild_id, member.id, other_id)
        t = _request_type_label(meta.get("type"))
        reason = (meta.get("reason") or "").strip()

        if reason:
            short = reason if len(reason) <= 60 else reason[:57] + "..."
            return f"• {name} — {t} — “{short}”"
        return f"• {name} — {t}"

    mutual_lines = [_format_line(uid) for uid in _sorted_ids(mutual)]
    outgoing_lines = [_format_line(uid) for uid in _sorted_ids(outgoing)]
    incoming_lines = [_format_line(uid) for uid in _sorted_ids(incoming)]

    # ==============================
    # Build Embed
    # ==============================
    embed = discord.Embed(
        title="📬 Your DM Information",
        color=discord.Color.gold()
    )

    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    embed.add_field(
        name="Current Mode",
        value=f"**{mode}**\n{mode_desc}",
        inline=False
    )

    if mutual_lines:
        embed.add_field(
            name=f"✅ Mutual Permissions ({len(mutual_lines)})",
            value="\n".join(mutual_lines),
            inline=False
        )

    if outgoing_lines:
        embed.add_field(
            name=f"➡️ You Allowed ({len(outgoing_lines)})",
            value="\n".join(outgoing_lines),
            inline=False
        )

    if incoming_lines:
        embed.add_field(
            name=f"⬅️ They Allowed You ({len(incoming_lines)})",
            value="\n".join(incoming_lines),
            inline=False
        )

    if not (mutual_lines or outgoing_lines or incoming_lines):
        embed.add_field(
            name="No Stored Permissions",
            value="You currently have no DM permissions recorded.",
            inline=False
        )

    embed.set_footer(
        text="Use /dm_ask or /dm_revoke to manage permissions."
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="dm_set_mode",
    description="Set your DM request mode (open, ask, or closed)"
)
@app_commands.describe(mode="Choose your DM mode")
@app_commands.choices(
    mode=[
        app_commands.Choice(name="open", value="open"),
        app_commands.Choice(name="ask", value="ask"),
        app_commands.Choice(name="closed", value="closed")
    ]
)
async def dm_set_mode(interaction: discord.Interaction, mode: app_commands.Choice[str]):

    guild = interaction.guild
    member = interaction.user

    async def get_or_create(role_name):
        role = discord.utils.get(guild.roles, name=role_name)
        if role is None:
            role = await guild.create_role(
                name=role_name,
                mentionable=False,
                hoist=False,
                reason="Auto-created DM preference role"
            )
        return role

    try:
        role_open = await get_or_create(ROLE_DM_OPEN)
        role_ask = await get_or_create(ROLE_DM_ASK)
        role_closed = await get_or_create(ROLE_DM_CLOSED)
    except discord.Forbidden:
        await interaction.response.send_message(
            "I lack permission to create roles.",
            ephemeral=True
        )
        return

    # Remove ALL DM roles first
    dm_roles = [r for r in member.roles if r.name in DM_ROLE_NAMES]

    try:
        await member.remove_roles(*dm_roles)
    except discord.Forbidden:
        await interaction.response.send_message(
            "I lack permission to manage roles.",
            ephemeral=True
        )
        return

    # Assign selected role
    if mode.value == "open":
        await member.add_roles(role_open)
        status = "OPEN"
    elif mode.value == "ask":
        await member.add_roles(role_ask)
        status = "ASK"
    else:
        await member.add_roles(role_closed)
        status = "CLOSED"

    embed = discord.Embed(
        title="DM Request Mode Updated",
        description=f"You are now set to **{status}**.",
        color=discord.Color.gold()
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)



@bot.tree.command(
    name="dm_allow",
    description="Create a mutual DM permission relationship with a user"
)
@app_commands.describe(user="User to grant mutual permission with")
async def dm_allow(interaction: discord.Interaction, user: discord.Member):

    guild_id = interaction.guild.id

    if guild_id not in INTERACTION_PAIRS:
        INTERACTION_PAIRS[guild_id] = set()

    INTERACTION_PAIRS[guild_id].add((interaction.user.id, user.id))
    INTERACTION_PAIRS[guild_id].add((user.id, interaction.user.id))

    save_consent()

    # Default metadata for manual allow (assume DM)
    set_relationship_meta(guild_id, interaction.user.id, user.id, "dm", "")
    save_relationships()

    await interaction.response.send_message(
        f"You and {user.mention} may now mention each other globally."
    )

@bot.tree.command(
    name="dm_revoke",
    description="Remove DM permission relationship with another user"
)
@app_commands.describe(user="User to revoke permission with")
async def dm_revoke(interaction: discord.Interaction, user: discord.Member):

    guild_id = interaction.guild.id

    if guild_id not in INTERACTION_PAIRS:
        await interaction.response.send_message(
            "No consent records exist.",
            ephemeral=True
        )
        return

    pair_set = INTERACTION_PAIRS[guild_id]

    removed = False

    if (interaction.user.id, user.id) in pair_set:
        pair_set.remove((interaction.user.id, user.id))
        removed = True

    if (user.id, interaction.user.id) in pair_set:
        pair_set.remove((user.id, interaction.user.id))
        removed = True

    if not removed:
        await interaction.response.send_message(
            "No mutual consent existed.",
            ephemeral=True
        )
        return

    # Pull relationship meta (defaults to DM if missing)
    meta = get_relationship_meta(guild_id, interaction.user.id, user.id)
    legacy_record = CONSENT_MESSAGES.get(guild_id, {}).get(f"{interaction.user.id}:{user.id}")
    if legacy_record is None:
        legacy_record = CONSENT_MESSAGES.get(guild_id, {}).get(f"{user.id}:{interaction.user.id}")

    revoked_embed = discord.Embed(
        title="🚫 DM Permission Revoked",
        description=(
            f"**{interaction.user.display_name}** ↔ **{user.display_name}**\n\n"
            "You may no longer DM each other."
        ),
        color=discord.Color.red()
    )
    revoked_embed.add_field(
        name="Request Type",
        value=_request_type_label(meta.get("type")),
        inline=True
    )
    revoked_embed.add_field(
        name="Reason",
        value=meta.get("reason") if meta.get("reason") else "—",
        inline=False
    )

    # Remove relationship metadata
    delete_relationship_meta(guild_id, interaction.user.id, user.id)
    save_relationships()
    consent_records = CONSENT_MESSAGES.get(guild_id, {})
    consent_records.pop(f"{interaction.user.id}:{user.id}", None)
    consent_records.pop(f"{user.id}:{interaction.user.id}", None)
    if not consent_records and guild_id in CONSENT_MESSAGES:
        del CONSENT_MESSAGES[guild_id]

    # Try to update the original request message when we have a stored location.
    request_channel_id = meta.get("source_channel_id") or REQUEST_CHANNELS.get(guild_id)
    if not meta.get("source_message_id") and legacy_record:
        request_channel_id = legacy_record.get("channel_id") or request_channel_id

    if request_channel_id:
        channel = interaction.guild.get_channel(request_channel_id)
        if channel:
            message_id = meta.get("source_message_id")
            if not message_id and legacy_record:
                message_id = legacy_record.get("message_id")

            if message_id:
                try:
                    msg = await channel.fetch_message(int(message_id))
                    await msg.edit(embed=revoked_embed, view=None)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

    await safe_dm_user(interaction.user, revoked_embed)
    await safe_dm_user(user, revoked_embed)

    save_consent()

    await log_audit_event(
        interaction.guild,
        f"DM permission revoked: {interaction.user.display_name} <-> {user.display_name} (by {interaction.user.display_name})",
        action="relationship_revoked",
        actor_id=interaction.user.id,
        user1_id=interaction.user.id,
        user2_id=user.id,
        request_type=meta.get("type"),
    )

    await interaction.response.send_message(
        f"DM consent revoked with {user.mention}."
    )


@bot.tree.command(
    name="dm_status",
    description="Check whether mutual DM permission exists with a user"
)
@app_commands.describe(user="User to check permission status with")
async def dm_status(interaction: discord.Interaction, user: discord.Member):

    guild_id = interaction.guild.id
    author_id = interaction.user.id
    target_id = user.id

    allowed_pairs = INTERACTION_PAIRS.get(guild_id, set())

    mutual = (
        (author_id, target_id) in allowed_pairs and
        (target_id, author_id) in allowed_pairs
    )

    if mutual:
        result = "✅ Mutual consent active."
    else:
        result = "❌ No mutual consent."

    await interaction.response.send_message(
        f"**DM permission Status**\n\n"
        f"You ↔ {user.display_name}\n\n"
        f"{result}",
        ephemeral=True
    )

@bot.tree.command(
    name="dm_ask",
    description="Send a DM permission request to a user"
)
@app_commands.describe(
    user="User you want to contact",
    request_type="Choose DM or friend request",
    reason="Optional context shown to the recipient"
)
@app_commands.choices(
    request_type=[
        app_commands.Choice(name="Direct Message", value="dm"),
        app_commands.Choice(name="Friend Request", value="friend")
    ]
)
async def dm_ask(
    interaction: discord.Interaction,
    user: discord.Member,
    request_type: app_commands.Choice[str] | None = None,
    reason: str | None = None
):
    await _submit_dm_request(
        interaction,
        user,
        request_type.value if request_type else "dm",
        reason,
    )



@bot.tree.command(
    name="dm_request_channel_set",
    description="Set the channel where DM requests are posted"
)
@app_commands.describe(channel="Channel to send DM requests to")
async def dm_request_channel_set(
    interaction: discord.Interaction,
    channel: discord.TextChannel
):

    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message(
            "You do not have permission to configure this.",
            ephemeral=True
        )
        return

    REQUEST_CHANNELS[interaction.guild.id] = channel.id
    save_request_channels()

    await interaction.response.send_message(
        f"✅ DM requests will now be sent to {channel.mention}."
    )


@bot.tree.command(
    name="dm_request_panel_set",
    description="Set the channel that holds the DM request button panel"
)
@app_commands.describe(channel="Channel where the DM request button should stay at the bottom")
async def dm_request_panel_set(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message(
            "You do not have permission to configure the DM request panel.",
            ephemeral=True,
        )
        return

    settings = _get_panel_settings(interaction.guild.id)
    settings["panel_channel_id"] = channel.id
    PANEL_SETTINGS[interaction.guild.id] = settings
    save_panel_settings()

    message_id = await ensure_dm_request_panel_message(interaction.guild, channel.id, force_repost=True)
    if message_id is None:
        await interaction.response.send_message(
            "I could not post the DM request panel there. Check channel permissions.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"✅ DM request panel set in {channel.mention}.",
        ephemeral=True,
    )

    await log_audit_event(
        interaction.guild,
        f"DM request panel configured in {channel.mention} by {interaction.user.display_name}",
        action="dm_request_panel_set",
        actor_id=interaction.user.id,
    )


@bot.tree.command(
    name="dm_request_panel_refresh",
    description="Repost the DM request panel so it is the newest message"
)
async def dm_request_panel_refresh(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message(
            "You do not have permission to refresh the DM request panel.",
            ephemeral=True,
        )
        return

    settings = _get_panel_settings(interaction.guild.id)
    panel_channel_id = settings.get("panel_channel_id")
    if panel_channel_id is None:
        await interaction.response.send_message(
            "No DM request panel is configured. Use `/dm_request_panel_set` first.",
            ephemeral=True,
        )
        return

    message_id = await ensure_dm_request_panel_message(interaction.guild, int(panel_channel_id), force_repost=True)
    if message_id is None:
        await interaction.response.send_message(
            "I could not refresh the DM request panel in the configured channel.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        "✅ DM request panel refreshed.",
        ephemeral=True,
    )


@bot.tree.command(
    name="debug_status_check",
    description="Show your current DM mode (debug)"
)
async def debug_status_check(interaction: discord.Interaction):

    guild = interaction.guild
    member = guild.get_member(interaction.user.id)

    role_names = {role.name for role in member.roles}

    status = "OPEN"
    explanation = "Anyone may DM you."

    if ROLE_DM_CLOSED in role_names:
        status = "CLOSED"
        explanation = "No one may DM you."
    elif ROLE_DM_ASK in role_names:
        status = "ASK"
        explanation = "Mutual consent required before mentions."
    elif ROLE_DM_OPEN in role_names:
        status = "OPEN"
        explanation = "Anyone may DM you."

    await interaction.response.send_message(
        f"**Your DM Preference**\n\n"
        f"Status: **{status}**\n"
        f"{explanation}",
        ephemeral=True
    )

@bot.tree.command(
    name="debug_permissions_list",
    description="List all stored DM permission relationships (debug)"
)
async def debug_permissions_list(interaction: discord.Interaction):

    guild_id = interaction.guild.id
    guild = interaction.guild

    pairs = INTERACTION_PAIRS.get(guild_id, set())

    if not pairs:
        await interaction.response.send_message(
            "No stored DM permission permissions exist.",
            ephemeral=True
        )
        return

    # Deduplicate mirrored pairs
    unique = set()
    for a, b in pairs:
        if (b, a) not in unique:
            unique.add((a, b))

    lines = []

    for a, b in unique:
        member_a = guild.get_member(a)
        member_b = guild.get_member(b)

        name_a = member_a.display_name if member_a else f"Unknown({a})"
        name_b = member_b.display_name if member_b else f"Unknown({b})"

        lines.append(f"{name_a} ↔ {name_b}")

    output = "\n".join(lines)

    if len(output) > 1800:
        output = output[:1800] + "\n... (truncated)"

    await interaction.response.send_message(
        f"**Stored DM permission Permissions**\n\n{output}",
        ephemeral=True
    )

@bot.tree.command(
    name="debug_permissions_set",
    description="Manually create DM permission between two users (debug)"
)
@app_commands.describe(
    user1="First user",
    user2="Second user"
)
async def debug_permissions_set(
    interaction: discord.Interaction,
    user1: discord.Member,
    user2: discord.Member
):

    # Mod-only safeguard
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message(
            "You do not have permission to set permissions.",
            ephemeral=True
        )
        return

    if user1.id == user2.id:
        await interaction.response.send_message(
            "Cannot create permission between the same user.",
            ephemeral=True
        )
        return

    guild_id = interaction.guild.id

    if guild_id not in INTERACTION_PAIRS:
        INTERACTION_PAIRS[guild_id] = set()

    # Add both directions
    INTERACTION_PAIRS[guild_id].add((user1.id, user2.id))
    INTERACTION_PAIRS[guild_id].add((user2.id, user1.id))

    save_consent()

    # Default metadata for manual set (assume DM)
    set_relationship_meta(guild_id, user1.id, user2.id, "dm", "")
    save_relationships()

    await log_audit_event(
        interaction.guild,
        f"Manual DM permission set: {user1.display_name} ↔ {user2.display_name} (by {interaction.user.display_name})"
    )

    await interaction.response.send_message(
        f"✅ DM permission permission established between "
        f"{user1.mention} and {user2.mention}."
    )

@bot.tree.command(
    name="debug_permissions_remove",
    description="Manually remove DM permission between two users (debug)"
)
@app_commands.describe(
    user1="First user",
    user2="Second user"
)
async def debug_permissions_remove(
    interaction: discord.Interaction,
    user1: discord.Member,
    user2: discord.Member
):

    # Mod-only safeguard
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message(
            "You do not have permission to remove permissions.",
            ephemeral=True
        )
        return

    if user1.id == user2.id:
        await interaction.response.send_message(
            "Cannot remove permission between the same user.",
            ephemeral=True
        )
        return

    guild_id = interaction.guild.id

    if guild_id not in INTERACTION_PAIRS:
        await interaction.response.send_message(
            "No stored permissions exist in this server.",
            ephemeral=True
        )
        return

    pair_set = INTERACTION_PAIRS[guild_id]

    removed = False

    if (user1.id, user2.id) in pair_set:
        pair_set.remove((user1.id, user2.id))
        removed = True

    if (user2.id, user1.id) in pair_set:
        pair_set.remove((user2.id, user1.id))
        removed = True

    if removed:
        save_consent()
        await interaction.response.send_message(
            f"🗑️ DM permission permission removed between "
            f"{user1.mention} and {user2.mention}."
        )
    else:
        await interaction.response.send_message(
            "No mutual permission existed between those users.",
            ephemeral=True
        )
    
    await log_audit_event(
        interaction.guild,
        f"DM permission removed: {user1.display_name} ↔ {user2.display_name} (by {interaction.user.display_name})"
    )

@bot.tree.command(
    name="dm_set_audit_channel",
    description="Set the channel used for DM permission audit logs"
)
@app_commands.describe(channel="Channel to send audit logs to")
async def dm_set_audit_channel(interaction: discord.Interaction, channel: discord.TextChannel):

    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You do not have permission to configure audit logging.",
            ephemeral=True
        )
        return

    global AUDIT_LOG_CHANNEL_ID
    AUDIT_LOG_CHANNEL_ID = channel.id
    AUDIT_LOG_CHANNELS[interaction.guild.id] = channel.id
    save_audit_channels()

    await interaction.response.send_message(
        f"📜 Audit logs will now be sent to {channel.mention}."
    )

@bot.tree.command(
    name="dm_audit_user",
    description="Show DM permission audit history for a user"
)
@app_commands.describe(
    user="User to inspect",
    limit="Number of recent entries to show (default 10)"
)
async def dm_audit_user(
    interaction: discord.Interaction,
    user: discord.Member,
    limit: app_commands.Range[int, 1, 50] = 10
):

    # Admin only
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You do not have permission to view audit logs.",
            ephemeral=True
        )
        return

    guild_id = interaction.guild.id
    data = load_audit_log(guild_id=guild_id, user_id=user.id, limit=limit)

    if not data:
        data = [
            entry for entry in load_audit_log(guild_id=guild_id)
            if user.display_name in entry["message"] or str(user.id) in entry["message"]
        ][-limit:]

    if not data:
        await interaction.response.send_message(
            "No audit log entries found.",
            ephemeral=True
        )
        return

    lines = []
    for entry in reversed(data):
        lines.append(f"**{entry['timestamp']}**\n{entry['message']}\n")

    output = "\n".join(lines)

    if len(output) > 3500:
        output = output[:3500] + "\n... (truncated)"

    embed = discord.Embed(
        title=f"📜 Audit History — {user.display_name}",
        description=output,
        color=discord.Color.blurple()
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ==============================
# Run Bot
# ==============================
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set.")
    bot.run(TOKEN)
