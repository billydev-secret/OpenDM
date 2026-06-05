"""Bot class, instance, and Discord event handlers."""

from __future__ import annotations

import asyncio
import datetime
import logging

import discord
from discord import app_commands

from .config import DEBUG, GUILD_ID
from .constants import DM_ROLE_NAMES
from .models.database import ensure_database
from .services.permissions import (
    DM_REQUESTS,
    load_consent,
    load_dm_requests,
    load_relationships,
    load_request_channels,
    reconcile_relationship_defaults,
    request_type_label,
    save_consent,
    save_dm_requests,
    save_relationships,
    save_request_channels,
)
from .services.audit import load_audit_channels, log_audit_event, save_audit_channels
from .services.panel import (
    PANEL_SETTINGS,
    bump_dm_request_panel_if_needed,
    ensure_dm_request_panel_message,
    load_panel_settings,
    save_panel_settings,
)
from .utils import safe_dm_user, safe_field_text
from .views.panel import DmRequestPanelView

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("accord")

intents = discord.Intents.default()
intents.members = True

_expiry_task: asyncio.Task | None = None


async def _expire_stale_requests(bot_instance: discord.Client) -> None:
    """Expire DM requests that have been pending for over 24 hours."""
    now = datetime.datetime.utcnow()
    expired = []
    for guild_id, requests in list(DM_REQUESTS.items()):
        for (requester_id, target_id), record in list(requests.items()):
            try:
                created_at = datetime.datetime.strptime(
                    record.get("created_at", ""), "%Y-%m-%d %H:%M:%S UTC"
                )
            except (ValueError, TypeError):
                continue
            if (now - created_at).total_seconds() >= 86400:
                expired.append((guild_id, requester_id, target_id, dict(record)))

    for guild_id, requester_id, target_id, _ in expired:
        DM_REQUESTS.get(guild_id, {}).pop((requester_id, target_id), None)

    if expired:
        save_dm_requests()

    for guild_id, requester_id, target_id, record in expired:
        guild = bot_instance.get_guild(guild_id)
        if guild is None:
            continue
        requester = guild.get_member(requester_id)
        target = guild.get_member(target_id)
        req_type = record.get("request_type", "dm")
        target_name = target.display_name if target else str(target_id)
        requester_name = requester.display_name if requester else str(requester_id)

        if requester:
            exp_embed = discord.Embed(
                title="⌛ Request expired",
                description=(
                    f"Your {request_type_label(req_type).lower()} request "
                    f"to **{target_name}** in **{guild.name}** expired after 24 hours "
                    "without a response."
                ),
                color=discord.Color.orange(),
            )
            exp_embed.add_field(name="Request Type", value=request_type_label(req_type), inline=True)
            exp_embed.add_field(name="Reason", value=safe_field_text(record.get("reason", "")), inline=False)
            await safe_dm_user(requester, exp_embed)

        await log_audit_event(
            guild,
            f"DM request expired: {requester_name} ➝ {target_name} ({request_type_label(req_type)})",
            action="request_expired",
            user1_id=requester_id,
            user2_id=target_id,
            request_type=req_type,
        )


async def _expiry_loop(bot_instance: discord.Client) -> None:
    await bot_instance.wait_until_ready()
    while not bot_instance.is_closed():
        try:
            await _expire_stale_requests(bot_instance)
        except Exception:
            log.exception("DM request expiry sweep failed")
        await asyncio.sleep(60 * 60)


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        ensure_database()
        from .commands.dm import AskConsentView, _precheck_dm_request, _submit_dm_request

        # Persistent view — handles Accept/Deny clicks that survive bot restarts.
        # State is recovered from in-memory DM_REQUESTS (loaded in on_ready).
        self.add_view(AskConsentView())
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
    global _expiry_task
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

    if _expiry_task is None or _expiry_task.done():
        _expiry_task = asyncio.create_task(_expiry_loop(bot))


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
    global _expiry_task
    if _expiry_task is not None:
        _expiry_task.cancel()
        _expiry_task = None
    save_consent()
    save_dm_requests()
    save_relationships()
    save_request_channels()
    save_audit_channels()
    save_panel_settings()


@bot.event
async def on_message(message):
    await bump_dm_request_panel_if_needed(message)
