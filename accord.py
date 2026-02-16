import os
import datetime
import discord
from discord import app_commands
from dotenv import load_dotenv
import json

# ==============================
# Configuration
# ==============================

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

GUILD_ID = 1469491362444480666
SPOILER_REQUIRED_CHANNELS = {1469604360756396167}
BYPASS_ROLE_IDS = set()
CONSENT_FILE = "consent_data.json"

DEBUG = True  # Set to False when going global
REQUEST_CHANNELS = {}  # {guild_id: channel_id}
REQUEST_CHANNEL_FILE = "request_channels.json"


# ==============================
# Intents
# ==============================
intents = discord.Intents.default()
intents.members = True
intents.message_content = True  # Required for spoiler enforcement

# Interaction Consent State
INTERACTION_PAIRS = {}        # {channel_id: set(("userA","userB"))}

# Static DM Mode Roles
ROLE_DM_OPEN = "DMs: Open"
ROLE_DM_ASK = "DMs: Ask"
ROLE_DM_CLOSED = "DMs: Closed"

DM_ROLE_NAMES = {
    ROLE_DM_OPEN,
    ROLE_DM_ASK,
    ROLE_DM_CLOSED
}

# ==============================
# Bot Class
# ==============================
class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if DEBUG:
            guild = discord.Object(id=GUILD_ID)
            if not hasattr(self, "synced"):
                await self.tree.sync(guild=guild)
                self.synced = True
            print("Synced commands to development guild.")
        else:
            await self.tree.sync()
            print("Synced commands globally.")

bot = Bot()

# ==============================
# Events
# ==============================
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    load_consent()
    load_request_channels()
    print("------")

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):

    before_dm = [r for r in before.roles if r.name in DM_ROLE_NAMES]
    after_dm = [r for r in after.roles if r.name in DM_ROLE_NAMES]

    # If more than one DM role exists after update
    if len(after_dm) > 1:

        # Keep the highest role in hierarchy
        keep = max(after_dm, key=lambda r: r.position)

        remove = [r for r in after_dm if r != keep]

        try:
            await after.remove_roles(*remove)
        except discord.Forbidden:
            pass

@bot.event
async def on_message(message: discord.Message):

    if DEBUG:
        print("Message detected:", message.content, message.attachments)

    if message.author.bot:
        return

    # ==============================
    # Spoiler Enforcement
    # ==============================
    if message.channel.id in SPOILER_REQUIRED_CHANNELS:

        if any(role.id in BYPASS_ROLE_IDS for role in message.author.roles):
            pass

        elif message.attachments:
            for attachment in message.attachments:
                filename = attachment.filename.lower()

                if filename.endswith((".png", ".jpg", ".jpeg")):
                    if not attachment.is_spoiler():
                        try:
                            await message.delete()
                            await message.channel.send(
                                f"{message.author.mention} — 🚨 NSFW images must be marked as spoiler.",
                                delete_after=20
                            )
                        except discord.Forbidden:
                            pass
                        return

    # ==============================
    # Mutual Mention Enforcement
    # ==============================
    # if not message.mentions:
    #     return

    # guild_id = message.guild.id
    # pair_set = INTERACTION_PAIRS.get(guild_id, set())

    # for mentioned_user in message.mentions:

    #     if mentioned_user.bot:
    #         continue

    #     mode = resolve_mode(mentioned_user)

    #     if mode == "open":
    #         continue

    #     if mode == "closed":
    #         await message.delete()
    #         await message.channel.send(
    #             f"{message.author.mention} — {mentioned_user.display_name} does not allow mentions.",
    #             delete_after=10
    #         )
    #         return

    #     if mode == "ask":
    #         if (message.author.id, mentioned_user.id) not in pair_set:
    #             await message.delete()
    #             await message.channel.send(
    #                 f"{message.author.mention} — DM request required before mentioning {mentioned_user.display_name}.",
    #                 delete_after=10
    #             )
    #             return


# ==============================
# Logic
# ==============================
def resolve_mode(member: discord.Member):

    role_names = {role.name for role in member.roles}

    if ROLE_DM_CLOSED in role_names:
        return "closed"

    if ROLE_DM_ASK in role_names:
        return "ask"

    return "open"

def load_consent():
    global INTERACTION_PAIRS
    try:
        with open(CONSENT_FILE, "r") as f:
            raw = json.load(f)

        for guild_id, pairs in raw.items():
            INTERACTION_PAIRS[int(guild_id)] = set()
            for pair in pairs:
                a, b = pair
                if a != b:
                    INTERACTION_PAIRS[int(guild_id)].add((a, b))
                    INTERACTION_PAIRS[int(guild_id)].add((b, a))
    except FileNotFoundError:
        INTERACTION_PAIRS = {}

def load_request_channels():
    global REQUEST_CHANNELS
    try:
        with open(REQUEST_CHANNEL_FILE, "r") as f:
            raw = json.load(f)
            REQUEST_CHANNELS = {int(k): int(v) for k, v in raw.items()}
    except FileNotFoundError:
        REQUEST_CHANNELS = {}


def save_request_channels():
    with open(REQUEST_CHANNEL_FILE, "w") as f:
        json.dump(REQUEST_CHANNELS, f, indent=4)


def save_consent():
    output = {}

    for guild_id, pairs in INTERACTION_PAIRS.items():
        unique_pairs = set()

        for a, b in pairs:
            if (b, a) not in unique_pairs:
                unique_pairs.add((a, b))

        output[str(guild_id)] = [
            [a, b] for a, b in unique_pairs
        ]

    with open(CONSENT_FILE, "w") as f:
        json.dump(output, f, indent=4)


class AskConsentView(discord.ui.View):
    def __init__(self, requester_id: int, target_id: int):
        super().__init__(timeout=120)
        self.requester_id = requester_id
        self.target_id = target_id

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):

        if interaction.user.id != self.target_id:
            await interaction.response.send_message(
                "You are not the target of this request.",
                ephemeral=True
            )
            return

        guild_id = interaction.guild.id

        if guild_id not in INTERACTION_PAIRS:
            INTERACTION_PAIRS[guild_id] = set()

        INTERACTION_PAIRS[guild_id].add((self.requester_id, self.target_id))
        INTERACTION_PAIRS[guild_id].add((self.target_id, self.requester_id))

        save_consent()

        for child in self.children:
            child.disabled = True

        success_embed = discord.Embed(
            title="✅ DM Request Approved",
            description="Both users may now mention each other.",
            color=discord.Color.green()
        )

        await interaction.response.edit_message(
            embed=success_embed,
            view=self
        )


    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):

        if interaction.user.id != self.target_id:
            await interaction.response.send_message(
                "You are not the target of this request.",
                ephemeral=True
            )
            return

        for child in self.children:
            child.disabled = True

        deny_embed = discord.Embed(
            title="❌ Consent Denied",
            description="No permission was granted.",
            color=discord.Color.red()
        )

        await interaction.response.edit_message(
            embed=deny_embed,
            view=self
        )


# ==============================
# Slash Commands
# ==============================
# @bot.tree.command(
#     name="consent_check_enable",
#     description="Enable mutual mention consent in a channel",
#     guild=discord.Object(id=GUILD_ID) if DEBUG else None
# )
# @app_commands.describe(channel="Channel to enforce mention consent in")
# async def consent_check_enable(interaction: discord.Interaction, channel: discord.TextChannel):

#     if not interaction.user.guild_permissions.manage_channels:
#         await interaction.response.send_message("No permission.", ephemeral=True)
#         return

#     await interaction.response.send_message(
#         f"Mutual mention consent enabled in {channel.mention}"
#     )

@bot.tree.command(
    name="dm_help",
    description="Learn how DM request permissions work",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
async def dm_help(interaction: discord.Interaction):

    guild = interaction.guild

    embed = discord.Embed(
        title="📬 DM Request System",
        description="Control how users may request DM interaction with you.",
        color=discord.Color.gold()
    )

    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    embed.add_field(
        name="Your DM Modes",
        value=(
            "**OPEN** — Anyone may DM you.\n"
            "**ASK** — Requires mutual approval.\n"
            "**CLOSED** — No one may DM you."
        ),
        inline=False
    )

    embed.add_field(
        name="How Mutual Requests Work",
        value=(
            "• Use `/dm_ask @user` to request permission.\n"
            "• The other user can Accept or Deny.\n"
            "• Permissions can be revoked anytime."
        ),
        inline=False
    )

    embed.add_field(
        name="Available Commands",
        value=(
            "`/dm_info` — Show your perms and status \n"
            "`/dm_set_mode` — Set your DM preference\n"
            "`/dm_ask @user` — Request DM access\n"
            "`/dm_revoke @user` — Revoke permission\n"
            "`/dm_status @user` — Check status\n"
        ),
        inline=False
    )

    embed.set_footer(text="This system protects consent and clarity.")

    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(
    name="dm_info",
    description="View your DM mode and all stored DM permissions",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
async def dm_info(interaction: discord.Interaction):

    guild = interaction.guild
    member = interaction.user
    guild_id = guild.id

    # ==============================
    # Resolve Current Mode
    # ==============================
    role_names = {role.name for role in member.roles}

    if ROLE_DM_CLOSED in role_names:
        mode = "CLOSED"
        mode_desc = "No one may DM you."
    elif ROLE_DM_ASK in role_names:
        mode = "ASK"
        mode_desc = "DM requests require mutual approval."
    else:
        mode = "OPEN"
        mode_desc = "Anyone may DM you."

    # ==============================
    # Gather Permission States
    # ==============================
    pair_set = INTERACTION_PAIRS.get(guild_id, set())

    mutual = set()
    outgoing = set()
    incoming = set()

    for a, b in pair_set:
        if a == member.id:
            if (b, a) in pair_set:
                mutual.add(b)
            else:
                outgoing.add(b)

        elif b == member.id:
            if (a, b) not in pair_set:
                incoming.add(a)

    # Convert IDs to names
    def resolve_names(id_set):
        names = []
        for user_id in id_set:
            user = guild.get_member(user_id)
            if user:
                names.append(user.display_name)
        return sorted(names)

    mutual_names = resolve_names(mutual)
    outgoing_names = resolve_names(outgoing)
    incoming_names = resolve_names(incoming)

    # ==============================
    # Build Embed
    # ==============================
    embed = discord.Embed(
        title="📬 Your DM Information",
        color=discord.Color.gold()
    )

    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    embed.add_field(
        name="Current Mode",
        value=f"**{mode}**\n{mode_desc}",
        inline=False
    )

    # Mutual
    if mutual_names:
        embed.add_field(
            name=f"✅ Mutual Permissions ({len(mutual_names)})",
            value="\n".join(f"• {n}" for n in mutual_names),
            inline=False
        )

    # Outgoing (you allowed them)
    if outgoing_names:
        embed.add_field(
            name=f"➡️ You Allowed ({len(outgoing_names)})",
            value="\n".join(f"• {n}" for n in outgoing_names),
            inline=False
        )

    # Incoming (they allowed you)
    if incoming_names:
        embed.add_field(
            name=f"⬅️ They Allowed You ({len(incoming_names)})",
            value="\n".join(f"• {n}" for n in incoming_names),
            inline=False
        )

    if not (mutual_names or outgoing_names or incoming_names):
        embed.add_field(
            name="No Stored Permissions",
            value="You currently have no DM permissions recorded.",
            inline=False
        )

    embed.set_footer(
        text="Use /dm_ask, /dm_permissions_set, or /dm_revoke to manage permissions."
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(
    name="dm_set_mode",
    description="Set your DM request preference",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
@app_commands.describe(mode="open, ask, or closed")
@app_commands.choices(
    mode=[
        app_commands.Choice(name="open", value="open"),
        app_commands.Choice(name="ask", value="ask"),
        app_commands.Choice(name="closed", value="closed")
    ]
)
async def dm_set_mode(interaction: discord.Interaction, mode: app_commands.Choice[str]):

    guild = interaction.guild
    member = interaction.user

    async def get_or_create(role_name):
        role = discord.utils.get(guild.roles, name=role_name)
        if role is None:
            role = await guild.create_role(
                name=role_name,
                mentionable=False,
                hoist=False,
                reason="Auto-created DM preference role"
            )
        return role

    try:
        role_open = await get_or_create(ROLE_DM_OPEN)
        role_ask = await get_or_create(ROLE_DM_ASK)
        role_closed = await get_or_create(ROLE_DM_CLOSED)
    except discord.Forbidden:
        await interaction.response.send_message(
            "I lack permission to create roles.",
            ephemeral=True
        )
        return

    # Remove ALL DM roles first
    dm_roles = [r for r in guild.roles if r.name in DM_ROLE_NAMES]

    try:
        await member.remove_roles(*dm_roles)
    except discord.Forbidden:
        await interaction.response.send_message(
            "I lack permission to manage roles.",
            ephemeral=True
        )
        return

    # Assign selected role
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
        title="DM Request Mode Updated",
        description=f"You are now set to **{status}**.",
        color=discord.Color.gold()
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)



@bot.tree.command(
    name="dm_allow",
    description="Mutually allow mentions with another user",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
@app_commands.describe(user="User to allow")
async def dm_allow(interaction: discord.Interaction, user: discord.Member):

    guild_id = interaction.guild.id

    if guild_id not in INTERACTION_PAIRS:
        INTERACTION_PAIRS[guild_id] = set()

    INTERACTION_PAIRS[guild_id].add((interaction.user.id, user.id))
    INTERACTION_PAIRS[guild_id].add((user.id, interaction.user.id))

    save_consent()

    await interaction.response.send_message(
        f"You and {user.mention} may now mention each other globally."
    )

@bot.tree.command(
    name="dm_revoke",
    description="Revoke mutual mention consent with another user",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
@app_commands.describe(user="User to revoke consent with")
async def dm_revoke(interaction: discord.Interaction, user: discord.Member):

    guild_id = interaction.guild.id

    if guild_id not in INTERACTION_PAIRS:
        await interaction.response.send_message(
            "No consent records exist.",
            ephemeral=True
        )
        return

    pair_set = INTERACTION_PAIRS[guild_id]

    removed = False

    if (interaction.user.id, user.id) in pair_set:
        pair_set.remove((interaction.user.id, user.id))
        removed = True

    if (user.id, interaction.user.id) in pair_set:
        pair_set.remove((user.id, interaction.user.id))
        removed = True

    if removed:
        save_consent()
        await interaction.response.send_message(
            f"Mutual mention consent revoked with {user.mention}."
        )
    else:
        await interaction.response.send_message(
            "No mutual consent existed.",
            ephemeral=True
        )


@bot.tree.command(
    name="dm_status",
    description="Check mutual mention consent status with a user",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
@app_commands.describe(user="User to check status with")
async def dm_status(interaction: discord.Interaction, user: discord.Member):

    guild_id = interaction.guild.id
    author_id = interaction.user.id
    target_id = user.id

    allowed_pairs = INTERACTION_PAIRS.get(guild_id, set())

    mutual = (
        (author_id, target_id) in allowed_pairs and
        (target_id, author_id) in allowed_pairs
    )

    if mutual:
        result = "✅ Mutual consent active."
    else:
        result = "❌ No mutual consent."

    await interaction.response.send_message(
        f"**Mutual Mention Status**\n\n"
        f"You ↔ {user.display_name}\n\n"
        f"{result}",
        ephemeral=True
    )

@bot.tree.command(
    name="dm_ask",
    description="Request DM permission with a user",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
@app_commands.describe(user="User to request permission from")
async def dm_ask(interaction: discord.Interaction, user: discord.Member):

    guild_id = interaction.guild.id  # ✅ DEFINE FIRST

    # ❌ Self check
    if user.id == interaction.user.id:
        await interaction.response.send_message(
            "You cannot request permission with yourself.",
            ephemeral=True
        )
        return

    # ❌ Bot check
    if user.bot:
        await interaction.response.send_message(
            "You cannot request permission from bots.",
            ephemeral=True
        )
        return

    # ❌ Respect CLOSED mode
    mode = resolve_mode(user)

    if mode == "closed":
        await interaction.response.send_message(
            f"{user.display_name} has DMs set to CLOSED and is not accepting requests.",
            ephemeral=True
        )
        return

    # ✅ OPEN shortcut
    if mode == "open":
        await interaction.response.send_message(
            f"{user.display_name} has DMs set to OPEN. No request required.",
            ephemeral=True
        )
        return

    # ❌ Existing relationship
    pair_set = INTERACTION_PAIRS.get(guild_id, set())

    if (interaction.user.id, user.id) in pair_set:
        await interaction.response.send_message(
            "A permission relationship already exists.",
            ephemeral=True
        )
        return

    # ---- SEND EMBED REQUEST BELOW ----


@bot.tree.command(
    name="dm_request_channel_set",
    description="Set the channel where DM requests will be posted",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
@app_commands.describe(channel="Channel to send DM requests to")
async def dm_request_channel_set(
    interaction: discord.Interaction,
    channel: discord.TextChannel
):

    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message(
            "You do not have permission to configure this.",
            ephemeral=True
        )
        return

    REQUEST_CHANNELS[interaction.guild.id] = channel.id
    save_request_channels()

    await interaction.response.send_message(
        f"✅ DM requests will now be sent to {channel.mention}."
    )


@bot.tree.command(
    name="debug_status_check",
    description="Check your current DM interaction status",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
async def debug_status_check(interaction: discord.Interaction):

    guild = interaction.guild
    member = guild.get_member(interaction.user.id)

    role_names = {role.name for role in member.roles}

    status = "OPEN"
    explanation = "Anyone may DM you."

    if "DMs: CLOSED" in role_names:
        status = "CLOSED"
        explanation = "No one may DM you."
    elif "DMs: ASK" in role_names:
        status = "ASK"
        explanation = "Mutual consent required before mentions."
    elif "DMs: OPEN" in role_names:
        status = "OPEN"
        explanation = "Anyone may DM you."

    await interaction.response.send_message(
        f"**Your DM Preference**\n\n"
        f"Status: **{status}**\n"
        f"{explanation}",
        ephemeral=True
    )

@bot.tree.command(
    name="debug_permissions_list",
    description="List all stored mutual mention permissions",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
async def debug_permissions_list(interaction: discord.Interaction):

    guild_id = interaction.guild.id
    guild = interaction.guild

    pairs = INTERACTION_PAIRS.get(guild_id, set())

    if not pairs:
        await interaction.response.send_message(
            "No stored mutual mention permissions exist.",
            ephemeral=True
        )
        return

    # Deduplicate mirrored pairs
    unique = set()
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
        f"**Stored Mutual Mention Permissions**\n\n{output}",
        ephemeral=True
    )

@bot.tree.command(
    name="debug_permissions_set",
    description="Manually set mutual mention permission between two users",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
@app_commands.describe(
    user1="First user",
    user2="Second user"
)
async def debug_permissions_set(
    interaction: discord.Interaction,
    user1: discord.Member,
    user2: discord.Member
):

    # Mod-only safeguard
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message(
            "You do not have permission to set permissions.",
            ephemeral=True
        )
        return

    if user1.id == user2.id:
        await interaction.response.send_message(
            "Cannot create permission between the same user.",
            ephemeral=True
        )
        return

    guild_id = interaction.guild.id

    if guild_id not in INTERACTION_PAIRS:
        INTERACTION_PAIRS[guild_id] = set()

    # Add both directions
    INTERACTION_PAIRS[guild_id].add((user1.id, user2.id))
    INTERACTION_PAIRS[guild_id].add((user2.id, user1.id))

    save_consent()

    await interaction.response.send_message(
        f"✅ Mutual mention permission established between "
        f"{user1.mention} and {user2.mention}."
    )

@bot.tree.command(
    name="debug_permissions_remove",
    description="Remove mutual mention permission between two users",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
@app_commands.describe(
    user1="First user",
    user2="Second user"
)
async def debug_permissions_remove(
    interaction: discord.Interaction,
    user1: discord.Member,
    user2: discord.Member
):

    # Mod-only safeguard
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message(
            "You do not have permission to remove permissions.",
            ephemeral=True
        )
        return

    if user1.id == user2.id:
        await interaction.response.send_message(
            "Cannot remove permission between the same user.",
            ephemeral=True
        )
        return

    guild_id = interaction.guild.id

    if guild_id not in INTERACTION_PAIRS:
        await interaction.response.send_message(
            "No stored permissions exist in this server.",
            ephemeral=True
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
            f"🗑️ Mutual mention permission removed between "
            f"{user1.mention} and {user2.mention}."
        )
    else:
        await interaction.response.send_message(
            "No mutual permission existed between those users.",
            ephemeral=True
        )


@bot.tree.command(
    name="spoiler_add_channel",
    description="Require spoilers for images in a channel",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
@app_commands.describe(channel="Channel to enforce spoiler images in")
async def spoiler_add_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel
):
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message(
            "You do not have permission to configure this.",
            ephemeral=True
        )
        return

    SPOILER_REQUIRED_CHANNELS.add(channel.id)

    await interaction.response.send_message(
        f"Spoiler enforcement enabled in {channel.mention}"
    )


@bot.tree.command(
    name="spoiler_remove_channel",
    description="Stop requiring spoilers in a channel",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
@app_commands.describe(channel="Channel to remove enforcement from")
async def spoiler_remove_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel
):
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message(
            "You do not have permission to configure this.",
            ephemeral=True
        )
        return

    SPOILER_REQUIRED_CHANNELS.discard(channel.id)

    await interaction.response.send_message(
        f"Spoiler enforcement disabled in {channel.mention}"
    )

@bot.tree.command(
    name="lurker_locator",
    description="Report inactivity for a role",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
@app_commands.describe(
    role="Role to analyze",
    days="Number of days to check (default: 7)"
)
async def lurker_locator(
    interaction: discord.Interaction,
    role: discord.Role,
    days: app_commands.Range[int, 1, 60] = 7
):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message(
            "You do not have permission to use this command.",
            ephemeral=True
        )
        return

    await interaction.response.defer()

    guild = interaction.guild
    cutoff = discord.utils.utcnow() - datetime.timedelta(days=days)

    role_members = set(role.members)
    active_members = set()

    for channel in guild.text_channels:
        if not channel.permissions_for(guild.me).read_message_history:
            continue

        try:
            async for message in channel.history(after=cutoff, limit=None):
                if message.author in role_members:
                    active_members.add(message.author)

                if active_members == role_members:
                    break
        except discord.Forbidden:
            continue

    inactive_members = role_members - active_members

    total = len(role_members)
    inactive_count = len(inactive_members)
    inactive_percent = (inactive_count / total * 100) if total else 0

    summary = (
        f"**Role Activity Report — {role.name} ({days} days)**\n"
        f"Total Members: {total}\n"
        f"Inactive: {inactive_count} ({inactive_percent:.1f}%)\n"
        f"----------------------------------\n"
    )

    if inactive_members:
        member_list = "\n".join(m.display_name for m in inactive_members)
        if len(member_list) > 1800:
            member_list = member_list[:1800] + "\n... (truncated)"
        summary += "\n**Inactive Members:**\n" + member_list
    else:
        summary += "\nAll members active in this period."

    await interaction.followup.send(summary)


# ==============================
# Run Bot
# ==============================

bot.run(TOKEN)
