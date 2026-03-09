"""Bot class, instance, and Discord event handlers."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from .config import DEBUG, GUILD_ID
from .constants import DM_ROLE_NAMES
from .models.database import ensure_database
from .services.permissions import (
    load_consent,
    load_dm_requests,
    load_relationships,
    load_request_channels,
    reconcile_relationship_defaults,
    save_consent,
    save_dm_requests,
    save_relationships,
    save_request_channels,
)
from .services.audit import load_audit_channels, save_audit_channels
from .services.panel import (
    PANEL_SETTINGS,
    bump_dm_request_panel_if_needed,
    ensure_dm_request_panel_message,
    load_panel_settings,
    save_panel_settings,
)
from .views.panel import DmRequestPanelView

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("accord")

intents = discord.Intents.default()
intents.members = True


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        ensure_database()
        from .commands.dm import _precheck_dm_request, _submit_dm_request

        self.add_view(DmRequestPanelView(
            precheck_fn=_precheck_dm_request,
            submit_fn=_submit_dm_request,
        ))

        if DEBUG and GUILD_ID is not None:
            guild = discord.Object(id=GUILD_ID)
            self.tree.clear_commands(guild=guild)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced to dev guild.")
        else:
            if DEBUG and GUILD_ID is None:
                log.warning("DEBUG is enabled but GUILD_ID is not set. Syncing commands globally.")
            await self.tree.sync()
            log.info("Synced globally.")


bot = Bot()


@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

    if not DEBUG:
        for guild in bot.guilds:
            bot.tree.clear_commands(guild=guild)
            await bot.tree.sync(guild=guild)
        log.info("Cleared guild-specific commands for %d guild(s).", len(bot.guilds))

    load_consent()
    load_dm_requests()
    load_relationships()
    reconcile_relationship_defaults()
    load_request_channels()
    load_audit_channels()
    load_panel_settings()

    from .commands.dm import _precheck_dm_request, _submit_dm_request

    for guild in bot.guilds:
        settings = PANEL_SETTINGS.get(guild.id)
        if not isinstance(settings, dict):
            continue
        panel_channel_id = settings.get("panel_channel_id")
        if panel_channel_id is None:
            continue
        await ensure_dm_request_panel_message(
            guild, int(panel_channel_id), force_repost=False,
            precheck_fn=_precheck_dm_request, submit_fn=_submit_dm_request,
        )


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    after_dm = [r for r in after.roles if r.name in DM_ROLE_NAMES]
    if len(after_dm) > 1:
        keep = max(after_dm, key=lambda r: r.position)
        remove = [r for r in after_dm if r != keep]
        try:
            await after.remove_roles(*remove)
        except discord.Forbidden:
            pass


@bot.event
async def on_disconnect():
    save_consent()
    save_dm_requests()
    save_relationships()
    save_request_channels()
    save_audit_channels()
    save_panel_settings()


@bot.event
async def on_message(message):
    await bump_dm_request_panel_if_needed(message)
