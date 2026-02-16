ROLE_DM_OPEN = "DMs: Open"
ROLE_DM_ASK = "DMs: Ask"
ROLE_DM_CLOSED = "DMs: Closed"

DM_ROLE_NAMES = {
    ROLE_DM_OPEN,
    ROLE_DM_ASK,
    ROLE_DM_CLOSED,
}


def resolve_mode(member) -> str:
    role_names = {role.name for role in member.roles}

    if ROLE_DM_CLOSED in role_names:
        return "closed"

    if ROLE_DM_ASK in role_names:
        return "ask"

    return "open"
