"""Bot configuration from environment variables."""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


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


TOKEN: str | None = os.getenv("DISCORD_TOKEN")
GUILD_ID: int | None = _get_int_env("GUILD_ID")
DEBUG: bool = _get_bool_env("DEBUG", False)
