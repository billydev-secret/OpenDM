import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock


class DummyResponse:
    def __init__(self):
        self.send_message = AsyncMock()
        self.edit_message = AsyncMock()


def make_role(name, position=1):
    return SimpleNamespace(name=name, position=position)


def make_member(member_id=1, display_name="User", roles=None, bot=False, perms=None):
    return SimpleNamespace(
        id=member_id,
        display_name=display_name,
        mention=f"<@{member_id}>",
        roles=roles or [],
        bot=bot,
        display_avatar=SimpleNamespace(url="https://example.com/avatar.png"),
        guild_permissions=perms or SimpleNamespace(
            manage_channels=False,
            manage_roles=False,
            manage_guild=False,
        ),
        remove_roles=AsyncMock(),
        add_roles=AsyncMock(),
    )


def make_guild(guild_id=123, icon=None, members=None, channels=None, roles=None):
    members = members or {}
    channels = channels or {}
    roles = roles or []

    async def _create_role(name, mentionable, hoist, reason):
        role = make_role(name=name, position=len(roles) + 1)
        roles.append(role)
        return role

    return SimpleNamespace(
        id=guild_id,
        icon=icon,
        roles=roles,
        get_member=lambda member_id: members.get(member_id),
        get_channel=lambda channel_id: channels.get(channel_id),
        create_role=AsyncMock(side_effect=_create_role),
    )


def make_interaction(guild, user):
    return SimpleNamespace(guild=guild, user=user, response=DummyResponse())


def run(coro):
    return asyncio.run(coro)


def get_sent_text(mock):
    if mock.await_args.args:
        return mock.await_args.args[0]
    return mock.await_args.kwargs.get("content")


def test_command_registration_includes_all_slash_commands(accord_module):
    command_names = {cmd.name for cmd in accord_module.bot.tree.get_commands()}
    expected = {
        "dm_help",
        "dm_info",
        "dm_set_mode",
        "dm_allow",
        "dm_revoke",
        "dm_status",
        "dm_ask",
        "dm_request_channel_set",
        "debug_status_check",
        "debug_permissions_list",
        "debug_permissions_set",
        "debug_permissions_remove",
        "dm_set_audit_channel",
        "dm_audit_user",
        "dm_request_panel_set",
        "dm_request_panel_refresh",
    }
    assert expected.issubset(command_names)


def test_dm_help_sends_ephemeral_embed(accord_module):
    user = make_member()
    guild = make_guild(icon=None)
    interaction = make_interaction(guild, user)

    run(accord_module.dm_help(interaction))

    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert kwargs["embed"].title == "📬 DM Request System"


def test_dm_info_shows_no_permissions_when_empty(accord_module):
    user = make_member(member_id=7, roles=[])
    guild = make_guild(members={7: user})
    interaction = make_interaction(guild, user)

    run(accord_module.dm_info(interaction))

    kwargs = interaction.response.send_message.await_args.kwargs
    field_names = [field.name for field in kwargs["embed"].fields]
    assert "No Stored Permissions" in field_names


def test_dm_set_mode_updates_roles_and_responds(accord_module):
    role_open = make_role("DMs: Open", position=1)
    role_ask = make_role("DMs: Ask", position=2)
    role_closed = make_role("DMs: Closed", position=3)

    user = make_member(roles=[role_open])
    guild = make_guild(roles=[role_open, role_ask, role_closed])
    interaction = make_interaction(guild, user)

    run(accord_module.dm_set_mode(interaction, SimpleNamespace(value="ask")))

    user.remove_roles.assert_awaited_once()
    user.add_roles.assert_awaited_once_with(role_ask)
    assert interaction.response.send_message.await_args.kwargs["embed"].description.endswith("**ASK**.")


def test_dm_allow_stores_mutual_pair(accord_module, monkeypatch):
    monkeypatch.setattr(accord_module, "save_consent", lambda: None)

    requester = make_member(member_id=10)
    target = make_member(member_id=20)
    guild = make_guild(guild_id=555)
    interaction = make_interaction(guild, requester)

    run(accord_module.dm_allow(interaction, target))

    assert (10, 20) in accord_module.INTERACTION_PAIRS[555]
    assert (20, 10) in accord_module.INTERACTION_PAIRS[555]


def test_dm_revoke_without_existing_records_returns_ephemeral(accord_module):
    requester = make_member(member_id=10)
    target = make_member(member_id=20, display_name="Target")
    guild = make_guild(guild_id=555)
    interaction = make_interaction(guild, requester)

    run(accord_module.dm_revoke(interaction, target))

    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert get_sent_text(interaction.response.send_message) == "No consent records exist."


def test_dm_status_reports_mutual_consent(accord_module):
    accord_module.INTERACTION_PAIRS = {444: {(1, 2), (2, 1)}}

    requester = make_member(member_id=1)
    target = make_member(member_id=2, display_name="Friend")
    guild = make_guild(guild_id=444)
    interaction = make_interaction(guild, requester)

    run(accord_module.dm_status(interaction, target))

    content = get_sent_text(interaction.response.send_message)
    assert "Mutual consent active" in content


def test_dm_ask_requires_configured_channel_when_missing(accord_module):
    requester = make_member(member_id=1, display_name="Requester")
    target = make_member(member_id=2, display_name="Target", roles=[make_role("DMs: Ask")])
    guild = make_guild(guild_id=123)
    interaction = make_interaction(guild, requester)

    run(accord_module.dm_ask(interaction, target))

    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert "No DM request channel has been configured" in get_sent_text(interaction.response.send_message)


def test_dm_request_channel_set_updates_channel_when_permitted(accord_module, monkeypatch):
    monkeypatch.setattr(accord_module, "save_request_channels", lambda: None)

    requester = make_member(
        member_id=1,
        perms=SimpleNamespace(manage_channels=True, manage_roles=False, manage_guild=False),
    )
    guild = make_guild(guild_id=999)
    interaction = make_interaction(guild, requester)
    channel = SimpleNamespace(id=77, mention="#requests")

    run(accord_module.dm_request_channel_set(interaction, channel))

    assert accord_module.REQUEST_CHANNELS[999] == 77
    assert "#requests" in get_sent_text(interaction.response.send_message)


def test_dm_request_panel_set_updates_channel_when_permitted(accord_module, monkeypatch):
    monkeypatch.setattr(accord_module, "save_panel_settings", lambda: None)

    async def _fake_ensure(guild, panel_channel_id, force_repost=False):
        return 12345

    monkeypatch.setattr(accord_module, "ensure_dm_request_panel_message", _fake_ensure)
    monkeypatch.setattr(accord_module, "log_audit_event", AsyncMock())

    requester = make_member(
        member_id=1,
        perms=SimpleNamespace(manage_channels=True, manage_roles=False, manage_guild=False),
    )
    guild = make_guild(guild_id=999)
    interaction = make_interaction(guild, requester)
    channel = SimpleNamespace(id=88, mention="#dm-request-panel")

    run(accord_module.dm_request_panel_set(interaction, channel))

    settings = accord_module.PANEL_SETTINGS[999]
    assert settings["panel_channel_id"] == 88
    assert "#dm-request-panel" in get_sent_text(interaction.response.send_message)


def test_debug_status_check_reports_open_state(accord_module):
    user = make_member(member_id=8)
    guild = make_guild(guild_id=101, members={8: user})
    interaction = make_interaction(guild, user)

    run(accord_module.debug_status_check(interaction))

    sent = get_sent_text(interaction.response.send_message)
    assert "Status: **OPEN**" in sent


def test_debug_permissions_list_handles_empty_pairs(accord_module):
    user = make_member(member_id=1)
    guild = make_guild(guild_id=101)
    interaction = make_interaction(guild, user)

    run(accord_module.debug_permissions_list(interaction))

    kwargs = interaction.response.send_message.await_args.kwargs
    assert get_sent_text(interaction.response.send_message) == "No stored DM permission permissions exist."
    assert kwargs["ephemeral"] is True


def test_debug_permissions_set_requires_manage_roles_permission(accord_module):
    actor = make_member(member_id=1)
    user1 = make_member(member_id=2)
    user2 = make_member(member_id=3)
    guild = make_guild(guild_id=202)
    interaction = make_interaction(guild, actor)

    run(accord_module.debug_permissions_set(interaction, user1, user2))

    kwargs = interaction.response.send_message.await_args.kwargs
    assert get_sent_text(interaction.response.send_message) == "You do not have permission to set permissions."
    assert kwargs["ephemeral"] is True


def test_debug_permissions_remove_requires_manage_roles_permission(accord_module):
    actor = make_member(member_id=1)
    user1 = make_member(member_id=2)
    user2 = make_member(member_id=3)
    guild = make_guild(guild_id=202)
    interaction = make_interaction(guild, actor)

    run(accord_module.debug_permissions_remove(interaction, user1, user2))

    kwargs = interaction.response.send_message.await_args.kwargs
    assert get_sent_text(interaction.response.send_message) == "You do not have permission to remove permissions."
    assert kwargs["ephemeral"] is True


def test_dm_set_audit_channel_requires_manage_guild_permission(accord_module):
    actor = make_member(member_id=1)
    guild = make_guild(guild_id=202)
    interaction = make_interaction(guild, actor)
    channel = SimpleNamespace(id=99, mention="#audit")

    run(accord_module.dm_set_audit_channel(interaction, channel))

    kwargs = interaction.response.send_message.await_args.kwargs
    assert get_sent_text(interaction.response.send_message) == "You do not have permission to configure audit logging."
    assert kwargs["ephemeral"] is True


def test_accept_embed_lists_both_consented_users(accord_module, monkeypatch):
    monkeypatch.setattr(accord_module, "save_consent", lambda: None)
    monkeypatch.setattr(accord_module, "save_consent_messages", lambda: None)

    requester = make_member(member_id=10, display_name="Requester")
    target = make_member(member_id=20, display_name="Target")
    guild = make_guild(guild_id=333, members={10: requester, 20: target})
    interaction = make_interaction(guild, target)

    view = accord_module.AskConsentView(requester_id=10, target_id=20)
    view.message = SimpleNamespace(id=999, channel=SimpleNamespace(id=321))

    run(view.accept(interaction, None))

    embed = interaction.response.edit_message.await_args.kwargs["embed"]
    assert "Requester: <@10>" in embed.description
    assert "Target: <@20>" in embed.description


def test_dm_revoke_updates_existing_grant_embed(accord_module, monkeypatch):
    monkeypatch.setattr(accord_module, "save_consent", lambda: None)

    requester = make_member(member_id=10, display_name="Requester")
    target = make_member(member_id=20, display_name="Target")

    message = SimpleNamespace(edit=AsyncMock())
    channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
    guild = make_guild(guild_id=555, channels={77: channel})
    interaction = make_interaction(guild, requester)

    accord_module.INTERACTION_PAIRS = {555: {(10, 20), (20, 10)}}
    accord_module.CONSENT_MESSAGES = {
        555: {
            "10:20": {
                "channel_id": 77,
                "message_id": 88,
                "requester_id": 10,
                "target_id": 20,
            }
        }
    }

    run(accord_module.dm_revoke(interaction, target))

    message.edit.assert_awaited_once()
    revoked_embed = message.edit.await_args.kwargs["embed"]
    assert revoked_embed.title == "🚫 DM Permission Revoked"
