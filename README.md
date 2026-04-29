# Accord — Discord DM Permissions Bot

A Discord bot for managing consensual DM and friend request workflows between server members. Users set a preference (open / ask / closed), others send requests through a button panel or slash command, and accepted requests create a mutual permission that persists until either party revokes it. All activity is audit-logged.

---

## Features

- **DM mode roles** — three modes enforced via auto-managed Discord roles: Open, Ask, Closed
- **Request workflow** — requesters send DM/friend requests with an optional reason; recipients accept or deny via buttons with a 24-hour window
- **Mutual permissions** — accepted requests create a bidirectional consent record; either party can revoke at any time
- **Interactive panel** — a persistent button panel in a designated channel lets members pick a recipient and request type without typing commands
- **Audit log** — every action is logged to SQLite and optionally posted to a Discord channel in real time

---

## Commands

### User commands

| Command | Description |
|---|---|
| `/dm_help` | Overview of the DM request system |
| `/dm_info` | Your current mode and all permission relationships |
| `/dm_set_mode mode` | Set your mode: `open`, `ask`, or `closed` |
| `/dm_ask user [request_type] [reason]` | Send a DM or friend request to a user |
| `/dm_status user` | Check whether you have mutual permission with a user |
| `/dm_allow user` | Instantly create a mutual permission without a request |
| `/dm_revoke user` | Remove your permission relationship with a user |
| `/invite` | Get an invite link for the bot |

### Server configuration (requires Manage Channels / Manage Server)

| Command | Description |
|---|---|
| `/dm_request_channel_set channel` | Channel where request notifications are posted |
| `/dm_request_panel_set channel` | Channel for the interactive request panel |
| `/dm_request_panel_refresh` | Repost the panel button so it stays at the bottom |
| `/dm_set_audit_channel channel` | Channel for real-time audit log posts |
| `/dm_audit_user user [limit]` | View audit history for a user (default 10, max 50) |

### Debug / moderation (requires Manage Roles)

| Command | Description |
|---|---|
| `/debug_status_check` | Check your current DM mode |
| `/debug_permissions_list` | List all stored permission relationships |
| `/debug_permissions_set user1 user2` | Manually create a permission |
| `/debug_permissions_remove user1 user2` | Manually remove a permission |

---

## Setup

### Prerequisites

- Python 3.10+
- A Discord bot token with **Message Content Intent** and **Server Members Intent** enabled in the [Developer Portal](https://discord.com/developers/applications)

### Install

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Configure

Create a `.env` file in the project root:

```env
DISCORD_TOKEN=your_bot_token_here
GUILD_ID=your_test_server_id      # optional — speeds up command sync during dev
DEBUG=false                        # set true to sync commands to GUILD_ID only
ACCORD_DB_FILE=accord.db          # optional — path to SQLite database
```

### Run

```bash
python accord.py
```

On first run the bot creates the SQLite database and registers slash commands. With `DEBUG=true` and a `GUILD_ID` set, commands sync instantly to that server instead of globally (global sync can take up to an hour).

---

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

---

## Project structure

```
accord.py                      # entry point
accord_bot/
  bot.py                       # bot instance, startup/shutdown handlers
  constants.py                 # role names, view IDs
  utils.py                     # shared DM sending helpers
  commands/
    dm.py                      # user-facing commands, accept/deny views
    debug.py                   # moderation/debug commands
  services/
    permissions.py             # permission state, load/save
    audit.py                   # audit log management
    panel.py                   # panel config and repost logic
    dm_roles.py                # DM mode resolution
  views/
    panel.py                   # interactive button panel views
  models/
    database.py                # SQLite schema and utilities
```

---

## Required bot permissions

- Manage Roles (create and assign DM mode roles)
- Send Messages
- Read Message History
- Use Slash Commands
