"""DM permission slash commands, consent flow, and AskConsentView."""

from __future__ import annotations

import datetime
import logging

import discord

log = logging.getLogger(__name__)
from discord import app_commands

from ..constants import DM_ROLE_NAMES, ROLE_DM_ASK, ROLE_DM_CLOSED, ROLE_DM_OPEN

# From-imports make these patchable on this module (critical for test monkeypatching)
from ..services.permissions import (
    CONSENT_MESSAGES,
    DM_REQUESTS,
    INTERACTION_PAIRS,
    REQUEST_CHANNELS,
    add_mutual_pair,
    delete_relationship_meta,
    get_relationship_meta,
    normalize_request_type,
    precheck_dm_request as _precheck_dm_request_svc,
    request_type_label,
    save_consent,
    save_consent_messages,
    save_dm_requests,
    save_relationships,
    save_request_channels,
    set_relationship_meta,
)
from ..services.audit import (
    AUDIT_LOG_CHANNELS,
    load_audit_log,
    log_audit_event,
    save_audit_channels,
)
import accord_bot.services.audit as _audit_svc

from ..services.panel import (
    PANEL_SETTINGS,
    ensure_dm_request_panel_message,
    get_panel_settings,
    save_panel_settings,
)
from ..utils import safe_dm_user


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _precheck_dm_request(guild, requester, target):
    return _precheck_dm_request_svc(guild, requester, target)


async def _submit_dm_request(interaction, user, request_type, reason):
    guild = interaction.guild
    guild_id = guild.id
    requester = interaction.user

    req_type = normalize_request_type(request_type or "dm")
    reason_clean = str(reason or "").strip()
    if len(reason_clean) > 256:
        reason_clean = reason_clean[:253] + "..."

    error_message, request_channel = _precheck_dm_request(guild, requester, user)
    if error_message:
        await interaction.response.send_message(error_message, ephemeral=True)
        return

    if request_channel is None:
        await interaction.response.send_message(
            "Configured DM request channel is invalid.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    embed = discord.Embed(
        title="📨 Someone wants to connect with you",
        description=(
            f"{user.mention}\n\n"
            "This request expires in 24 hours."
        ),
        color=discord.Color.gold(),
    )
    embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
    embed.set_footer(text="You can revoke this permission at any time with /dm_revoke")
    embed.add_field(name="Request Type", value=request_type_label(req_type), inline=True)
    embed.add_field(name="Reason", value=reason_clean if reason_clean else "—", inline=False)

    view = AskConsentView(
        requester_id=requester.id,
        target_id=user.id,
        guild_id=guild_id,
        request_type=req_type,
        reason=reason_clean,
    )

    try:
        message = await request_channel.send(
            content=user.mention,
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions(users=[user]),
        )
        await message.edit(content=None)
        view.message = message

        DM_REQUESTS.setdefault(guild_id, {})
        DM_REQUESTS[guild_id][(requester.id, user.id)] = {
            "message_id": message.id,
            "request_type": req_type,
            "reason": reason_clean,
            "created_at": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        }
        save_dm_requests()

    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to post in the configured request channel.",
            ephemeral=True,
        )
        return
    except (discord.NotFound, discord.HTTPException) as exc:
        log.error("Failed to post DM request message: %s", exc)
        await interaction.followup.send(
            "Something went wrong sending the request — the channel might be unavailable.", ephemeral=True
        )
        return

    await log_audit_event(
        interaction.guild,
        f"DM request asked: {interaction.user.display_name} ➝ {user.display_name} ({request_type_label(req_type)})",
    )
    await interaction.followup.send(
        f"📨 Request sent! They'll see it in {request_channel.mention}.",
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# AskConsentView
# ---------------------------------------------------------------------------

class AskConsentView(discord.ui.View):
    def __init__(self, requester_id, target_id, guild_id=0, request_type="dm", reason=""):
        super().__init__(timeout=86400)
        self.requester_id = requester_id
        self.target_id = target_id
        self.guild_id = guild_id
        self.request_type = normalize_request_type(request_type)
        self.reason = (reason or "").strip()
        self.message = None

    def _clear_request_record(self):
        recs = DM_REQUESTS.get(self.guild_id, {})
        if (self.requester_id, self.target_id) in recs:
            del recs[(self.requester_id, self.target_id)]
        if not recs and self.guild_id in DM_REQUESTS:
            del DM_REQUESTS[self.guild_id]

    async def on_timeout(self):
        if self.message:
            for child in self.children:
                child.disabled = True
            timeout_embed = discord.Embed(
                title="⌛ Request expired",
                description="This one didn't get a response in time — it's been 24 hours.",
                color=discord.Color.orange(),
            )
            timeout_embed.add_field(
                name="Request Type", value=request_type_label(self.request_type), inline=True
            )
            timeout_embed.add_field(
                name="Reason", value=self.reason if self.reason else "—", inline=False
            )
            try:
                await self.message.edit(embed=timeout_embed, view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Could not update expired request message: %s", exc)
            self._clear_request_record()
            save_dm_requests()

            guild = self.message.guild
            if guild:
                requester = guild.get_member(self.requester_id)
                target = guild.get_member(self.target_id)
                requester_name = requester.display_name if requester else str(self.requester_id)
                target_name = target.display_name if target else str(self.target_id)
                await log_audit_event(
                    guild,
                    f"DM request expired: {requester_name} ➝ {target_name} ({request_type_label(self.request_type)})",
                    action="request_expired",
                    actor_id=None,
                    user1_id=self.requester_id,
                    user2_id=self.target_id,
                    request_type=self.request_type,
                )

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target_id:
            await interaction.response.send_message(
                "This request isn't for you.", ephemeral=True
            )
            return

        guild = interaction.guild
        requester = guild.get_member(self.requester_id)
        target = guild.get_member(self.target_id)

        if not requester or not target:
            await interaction.response.send_message("Couldn't find one or both users in this server.", ephemeral=True)
            return

        INTERACTION_PAIRS.setdefault(self.guild_id, set())
        add_mutual_pair(INTERACTION_PAIRS[self.guild_id], self.requester_id, self.target_id)

        set_relationship_meta(
            self.guild_id,
            self.requester_id,
            self.target_id,
            self.request_type,
            self.reason,
            source_channel_id=getattr(getattr(self.message, "channel", None), "id", None),
            source_message_id=getattr(self.message, "id", None),
        )
        self._clear_request_record()

        try:
            save_consent()
            save_relationships()
            save_consent_messages()
            save_dm_requests()
        except Exception:
            log.exception(
                "Failed to persist consent grant for guild=%s pair=(%s, %s); "
                "in-memory state is ahead of the database",
                self.guild_id, self.requester_id, self.target_id,
            )

        for child in self.children:
            child.disabled = True

        success_embed = discord.Embed(
            title="✅ Connection accepted!",
            color=discord.Color.green(),
        )
        success_embed.description = (
            f"**{requester.display_name}** <-> **{target.display_name}**\n"
            f"{getattr(requester, 'mention', requester.display_name)} and {getattr(target, 'mention', target.display_name)} can now DM each other.\n\n"
            "Either of you can undo this at any time with `/dm_revoke`."
        )
        success_embed.add_field(
            name="Request Type", value=request_type_label(self.request_type), inline=True
        )
        success_embed.add_field(
            name="Reason", value=self.reason if self.reason else "—", inline=False
        )

        await interaction.response.edit_message(embed=success_embed, view=self)
        await safe_dm_user(requester, success_embed)
        await safe_dm_user(target, success_embed)

        await log_audit_event(
            guild,
            f"DM request accepted: {requester.display_name} ↔ {target.display_name} ({request_type_label(self.request_type)})",
            action="request_accepted",
            actor_id=self.target_id,
            user1_id=self.requester_id,
            user2_id=self.target_id,
            request_type=self.request_type,
        )

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target_id:
            await interaction.response.send_message(
                "This request isn't for you.", ephemeral=True
            )
            return

        for child in self.children:
            child.disabled = True

        deny_embed = discord.Embed(
            title="❌ Request declined",
            description="No worries — the request was turned down.",
            color=discord.Color.red(),
        )
        deny_embed.add_field(
            name="Request Type", value=request_type_label(self.request_type), inline=True
        )
        deny_embed.add_field(
            name="Reason", value=self.reason if self.reason else "—", inline=False
        )
        await interaction.response.edit_message(embed=deny_embed, view=self)
        self._clear_request_record()
        save_dm_requests()

        guild = interaction.guild
        requester = guild.get_member(self.requester_id)
        target = guild.get_member(self.target_id)
        requester_name = requester.display_name if requester else str(self.requester_id)
        target_name = target.display_name if target else str(self.target_id)
        await log_audit_event(
            guild,
            f"DM request denied: {requester_name} ➝ {target_name} ({request_type_label(self.request_type)})",
            action="request_denied",
            actor_id=self.target_id,
            user1_id=self.requester_id,
            user2_id=self.target_id,
            request_type=self.request_type,
        )


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

async def dm_help(interaction: discord.Interaction):
    guild = interaction.guild
    embed = discord.Embed(
        title="📬 DM Request System",
        description="Control how users may request DM access with you.",
        color=discord.Color.gold(),
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(
        name="Your DM Modes",
        value=(
            "**OPEN** — Anyone may DM.\n"
            "**ASK** — You must approve requests.\n"
            "**CLOSED** — DM requests are blocked."
        ),
        inline=False,
    )
    embed.add_field(
        name="How DM Requests Work",
        value=(
            "• Use `/dm_ask @user` to send a request.\n"
            "• Requests are sent to the configured request channel.\n"
            "• The recipient may Accept or Deny.\n"
            "• Requests expire after 24 hours.\n"
            "• Relationships persist until revoked."
        ),
        inline=False,
    )
    embed.add_field(
        name="Your Commands",
        value=(
            "`/dm_info` — View your full DM status\n"
            "`/dm_set_mode` — Set your DM preference\n"
            "`/dm_ask @user` — Send DM request (type + reason)\n"
            "`/dm_revoke @user` — Revoke relationship\n"
            "`/dm_status @user` — Check relationship status\n"
        ),
        inline=False,
    )
    embed.add_field(
        name="Moderator Tools",
        value=(
            "`/debug_permissions_set` — Manually create relationship\n"
            "`/debug_permissions_remove` — Remove relationship\n"
            "`/debug_permissions_list` — View all stored relationships\n"
            "`/dm_set_audit_channel` — Configure audit log channel\n"
            "`/dm_audit_user` — View per-user audit history\n"
            "`/dm_request_panel_set` — Set DM request panel channel\n"
            "`/dm_request_panel_refresh` — Repost DM request panel"
        ),
        inline=False,
    )
    embed.set_footer(text="DM relationships are logged for audit transparency.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def dm_info(interaction: discord.Interaction):
    guild = interaction.guild
    member = interaction.user
    guild_id = guild.id
    role_names = {role.name for role in member.roles}

    if ROLE_DM_CLOSED in role_names:
        mode, mode_desc = "CLOSED", "No one may DM you."
    elif ROLE_DM_ASK in role_names:
        mode, mode_desc = "ASK", "DM requests require mutual approval."
    else:
        mode, mode_desc = "OPEN", "Anyone may DM you."

    pair_set = INTERACTION_PAIRS.get(guild_id, set())
    mutual: set[int] = set()
    outgoing: set[int] = set()
    incoming: set[int] = set()

    for a, b in pair_set:
        if a == member.id:
            if (b, a) in pair_set:
                mutual.add(b)
            else:
                outgoing.add(b)
        elif b == member.id:
            if (a, b) not in pair_set:
                incoming.add(a)

    def _sorted_ids(ids):
        def _name(uid):
            u = guild.get_member(uid)
            return (u.display_name if u else f"Unknown({uid})").lower()
        return sorted(ids, key=_name)

    def _format_line(other_id):
        u = guild.get_member(other_id)
        name = u.display_name if u else f"Unknown({other_id})"
        meta = get_relationship_meta(guild_id, member.id, other_id)
        t = request_type_label(meta.get("type"))
        reason = (meta.get("reason") or "").strip()
        if reason:
            short = reason if len(reason) <= 60 else reason[:57] + "..."
            return f"• {name} — {t} — \"{short}\""
        return f"• {name} — {t}"

    mutual_lines = [_format_line(uid) for uid in _sorted_ids(mutual)]
    outgoing_lines = [_format_line(uid) for uid in _sorted_ids(outgoing)]
    incoming_lines = [_format_line(uid) for uid in _sorted_ids(incoming)]

    embed = discord.Embed(title="📬 Your DM Information", color=discord.Color.gold())
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Current Mode", value=f"**{mode}**\n{mode_desc}", inline=False)

    if mutual_lines:
        embed.add_field(
            name=f"✅ Mutual Permissions ({len(mutual_lines)})",
            value="\n".join(mutual_lines),
            inline=False,
        )
    if outgoing_lines:
        embed.add_field(
            name=f"➡️ You Allowed ({len(outgoing_lines)})",
            value="\n".join(outgoing_lines),
            inline=False,
        )
    if incoming_lines:
        embed.add_field(
            name=f"⬅️ They Allowed You ({len(incoming_lines)})",
            value="\n".join(incoming_lines),
            inline=False,
        )
    if not (mutual_lines or outgoing_lines or incoming_lines):
        embed.add_field(
            name="No Stored Permissions",
            value="You currently have no DM permissions recorded.",
            inline=False,
        )
    embed.set_footer(text="Use /dm_ask or /dm_revoke to manage permissions.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def dm_set_mode(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    guild = interaction.guild
    member = interaction.user

    async def get_or_create(role_name):
        role = discord.utils.get(guild.roles, name=role_name)
        if role is None:
            role = await guild.create_role(
                name=role_name, mentionable=False, hoist=False,
                reason="Auto-created DM preference role",
            )
        return role

    await interaction.response.defer(ephemeral=True)

    try:
        role_open = await get_or_create(ROLE_DM_OPEN)
        role_ask = await get_or_create(ROLE_DM_ASK)
        role_closed = await get_or_create(ROLE_DM_CLOSED)
    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to create roles here.", ephemeral=True)
        return

    dm_roles = [r for r in member.roles if r.name in DM_ROLE_NAMES]
    try:
        await member.remove_roles(*dm_roles)
    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to manage roles here.", ephemeral=True)
        return

    if mode.value == "open":
        await member.add_roles(role_open)
        status = "OPEN"
    elif mode.value == "ask":
        await member.add_roles(role_ask)
        status = "ASK"
    else:
        await member.add_roles(role_closed)
        status = "CLOSED"

    embed = discord.Embed(
        title="DM preference updated",
        description=f"You're now set to **{status}**.",
        color=discord.Color.gold(),
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


async def dm_allow(interaction: discord.Interaction, user: discord.Member):
    guild_id = interaction.guild.id
    INTERACTION_PAIRS.setdefault(guild_id, set())
    add_mutual_pair(INTERACTION_PAIRS[guild_id], interaction.user.id, user.id)
    save_consent()
    set_relationship_meta(guild_id, interaction.user.id, user.id, "dm", "")
    save_relationships()
    await interaction.response.send_message(
        f"You and {user.mention} may now mention each other globally."
    )


async def dm_revoke(interaction: discord.Interaction, user: discord.Member):
    guild_id = interaction.guild.id

    if guild_id not in INTERACTION_PAIRS:
        await interaction.response.send_message("There are no connections to remove.", ephemeral=True)
        return

    pair_set = INTERACTION_PAIRS[guild_id]
    removed = False
    if (interaction.user.id, user.id) in pair_set:
        pair_set.remove((interaction.user.id, user.id))
        removed = True
    if (user.id, interaction.user.id) in pair_set:
        pair_set.remove((user.id, interaction.user.id))
        removed = True

    if not removed:
        await interaction.response.send_message(f"You don't have a connection with {user.display_name}.", ephemeral=True)
        return

    meta = get_relationship_meta(guild_id, interaction.user.id, user.id)
    legacy_record = CONSENT_MESSAGES.get(guild_id, {}).get(f"{interaction.user.id}:{user.id}")
    if legacy_record is None:
        legacy_record = CONSENT_MESSAGES.get(guild_id, {}).get(f"{user.id}:{interaction.user.id}")

    revoked_embed = discord.Embed(
        title="🚫 Connection removed",
        description=(
            f"**{interaction.user.display_name}** ↔ **{user.display_name}**\n\n"
            "The DM connection between you two has been removed."
        ),
        color=discord.Color.red(),
    )
    revoked_embed.add_field(
        name="Request Type", value=request_type_label(meta.get("type")), inline=True
    )
    revoked_embed.add_field(
        name="Reason", value=meta.get("reason") if meta.get("reason") else "—", inline=False
    )

    delete_relationship_meta(guild_id, interaction.user.id, user.id)
    save_relationships()
    consent_records = CONSENT_MESSAGES.get(guild_id, {})
    consent_records.pop(f"{interaction.user.id}:{user.id}", None)
    consent_records.pop(f"{user.id}:{interaction.user.id}", None)
    if not consent_records and guild_id in CONSENT_MESSAGES:
        del CONSENT_MESSAGES[guild_id]

    request_channel_id = meta.get("source_channel_id") or REQUEST_CHANNELS.get(guild_id)
    if not meta.get("source_message_id") and legacy_record:
        request_channel_id = legacy_record.get("channel_id") or request_channel_id

    if request_channel_id:
        channel = interaction.guild.get_channel(request_channel_id)
        if channel:
            message_id = meta.get("source_message_id")
            if not message_id and legacy_record:
                message_id = legacy_record.get("message_id")
            if message_id:
                try:
                    msg = await channel.fetch_message(int(message_id))
                    await msg.edit(embed=revoked_embed, view=None)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

    await safe_dm_user(interaction.user, revoked_embed)
    await safe_dm_user(user, revoked_embed)
    save_consent()

    await log_audit_event(
        interaction.guild,
        f"DM permission revoked: {interaction.user.display_name} <-> {user.display_name} (by {interaction.user.display_name})",
        action="relationship_revoked",
        actor_id=interaction.user.id,
        user1_id=interaction.user.id,
        user2_id=user.id,
        request_type=meta.get("type"),
    )
    await interaction.response.send_message(f"Done — your connection with {user.mention} has been removed.")


async def dm_status(interaction: discord.Interaction, user: discord.Member):
    guild_id = interaction.guild.id
    allowed_pairs = INTERACTION_PAIRS.get(guild_id, set())
    mutual = (
        (interaction.user.id, user.id) in allowed_pairs
        and (user.id, interaction.user.id) in allowed_pairs
    )
    result = "✅ You two are connected." if mutual else "❌ No connection yet."
    await interaction.response.send_message(
        f"**DM status — you & {user.display_name}**\n\n{result}",
        ephemeral=True,
    )


async def dm_ask(
    interaction: discord.Interaction,
    user: discord.Member,
    request_type: app_commands.Choice[str] | None = None,
    reason: str | None = None,
):
    await _submit_dm_request(
        interaction, user, request_type.value if request_type else "dm", reason
    )


async def dm_request_channel_set(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message(
            "You need the Manage Channels permission to do that.", ephemeral=True
        )
        return
    REQUEST_CHANNELS[interaction.guild.id] = channel.id
    save_request_channels()
    await interaction.response.send_message(
        f"✅ DM requests will now go to {channel.mention}."
    )


async def dm_request_panel_set(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message(
            "You need the Manage Channels permission to do that.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    settings = get_panel_settings(interaction.guild.id)
    settings["panel_channel_id"] = channel.id
    PANEL_SETTINGS[interaction.guild.id] = settings
    save_panel_settings()

    message_id = await ensure_dm_request_panel_message(
        interaction.guild, channel.id, force_repost=True,
        precheck_fn=_precheck_dm_request, submit_fn=_submit_dm_request,
    )
    if message_id is None:
        await interaction.followup.send(
            "I couldn't post the panel there — double check that I have permission to send messages in that channel.", ephemeral=True
        )
        return

    await interaction.followup.send(
        f"✅ Panel is live in {channel.mention}.", ephemeral=True
    )
    await log_audit_event(
        interaction.guild,
        f"DM request panel configured in {channel.mention} by {interaction.user.display_name}",
        action="dm_request_panel_set",
        actor_id=interaction.user.id,
    )


async def dm_request_panel_refresh(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message(
            "You need the Manage Channels permission to do that.", ephemeral=True
        )
        return

    settings = get_panel_settings(interaction.guild.id)
    panel_channel_id = settings.get("panel_channel_id")
    if panel_channel_id is None:
        await interaction.response.send_message(
            "No panel is set up yet — use `/dm_request_panel_set` to get started.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    message_id = await ensure_dm_request_panel_message(
        interaction.guild, int(panel_channel_id), force_repost=True,
        precheck_fn=_precheck_dm_request, submit_fn=_submit_dm_request,
    )
    if message_id is None:
        await interaction.followup.send(
            "Couldn't refresh the panel — I may not have permission to post in that channel.", ephemeral=True
        )
        return

    await interaction.followup.send("✅ Panel bumped to the bottom.", ephemeral=True)


async def dm_set_audit_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You need the Manage Server permission to do that.", ephemeral=True
        )
        return

    _audit_svc.AUDIT_LOG_CHANNEL_ID = channel.id
    AUDIT_LOG_CHANNELS[interaction.guild.id] = channel.id
    save_audit_channels()
    await interaction.response.send_message(f"📜 Audit logs will now go to {channel.mention}.")


async def dm_audit_user(
    interaction: discord.Interaction,
    user: discord.Member,
    limit: app_commands.Range[int, 1, 50] = 10,
):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You need the Manage Server permission to view audit logs.", ephemeral=True
        )
        return

    guild_id = interaction.guild.id
    data = load_audit_log(guild_id=guild_id, user_id=user.id, limit=limit)

    if not data:
        data = [
            entry for entry in load_audit_log(guild_id=guild_id)
            if user.display_name in entry["message"] or str(user.id) in entry["message"]
        ][-limit:]

    if not data:
        await interaction.response.send_message("No audit entries found for that user.", ephemeral=True)
        return

    lines = [f"**{entry['timestamp']}**\n{entry['message']}\n" for entry in reversed(data)]
    output = "\n".join(lines)
    if len(output) > 3500:
        output = output[:3500] + "\n... (truncated)"

    embed = discord.Embed(
        title=f"📜 Audit History — {user.display_name}",
        description=output,
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Command registration
# ---------------------------------------------------------------------------

def setup(bot) -> None:
    """Register all DM commands on the bot's command tree."""
    tree = bot.tree

    tree.command(name="dm_help", description="Show an overview of the DM request system")(dm_help)

    tree.command(name="dm_info", description="Show your DM mode and current permission relationships")(dm_info)

    _dm_set_mode = app_commands.choices(mode=[
        app_commands.Choice(name="open", value="open"),
        app_commands.Choice(name="ask", value="ask"),
        app_commands.Choice(name="closed", value="closed"),
    ])(app_commands.describe(mode="Choose your DM mode")(dm_set_mode))
    tree.command(name="dm_set_mode", description="Set your DM request mode (open, ask, or closed)")(_dm_set_mode)

    tree.command(name="dm_allow", description="Create a mutual DM permission relationship with a user")(
        app_commands.describe(user="User to grant mutual permission with")(dm_allow)
    )

    tree.command(name="dm_revoke", description="Remove DM permission relationship with another user")(
        app_commands.describe(user="User to revoke permission with")(dm_revoke)
    )

    tree.command(name="dm_status", description="Check whether mutual DM permission exists with a user")(
        app_commands.describe(user="User to check permission status with")(dm_status)
    )

    _dm_ask = app_commands.choices(request_type=[
        app_commands.Choice(name="Direct Message", value="dm"),
        app_commands.Choice(name="Friend Request", value="friend"),
    ])(app_commands.describe(
        user="User you want to contact",
        request_type="Choose DM or friend request",
        reason="Optional context shown to the recipient",
    )(dm_ask))
    tree.command(name="dm_ask", description="Send a DM permission request to a user")(_dm_ask)

    tree.command(name="dm_request_channel_set", description="Set the channel where DM requests are posted")(
        app_commands.describe(channel="Channel to send DM requests to")(dm_request_channel_set)
    )

    tree.command(name="dm_request_panel_set", description="Set the channel that holds the DM request button panel")(
        app_commands.describe(channel="Channel where the DM request button should stay at the bottom")(dm_request_panel_set)
    )

    tree.command(name="dm_request_panel_refresh", description="Repost the DM request panel so it is the newest message")(dm_request_panel_refresh)

    tree.command(name="dm_set_audit_channel", description="Set the channel used for DM permission audit logs")(
        app_commands.describe(channel="Channel to send audit logs to")(dm_set_audit_channel)
    )

    tree.command(name="dm_audit_user", description="Show DM permission audit history for a user")(
        app_commands.describe(user="User to inspect", limit="Number of recent entries to show (default 10)")(dm_audit_user)
    )
