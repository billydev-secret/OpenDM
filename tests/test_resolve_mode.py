from types import SimpleNamespace

from accord_bot.services.dm_roles import resolve_mode


def _member_with_roles(*role_names: str):
    roles = [SimpleNamespace(name=name) for name in role_names]
    return SimpleNamespace(roles=roles)


def test_resolve_mode_returns_closed_when_closed_role_present():
    member = _member_with_roles("DMs: Open", "DMs: Closed")
    assert resolve_mode(member) == "closed"


def test_resolve_mode_returns_ask_when_ask_role_present():
    member = _member_with_roles("DMs: Ask")
    assert resolve_mode(member) == "ask"


def test_resolve_mode_defaults_to_open_without_matching_role():
    member = _member_with_roles("Moderator")
    assert resolve_mode(member) == "open"
