"""Permission and relationship management service."""

from __future__ import annotations

import datetime
import re
from typing import Any

from ..models.database import connect_db, ensure_database, iter_unique_pair_rows
from .dm_roles import resolve_mode

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
INTERACTION_PAIRS: dict[int, set[tuple[int, int]]] = {}
RELATIONSHIPS: dict[int, dict[str, dict[str, Any]]] = {}
CONSENT_MESSAGES: dict[int, dict[str, dict[str, int]]] = {}
DM_REQUESTS: dict[int, dict[tuple[int, int], dict[str, Any]]] = {}
REQUEST_CHANNELS: dict[int, int] = {}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def normalize_request_type(value: str | None) -> str:
    v = (value or "").strip().lower()
    if v in {"friend", "friend_request", "fr", "friendrequest"}:
        return "friend"
    return "dm"


def request_type_label(value: str | None) -> str:
    return "Friend Request" if normalize_request_type(value) == "friend" else "Direct Message"


def relationship_key(a: int, b: int) -> str:
    lo, hi = (a, b) if a < b else (b, a)
    return f"{lo}-{hi}"


def is_mutual(guild_id: int, user1: int, user2: int) -> bool:
    pairs = INTERACTION_PAIRS.get(guild_id, set())
    return (user1, user2) in pairs and (user2, user1) in pairs


def add_mutual_pair(pair_set: set, a: int, b: int) -> None:
    pair_set.add((a, b))
    pair_set.add((b, a))


def resolve_member_from_text(guild: Any, raw: str) -> Any:
    text = str(raw or "").strip()
    if not text:
        return None
    match = re.fullmatch(r"<@!?(\d+)>", text)
    if match:
        return guild.get_member(int(match.group(1)))
    if text.isdigit():
        return guild.get_member(int(text))
    return None


def precheck_dm_request(guild: Any, requester: Any, target: Any) -> tuple[str | None, Any]:
    """Validate a DM request. Returns (error_message, request_channel)."""
    from ..config import DEBUG

    if target.id == requester.id and not DEBUG:
        return "You can't send a request to yourself!", None
    if target.bot:
        return "Bots don't accept DM requests.", None

    mode = resolve_mode(target)
    if mode == "closed":
        return f"{target.display_name} isn't accepting DM requests right now.", None
    if mode == "open" and not DEBUG:
        return f"{target.display_name} has their DMs open — no request needed, just message them!", None
    if is_mutual(guild.id, requester.id, target.id):
        return "You two already have a connection — no need to request again.", None

    request_channel_id = REQUEST_CHANNELS.get(guild.id)
    if not request_channel_id:
        return "There's no DM request channel set up yet. An admin can fix that with `/dm_request_channel_set`.", None

    request_channel = guild.get_channel(request_channel_id)
    if not request_channel:
        return "The configured request channel doesn't seem to exist anymore. An admin may need to update it.", None

    return None, request_channel


# ---------------------------------------------------------------------------
# Consent (INTERACTION_PAIRS)
# ---------------------------------------------------------------------------

def load_consent() -> None:
    global INTERACTION_PAIRS
    ensure_database()
    INTERACTION_PAIRS = {}
    with connect_db() as conn:
        rows = conn.execute("SELECT guild_id, user_low, user_high FROM consent_pairs").fetchall()
    for row in rows:
        gid = int(row["guild_id"])
        a, b = int(row["user_low"]), int(row["user_high"])
        INTERACTION_PAIRS.setdefault(gid, set())
        INTERACTION_PAIRS[gid].add((a, b))
        INTERACTION_PAIRS[gid].add((b, a))


def save_consent() -> None:
    ensure_database()
    rows = list(iter_unique_pair_rows(INTERACTION_PAIRS))
    with connect_db() as conn:
        conn.execute("DELETE FROM consent_pairs")
        if rows:
            conn.executemany(
                "INSERT INTO consent_pairs(guild_id, user_low, user_high) VALUES(?, ?, ?)",
                rows,
            )


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------

def load_relationships() -> None:
    global RELATIONSHIPS
    ensure_database()
    out: dict[int, dict[str, dict[str, Any]]] = {}
    with connect_db() as conn:
        rows = conn.execute(
            "SELECT guild_id, pair_key, request_type, reason, created_at, source_channel_id, source_message_id"
            " FROM relationships"
        ).fetchall()
    for row in rows:
        gid = int(row["guild_id"])
        out.setdefault(gid, {})[str(row["pair_key"])] = {
            "type": normalize_request_type(row["request_type"]),
            "reason": (row["reason"] or "").strip(),
            "created_at": row["created_at"],
            "source_channel_id": row["source_channel_id"],
            "source_message_id": row["source_message_id"],
        }
    RELATIONSHIPS = out
    rebuild_consent_messages()


def save_relationships() -> None:
    ensure_database()
    rows = []
    for gid, pairs in RELATIONSHIPS.items():
        for pair_key, meta in pairs.items():
            rows.append((
                gid, pair_key,
                normalize_request_type(meta.get("type")),
                (meta.get("reason") or "").strip(),
                meta.get("created_at"),
                meta.get("source_channel_id"),
                meta.get("source_message_id"),
            ))
    with connect_db() as conn:
        conn.execute("DELETE FROM relationships")
        if rows:
            conn.executemany(
                "INSERT INTO relationships"
                "(guild_id, pair_key, request_type, reason, created_at, source_channel_id, source_message_id)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)",
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
) -> None:
    key = relationship_key(a, b)
    RELATIONSHIPS.setdefault(guild_id, {})
    existing = RELATIONSHIPS[guild_id].get(key, {})
    created_at = existing.get("created_at") or datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    RELATIONSHIPS[guild_id][key] = {
        "type": normalize_request_type(request_type),
        "reason": (reason or "").strip(),
        "created_at": created_at,
        "source_channel_id": source_channel_id if source_channel_id is not None else existing.get("source_channel_id"),
        "source_message_id": source_message_id if source_message_id is not None else existing.get("source_message_id"),
    }


def get_relationship_meta(guild_id: int, a: int, b: int) -> dict:
    key = relationship_key(a, b)
    meta = RELATIONSHIPS.get(guild_id, {}).get(key)
    if not isinstance(meta, dict):
        return {"type": "dm", "reason": "", "created_at": None, "source_channel_id": None, "source_message_id": None}
    return {
        "type": normalize_request_type(meta.get("type")),
        "reason": (meta.get("reason") or "").strip(),
        "created_at": meta.get("created_at"),
        "source_channel_id": meta.get("source_channel_id"),
        "source_message_id": meta.get("source_message_id"),
    }


def delete_relationship_meta(guild_id: int, a: int, b: int) -> None:
    key = relationship_key(a, b)
    if guild_id in RELATIONSHIPS and key in RELATIONSHIPS[guild_id]:
        del RELATIONSHIPS[guild_id][key]
        if not RELATIONSHIPS[guild_id]:
            del RELATIONSHIPS[guild_id]


def reconcile_relationship_defaults() -> None:
    changed = False
    for guild_id, pairs in INTERACTION_PAIRS.items():
        seen: set[str] = set()
        for a, b in pairs:
            if (b, a) not in pairs:
                continue
            key = relationship_key(a, b)
            if key in seen:
                continue
            seen.add(key)
            meta = RELATIONSHIPS.get(guild_id, {}).get(key)
            if not isinstance(meta, dict):
                set_relationship_meta(guild_id, a, b, "dm", "")
                changed = True
                continue
            t, r, ca = meta.get("type"), meta.get("reason"), meta.get("created_at")
            if t is None or r is None or ca is None:
                set_relationship_meta(guild_id, a, b, t or "dm", r or "")
                changed = True
    if changed:
        save_relationships()


def rebuild_consent_messages() -> None:
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
                requester_id, target_id = int(a_str), int(b_str)
            except (AttributeError, TypeError, ValueError):
                continue
            out.setdefault(guild_id, {})[f"{requester_id}:{target_id}"] = {
                "channel_id": int(channel_id),
                "message_id": int(message_id),
                "requester_id": requester_id,
                "target_id": target_id,
            }
    CONSENT_MESSAGES = out


def load_consent_messages() -> None:
    rebuild_consent_messages()


def save_consent_messages() -> None:
    rebuild_consent_messages()


# ---------------------------------------------------------------------------
# DM Requests
# ---------------------------------------------------------------------------

def load_dm_requests() -> None:
    global DM_REQUESTS
    ensure_database()
    out: dict[int, dict[tuple[int, int], dict[str, Any]]] = {}
    with connect_db() as conn:
        rows = conn.execute(
            "SELECT guild_id, requester_id, target_id, message_id, request_type, reason, created_at FROM dm_requests"
        ).fetchall()
    for row in rows:
        gid = int(row["guild_id"])
        out.setdefault(gid, {})
        out[gid][(int(row["requester_id"]), int(row["target_id"]))] = {
            "message_id": int(row["message_id"]),
            "request_type": normalize_request_type(row["request_type"]),
            "reason": (row["reason"] or "").strip(),
            "created_at": row["created_at"],
        }
    DM_REQUESTS = out


def save_dm_requests() -> None:
    ensure_database()
    rows = []
    for gid, pairs in DM_REQUESTS.items():
        for (requester_id, target_id), record in pairs.items():
            message_id = int(record.get("message_id") or 0)
            if message_id <= 0:
                continue
            rows.append((
                gid, requester_id, target_id, message_id,
                normalize_request_type(record.get("request_type")),
                (record.get("reason") or "").strip(),
                record.get("created_at"),
            ))
    with connect_db() as conn:
        conn.execute("DELETE FROM dm_requests")
        if rows:
            conn.executemany(
                "INSERT INTO dm_requests"
                "(guild_id, requester_id, target_id, message_id, request_type, reason, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)",
                rows,
            )


# ---------------------------------------------------------------------------
# Request channels
# ---------------------------------------------------------------------------

def load_request_channels() -> None:
    global REQUEST_CHANNELS
    ensure_database()
    with connect_db() as conn:
        rows = conn.execute("SELECT guild_id, channel_id FROM request_channels").fetchall()
    REQUEST_CHANNELS = {int(r["guild_id"]): int(r["channel_id"]) for r in rows}


def save_request_channels() -> None:
    ensure_database()
    rows = list(REQUEST_CHANNELS.items())
    with connect_db() as conn:
        conn.execute("DELETE FROM request_channels")
        if rows:
            conn.executemany("INSERT INTO request_channels(guild_id, channel_id) VALUES(?, ?)", rows)
