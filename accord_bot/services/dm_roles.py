"""DM role resolution logic."""

from ..constants import ROLE_DM_ASK, ROLE_DM_CLOSED, ROLE_DM_OPEN


def resolve_mode(member) -> str:
    """Return the DM mode for a member based on their roles."""
    role_names = {role.name for role in member.roles}

    if ROLE_DM_CLOSED in role_names:
        return "closed"

    if ROLE_DM_OPEN in role_names:
        return "open"

    return "ask"
