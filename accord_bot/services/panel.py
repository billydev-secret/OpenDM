"""Panel settings and DM request panel management."""

from __future__ import annotations

import asyncio
import datetime
from typing import Any

import discord

from ..models.database import connect_db, ensure_database

PANEL_SETTINGS: dict[int, dict[str, int | None]] = {}
DM_REQUEST_PANEL_BUMP_GUARD: dict[int, datetime.datetime] = {}
_PANEL_LOCKS: dict[int, asyncio.Lock] = {}


def _get_panel_lock(guild_id: int) -> asyncio.Lock:
    if guild_id not in _PANEL_LOCKS:
        _PANEL_LOCKS[guild_id] = asyncio.Lock()
    return _PANEL_LOCKS[guild_id]


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
        title="📬 DM Request System",
        description=(
            "Want to reach out to someone privately? Use the button below to send them a request first.\n\n"
            "Requests are delivered straight to their DMs — nothing gets posted publicly here."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="👤 DM Status Roles",
        value=(
            "Every member has a status that controls who can reach them. "
            "You can see someone's preference right on their profile as a role:\n\n"
            "🟢 **DMs: Open** — Anyone can message them freely\n"
            "🟡 **DMs: Ask** — They want to approve requests first\n"
            "🔴 **DMs: Closed** — Not accepting requests right now\n\n"
            "Set your own preference with `/dm_set_mode`."
        ),
        inline=False,
    )
    embed.add_field(
        name="📋 How to Send a Request",
        value=(
            "1. Hit **Open DM Request Form** below\n"
            "2. Pick the person you want to reach\n"
            "3. Choose the request type\n"
            "4. Optionally write a short reason\n"
            "5. Submit — they'll get a DM from this bot with Accept / Deny buttons\n\n"
            "You'll be notified in your own DMs when they respond."
        ),
        inline=False,
    )
    embed.add_field(
        name="💬 DM vs Friend Request — what's the difference?",
        value=(
            "**Direct Message** — You just want to chat with them on this server. "
            "This does *not* send a Discord friend request; it only grants permission within this community.\n\n"
            "**Friend Request** — You'd like to add them as a Discord friend, which lets you DM them "
            "outside of this server too. Choose this if you want a longer-term connection beyond just here."
        ),
        inline=False,
    )
    embed.set_footer(text="You can revoke any connection at any time with /dm_revoke.")
    return embed


async def ensure_dm_request_panel_message(
    guild: Any,
    panel_channel_id: int,
    *,
    force_repost: bool = False,
    precheck_fn=None,
    submit_fn=None,
) -> int | None:
    async with _get_panel_lock(guild.id):
        return await _ensure_dm_request_panel_message_locked(
            guild, panel_channel_id,
            force_repost=force_repost,
            precheck_fn=precheck_fn,
            submit_fn=submit_fn,
        )


async def _ensure_dm_request_panel_message_locked(
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
    from ..commands.dm import _precheck_dm_request, _submit_dm_request
    await ensure_dm_request_panel_message(
        guild, int(panel_channel_id), force_repost=True,
        precheck_fn=_precheck_dm_request, submit_fn=_submit_dm_request,
    )
