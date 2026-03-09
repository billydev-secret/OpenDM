"""Audit logging service."""

from __future__ import annotations

import datetime
import logging
from typing import Any

import discord

from ..models.database import connect_db, ensure_database
from .permissions import normalize_request_type

log = logging.getLogger(__name__)

AUDIT_LOG_CHANNEL_ID: int | None = None
AUDIT_LOG_CHANNELS: dict[int, int] = {}


def load_audit_channels() -> None:
    global AUDIT_LOG_CHANNELS, AUDIT_LOG_CHANNEL_ID
    ensure_database()
    with connect_db() as conn:
        rows = conn.execute("SELECT guild_id, channel_id FROM audit_channels").fetchall()
    AUDIT_LOG_CHANNELS = {int(r["guild_id"]): int(r["channel_id"]) for r in rows}
    AUDIT_LOG_CHANNEL_ID = next(iter(AUDIT_LOG_CHANNELS.values()), None)


def save_audit_channels() -> None:
    ensure_database()
    rows = list(AUDIT_LOG_CHANNELS.items())
    with connect_db() as conn:
        conn.execute("DELETE FROM audit_channels")
        if rows:
            conn.executemany("INSERT INTO audit_channels(guild_id, channel_id) VALUES(?, ?)", rows)


def load_audit_log(
    guild_id: int | None = None,
    user_id: int | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    ensure_database()
    query = [
        "SELECT timestamp, guild_id, action, message, actor_id, user1_id, user2_id, request_type FROM audit_log"
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

    with connect_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


async def log_audit_event(
    guild: Any,
    message: str,
    *,
    action: str = "generic",
    actor_id: int | None = None,
    user1_id: int | None = None,
    user2_id: int | None = None,
    request_type: str | None = None,
) -> None:
    timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    ensure_database()
    with connect_db() as conn:
        conn.execute(
            "INSERT INTO audit_log"
            "(timestamp, guild_id, action, message, actor_id, user1_id, user2_id, request_type)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                timestamp, guild.id, action, message, actor_id, user1_id, user2_id,
                normalize_request_type(request_type) if request_type else None,
            ),
        )
    log.info({
        "timestamp": timestamp, "guild_id": guild.id, "action": action,
        "message": message, "actor_id": actor_id, "user1_id": user1_id, "user2_id": user2_id,
    })

    channel_id = AUDIT_LOG_CHANNELS.get(guild.id) or AUDIT_LOG_CHANNEL_ID
    if channel_id:
        channel = guild.get_channel(channel_id)
        if channel:
            embed = discord.Embed(
                title="📜 DM Permission Audit",
                description=message,
                color=discord.Color.blurple(),
            )
            embed.set_footer(text=timestamp)
            await channel.send(embed=embed)
