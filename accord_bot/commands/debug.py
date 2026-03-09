"""Debug and moderator slash commands for DM permissions."""

from __future__ import annotations

import discord
from discord import app_commands

from ..constants import ROLE_DM_ASK, ROLE_DM_CLOSED, ROLE_DM_OPEN
from ..services.permissions import (
    INTERACTION_PAIRS,
    add_mutual_pair,
    save_consent,
    save_relationships,
    set_relationship_meta,
)
from ..services.audit import log_audit_event


async def debug_status_check(interaction: discord.Interaction):
    guild = interaction.guild
    member = guild.get_member(interaction.user.id)
    role_names = {role.name for role in member.roles}

    if ROLE_DM_CLOSED in role_names:
        status, explanation = "CLOSED", "No one may DM you."
    elif ROLE_DM_ASK in role_names:
        status, explanation = "ASK", "Mutual consent required before mentions."
    elif ROLE_DM_OPEN in role_names:
        status, explanation = "OPEN", "Anyone may DM you."
    else:
        status, explanation = "OPEN", "Anyone may DM you."

    await interaction.response.send_message(
        f"**Your DM Preference**\n\nStatus: **{status}**\n{explanation}",
        ephemeral=True,
    )


async def debug_permissions_list(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    guild = interaction.guild
    pairs = INTERACTION_PAIRS.get(guild_id, set())

    if not pairs:
        await interaction.response.send_message(
            "No stored DM permission permissions exist.", ephemeral=True
        )
        return

    unique: set[tuple[int, int]] = set()
    for a, b in pairs:
        if (b, a) not in unique:
            unique.add((a, b))

    lines = []
    for a, b in unique:
        member_a = guild.get_member(a)
        member_b = guild.get_member(b)
        name_a = member_a.display_name if member_a else f"Unknown({a})"
        name_b = member_b.display_name if member_b else f"Unknown({b})"
        lines.append(f"{name_a} ↔ {name_b}")

    output = "\n".join(lines)
    if len(output) > 1800:
        output = output[:1800] + "\n... (truncated)"

    await interaction.response.send_message(
        f"**Stored DM permission Permissions**\n\n{output}", ephemeral=True
    )


async def debug_permissions_set(
    interaction: discord.Interaction,
    user1: discord.Member,
    user2: discord.Member,
):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message(
            "You do not have permission to set permissions.", ephemeral=True
        )
        return

    if user1.id == user2.id:
        await interaction.response.send_message(
            "Cannot create permission between the same user.", ephemeral=True
        )
        return

    guild_id = interaction.guild.id
    INTERACTION_PAIRS.setdefault(guild_id, set())
    add_mutual_pair(INTERACTION_PAIRS[guild_id], user1.id, user2.id)
    save_consent()
    set_relationship_meta(guild_id, user1.id, user2.id, "dm", "")
    save_relationships()

    await log_audit_event(
        interaction.guild,
        f"Manual DM permission set: {user1.display_name} ↔ {user2.display_name} (by {interaction.user.display_name})",
    )
    await interaction.response.send_message(
        f"✅ DM permission permission established between {user1.mention} and {user2.mention}."
    )


async def debug_permissions_remove(
    interaction: discord.Interaction,
    user1: discord.Member,
    user2: discord.Member,
):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message(
            "You do not have permission to remove permissions.", ephemeral=True
        )
        return

    if user1.id == user2.id:
        await interaction.response.send_message(
            "Cannot remove permission between the same user.", ephemeral=True
        )
        return

    guild_id = interaction.guild.id
    if guild_id not in INTERACTION_PAIRS:
        await interaction.response.send_message(
            "No stored permissions exist in this server.", ephemeral=True
        )
        return

    pair_set = INTERACTION_PAIRS[guild_id]
    removed = False
    if (user1.id, user2.id) in pair_set:
        pair_set.remove((user1.id, user2.id))
        removed = True
    if (user2.id, user1.id) in pair_set:
        pair_set.remove((user2.id, user1.id))
        removed = True

    if removed:
        save_consent()
        await interaction.response.send_message(
            f"🗑️ DM permission permission removed between {user1.mention} and {user2.mention}."
        )
    else:
        await interaction.response.send_message(
            "No mutual permission existed between those users.", ephemeral=True
        )

    await log_audit_event(
        interaction.guild,
        f"DM permission removed: {user1.display_name} ↔ {user2.display_name} (by {interaction.user.display_name})",
    )


def setup(bot) -> None:
    """Register all debug commands on the bot's command tree."""
    tree = bot.tree

    tree.command(name="debug_status_check", description="Show your current DM mode (debug)")(debug_status_check)

    tree.command(name="debug_permissions_list", description="List all stored DM permission relationships (debug)")(debug_permissions_list)

    tree.command(name="debug_permissions_set", description="Manually create DM permission between two users (debug)")(
        app_commands.describe(user1="First user", user2="Second user")(debug_permissions_set)
    )

    tree.command(name="debug_permissions_remove", description="Manually remove DM permission between two users (debug)")(
        app_commands.describe(user1="First user", user2="Second user")(debug_permissions_remove)
    )
