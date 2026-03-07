# DM Permissions Bot

A Discord bot that manages DM permission workflows using slash commands.

## What it does

- Supports three DM modes per user: `open`, `ask`, `closed`
- Lets users request DM/friend access through a configured request channel
- Stores mutual permission relationships
- Allows revoking permissions at any time
- Writes audit events to SQLite and optional audit channels

## Requirements

- Python 3.10+
- Discord bot token

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:
   - `pip install discord.py python-dotenv`
3. Create a `.env` file in the repo root:
   - `DISCORD_TOKEN=your_bot_token`
   - `GUILD_ID=your_test_guild_id` (optional, recommended for debug sync)
   - `DEBUG=true` (optional; enables guild-only command sync when `GUILD_ID` is set)
   - `ACCORD_DB_FILE=accord.db` (optional)
4. Run the bot:
   - `python accord.py`

## Slash Commands

### User commands

- `/dm_help` - Show an overview of the DM request system.
- `/dm_info` - Show your DM mode and current permission relationships.
- `/dm_set_mode mode:(open|ask|closed)` - Set your DM request mode.
- `/dm_ask user request_type?(dm|friend) reason?` - Send a DM permission request.
- `/dm_status user` - Check whether mutual DM permission exists with a user.
- `/dm_revoke user` - Remove DM permission relationship with another user.
- `/dm_allow user` - Create a mutual DM permission relationship directly.

### Server configuration commands

- `/dm_request_channel_set channel` - Set the channel where DM requests are posted. Requires `Manage Channels`.
- `/dm_request_panel_set channel` - Set the channel that hosts the DM request modal button panel. Requires `Manage Channels`.
- `/dm_request_panel_refresh` - Repost the DM request panel so the button stays the newest message. Requires `Manage Channels`.
- `/dm_set_audit_channel channel` - Set the channel used for DM permission audit logs. Requires `Manage Server`.
- `/dm_audit_user user limit?` - Show DM permission audit history for a user. Requires `Manage Server`.

### Debug/moderation commands

- `/debug_status_check` - Show your current DM mode (debug).
- `/debug_permissions_list` - List stored DM permission relationships (debug).
- `/debug_permissions_set user1 user2` - Manually create DM permission between two users (debug). Requires `Manage Roles`.
- `/debug_permissions_remove user1 user2` - Manually remove DM permission between two users (debug). Requires `Manage Roles`.

## Data storage

- `accord.db` - SQLite database storing all state, relationships, requests, and audit logs.
