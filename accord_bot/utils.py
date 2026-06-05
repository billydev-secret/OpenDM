"""Shared utility helpers."""

from __future__ import annotations

from typing import Any

import discord


def safe_field_text(text: str | None) -> str:
    """Escape markdown in user-supplied embed field text; return em-dash for empty."""
    if not text:
        return "—"
    return discord.utils.escape_markdown(text)


async def safe_dm_user(user: Any, embed: discord.Embed) -> None:
    """Send a DM embed, silently ignoring Forbidden/HTTP errors."""
    sender = getattr(user, "send", None)
    if sender is None:
        return
    try:
        await sender(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass


async def send_dm(user: Any, **kwargs) -> discord.Message | None:
    """Send a DM and return the Message object, or None on failure."""
    sender = getattr(user, "send", None)
    if sender is None:
        return None
    try:
        return await sender(**kwargs)
    except (discord.Forbidden, discord.HTTPException):
        return None
