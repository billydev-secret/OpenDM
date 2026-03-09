"""Panel settings and DM request panel management."""

from __future__ import annotations

import datetime
from typing import Any

import discord

from ..models.database import connect_db, ensure_database

PANEL_SETTINGS: dict[int, dict[str, int | None]] = {}
DM_REQUEST_PANEL_BUMP_GUARD: dict[int, datetime.datetime] = {}


def _default_panel_settings() -> dict[str, int | None]:
    return {"panel_channel_id": None, "panel_message_id": None}


def get_panel_settings(guild_id: int) -> dict[str, int | None]:
    current = PANEL_SETTINGS.get(guild_id)
    if not isinstance(current, dict):
        current = _default_panel_settings()
        PANEL_SETTINGS[guild_id] = current
        return current
    for key, value in _default_panel_settings().items():
        current.setdefault(key, value)
    return current


def load_panel_settings() -> None:
    global PANEL_SETTINGS
    ensure_database()
    out: dict[int, dict[str, int | None]] = {}
    with connect_db() as conn:
        rows = conn.execute(
            "SELECT guild_id, panel_channel_id, panel_message_id FROM dm_panel_settings"
        ).fetchall()
    for row in rows:
        gid = int(row["guild_id"])
        out[gid] = {
            "panel_channel_id": int(row["panel_channel_id"]) if row["panel_channel_id"] is not None else None,
            "panel_message_id": int(row["panel_message_id"]) if row["panel_message_id"] is not None else None,
        }
    PANEL_SETTINGS = out


def save_panel_settings() -> None:
    ensure_database()
    rows = [
        (int(gid), s.get("panel_channel_id"), s.get("panel_message_id"), None)
        for gid, s in PANEL_SETTINGS.items()
    ]
    with connect_db() as conn:
        conn.execute("DELETE FROM dm_panel_settings")
        if rows:
            conn.executemany(
                "INSERT INTO dm_panel_settings"
                "(guild_id, panel_channel_id, panel_message_id, target_channel_id)"
                " VALUES(?, ?, ?, ?)",
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
    guild: Any,
    panel_channel_id: int,
    *,
    force_repost: bool = False,
    precheck_fn=None,
    submit_fn=None,
) -> int | None:
    from ..views.panel import DmRequestPanelView

    channel = guild.get_channel(panel_channel_id)
    if channel is None:
        return None

    settings = get_panel_settings(guild.id)
    old_message_id = settings.get("panel_message_id")

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
            await existing.edit(embed=_build_dm_request_panel_embed(), view=DmRequestPanelView(precheck_fn=precheck_fn, submit_fn=submit_fn))
            settings["panel_channel_id"] = int(panel_channel_id)
            PANEL_SETTINGS[guild.id] = settings
            save_panel_settings()
            return int(existing.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            settings["panel_message_id"] = None

    try:
        new_message = await channel.send(embed=_build_dm_request_panel_embed(), view=DmRequestPanelView(precheck_fn=precheck_fn, submit_fn=submit_fn))
    except (discord.Forbidden, discord.HTTPException):
        return None

    new_message_id = int(new_message.id)
    settings["panel_channel_id"] = int(panel_channel_id)
    settings["panel_message_id"] = new_message_id
    PANEL_SETTINGS[guild.id] = settings
    save_panel_settings()

    if old_message_id and int(old_message_id) != new_message_id:
        try:
            old = await channel.fetch_message(int(old_message_id))
            await old.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
            pass

    return new_message_id


async def bump_dm_request_panel_if_needed(message: Any) -> None:
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

    now = datetime.datetime.utcnow()
    last = DM_REQUEST_PANEL_BUMP_GUARD.get(guild.id)
    if last and (now - last).total_seconds() < 2:
        return

    DM_REQUEST_PANEL_BUMP_GUARD[guild.id] = now
    await ensure_dm_request_panel_message(guild, int(panel_channel_id), force_repost=True)
