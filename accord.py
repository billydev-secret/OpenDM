"""Entry point: wire up commands and run the bot."""

from dotenv import load_dotenv
load_dotenv()  # Must run before accord_bot.config is imported

from accord_bot.bot import bot
from accord_bot.commands import dm as _dm_cmds, debug as _debug_cmds

_dm_cmds.setup(bot)
_debug_cmds.setup(bot)

# Re-export everything tests rely on via accord_module fixture
from accord_bot.commands.dm import (  # noqa: E402, F401
    AskConsentView,
    _precheck_dm_request,
    _submit_dm_request,
    INTERACTION_PAIRS,
    CONSENT_MESSAGES,
    DM_REQUESTS,
    REQUEST_CHANNELS,
    PANEL_SETTINGS,
    AUDIT_LOG_CHANNELS,
    save_consent,
    save_consent_messages,
    save_dm_requests,
    save_relationships,
    save_request_channels,
    save_panel_settings,
    ensure_dm_request_panel_message,
    log_audit_event,
    dm_help,
    dm_info,
    dm_set_mode,
    dm_allow,
    dm_revoke,
    dm_status,
    dm_ask,
    dm_request_channel_set,
    dm_request_panel_set,
    dm_request_panel_refresh,
    dm_set_audit_channel,
    dm_audit_user,
)
from accord_bot.commands.debug import (  # noqa: F401
    debug_status_check,
    debug_permissions_list,
    debug_permissions_set,
    debug_permissions_remove,
)
import accord_bot.services.audit as _audit_svc

AUDIT_LOG_CHANNEL_ID = _audit_svc.AUDIT_LOG_CHANNEL_ID

if __name__ == "__main__":
    from accord_bot.config import TOKEN
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set.")
    bot.run(TOKEN)
