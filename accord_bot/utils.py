"""Shared utility helpers."""

from __future__ import annotations

from typing import Any

import discord


async def safe_dm_user(user: Any, embed: discord.Embed) -> None:
    """Send a DM embed, silently ignoring Forbidden/HTTP errors."""
    sender = getattr(user, "send", None)
    if sender is None:
        return
    try:
        await sender(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass
