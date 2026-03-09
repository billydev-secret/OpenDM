"""DM request panel UI views."""

from __future__ import annotations

import discord

from ..constants import DM_REQUEST_PANEL_VIEW_ID
from ..services.permissions import normalize_request_type, request_type_label


def _build_picker_prompt(selected_user_id: int | None) -> str:
    user_line = f"<@{selected_user_id}>" if selected_user_id is not None else "nobody yet"
    return (
        "**Who do you want to reach out to?**\n"
        f"Selected: {user_line}\n\n"
        "Pick someone from the list, choose what kind of request it is, then hit Continue."
    )


class DmRequestReasonModal(discord.ui.Modal):
    def __init__(self, target_user_id: int, request_type: str, submit_fn):
        super().__init__(title="Send DM Request")
        self.target_user_id = target_user_id
        self.request_type = normalize_request_type(request_type)
        self._submit_fn = submit_fn
        self.reason_input = discord.ui.TextInput(
            label="Want to add a reason? (optional)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=256,
            placeholder="Give them a heads up — why do you want to connect?",
        )
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This only works inside a server, not in DMs.", ephemeral=True
            )
            return
        target_user = guild.get_member(self.target_user_id)
        if target_user is None:
            await interaction.response.send_message(
                "Hmm, couldn't find that person in this server.", ephemeral=True
            )
            return
        await self._submit_fn(
            interaction, target_user, self.request_type, str(self.reason_input.value or "")
        )


class DmRequestUserSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Select a user...", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, DmRequestLookupView):
            return
        view.selected_user_id = self.values[0].id
        await interaction.response.edit_message(
            content=_build_picker_prompt(view.selected_user_id),
            view=view,
        )


class DmRequestLookupView(discord.ui.View):
    def __init__(self, precheck_fn, submit_fn):
        super().__init__(timeout=300)
        self.selected_user_id: int | None = None
        self.request_type: str = "dm"
        self._precheck_fn = precheck_fn
        self._submit_fn = submit_fn
        self.add_item(DmRequestUserSelect())

    def _sync_type_buttons(self):
        self.pick_dm.style = discord.ButtonStyle.primary if self.request_type == "dm" else discord.ButtonStyle.secondary
        self.pick_friend.style = discord.ButtonStyle.primary if self.request_type == "friend" else discord.ButtonStyle.secondary

    @discord.ui.button(label="Type: DM", style=discord.ButtonStyle.primary)
    async def pick_dm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.request_type = "dm"
        self._sync_type_buttons()
        await interaction.response.edit_message(
            content=_build_picker_prompt(self.selected_user_id, self.request_type), view=self
        )

    @discord.ui.button(label="Type: Friend", style=discord.ButtonStyle.secondary)
    async def pick_friend(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.request_type = "friend"
        self._sync_type_buttons()
        await interaction.response.edit_message(
            content=_build_picker_prompt(self.selected_user_id, self.request_type), view=self
        )

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.primary)
    async def continue_to_reason(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.selected_user_id is None:
            await interaction.response.send_message("You haven't picked anyone yet!", ephemeral=True)
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This only works inside a server, not in DMs.", ephemeral=True
            )
            return
        target_user = guild.get_member(self.selected_user_id)
        if target_user is None:
            await interaction.response.send_message(
                "Hmm, couldn't find that person in this server.", ephemeral=True
            )
            return
        error_message, _ = self._precheck_fn(guild, interaction.user, target_user)
        if error_message:
            await interaction.response.send_message(error_message, ephemeral=True)
            return
        await interaction.response.send_modal(
            DmRequestReasonModal(
                target_user_id=self.selected_user_id,
                request_type=self.request_type,
                submit_fn=self._submit_fn,
            )
        )


class DmRequestPanelView(discord.ui.View):
    def __init__(self, precheck_fn=None, submit_fn=None):
        super().__init__(timeout=None)
        self._precheck_fn = precheck_fn
        self._submit_fn = submit_fn

    @discord.ui.button(
        label="Open DM Request Form",
        style=discord.ButtonStyle.primary,
        custom_id=DM_REQUEST_PANEL_VIEW_ID,
    )
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message(
                "This only works inside a server, not in DMs.", ephemeral=True
            )
            return
        picker_view = DmRequestLookupView(
            precheck_fn=self._precheck_fn,
            submit_fn=self._submit_fn,
        )
        await interaction.response.send_message(
            _build_picker_prompt(None),
            view=picker_view,
            ephemeral=True,
        )
