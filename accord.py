import os
import datetime
import discord

from discord import app_commands
from dotenv import load_dotenv
import json

from dm_logic import DM_ROLE_NAMES, ROLE_DM_ASK, ROLE_DM_CLOSED, ROLE_DM_OPEN, resolve_mode
import logging

# ==============================
# Configuration
# ==============================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")

BYPASS_ROLE_IDS = set()
CONSENT_FILE = "consent_data.json"
DM_REQUESTS_FILE = "dm_requests.json"

DEBUG = True  # Set to False when going global
REQUEST_CHANNELS = {}  # {guild_id: channel_id}
REQUEST_CHANNEL_FILE = "request_channels.json"

log = logging.getLogger("accord")  # your bot namespace


# ==============================
# Intents
# ==============================
intents = discord.Intents.default()
intents.members = True

# Interaction Consent State
INTERACTION_PAIRS = {}        # {channel_id: set(("userA","userB"))}
DM_REQUESTS = {}  # {guild_id: {(user1_id, user2_id): message_id}}

AUDIT_LOG_CHANNEL_ID = None
AUDIT_FILE = "dm_audit_log.json"


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
            log.info("Synced commands to development guild.")
        else:
            await self.tree.sync()
            log.info("Synced commands globally.")

bot = Bot()

# ==============================
# Events
# ==============================
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    load_consent()
    load_request_channels()

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
async def on_disconnect():
    save_consent()

# ==============================
# Logic
# ==============================
async def safe_dm_user(user: discord.User | discord.Member, embed: discord.Embed):
    try:
        await user.send(embed=embed)
    except discord.Forbidden:
        # User has DMs closed or blocked the bot
        pass
    except discord.HTTPException:
        pass


def load_audit_log():
    try:
        with open(AUDIT_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def is_mutual(guild_id: int, user1: int, user2: int) -> bool:
    pairs = INTERACTION_PAIRS.get(guild_id, set())
    return (
        (user1, user2) in pairs and
        (user2, user1) in pairs
    )

def add_mutual_pair(pair_set: set, a: int, b: int):
    pair_set.add((a, b))
    pair_set.add((b, a))

def load_dm_requests():
    global DM_REQUESTS
    try:
        with open(DM_REQUESTS_FILE, "r") as f:
            raw = json.load(f)
            DM_REQUESTS = {
                int(g): {
                    tuple(map(int, k.split("-"))): v
                    for k, v in pairs.items()
                }
                for g, pairs in raw.items()
            }
    except FileNotFoundError:
        DM_REQUESTS = {}

def save_dm_requests():
    output = {}
    for guild_id, pairs in DM_REQUESTS.items():
        output[str(guild_id)] = {
            f"{a}-{b}": msg_id
            for (a, b), msg_id in pairs.items()
        }

    with open(DM_REQUESTS_FILE, "w") as f:
        json.dump(output, f, indent=4)


async def log_audit_event(guild: discord.Guild, message: str):
    global AUDIT_LOG_CHANNEL_ID

    timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    log_entry = {
        "timestamp": timestamp,
        "guild_id": guild.id,
        "message": message
    }

    # Append to JSON file
    try:
        with open(AUDIT_FILE, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = []

    data.append(log_entry)

    with open(AUDIT_FILE, "w") as f:
        json.dump(data, f, indent=4)

    log.info(log_entry)

    # Send to audit channel if configured
    if AUDIT_LOG_CHANNEL_ID:
        channel = guild.get_channel(AUDIT_LOG_CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                title="📜 DM Permission Audit",
                description=message,
                color=discord.Color.blurple()
            )
            embed.set_footer(text=timestamp)
            await channel.send(embed=embed)


def load_request_channels():
    global REQUEST_CHANNELS
    try:
        with open(REQUEST_CHANNEL_FILE, "r") as f:
            raw = json.load(f)
            REQUEST_CHANNELS = {int(k): v for k, v in raw.items()}
    except FileNotFoundError:
        REQUEST_CHANNELS = {}

def save_request_channels():
    with open(REQUEST_CHANNEL_FILE, "w") as f:
        json.dump(REQUEST_CHANNELS, f, indent=4)


def load_consent():
    global INTERACTION_PAIRS
    INTERACTION_PAIRS = {}

    try:
        with open(CONSENT_FILE, "r") as f:
            raw = json.load(f)

        for guild_id_str, pairs in raw.items():
            guild_id = int(guild_id_str)
            INTERACTION_PAIRS[guild_id] = set()

            for a, b in pairs:
                if a == b:
                    continue
                INTERACTION_PAIRS[guild_id].add((a, b))
                INTERACTION_PAIRS[guild_id].add((b, a))

    except FileNotFoundError:
        INTERACTION_PAIRS = {}

    if DEBUG:
        log.info("=== CONSENT STATE AFTER LOAD ===")
        log.info("Loaded pairs:", INTERACTION_PAIRS)


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
    def __init__(self, requester_id: int, target_id: int, guild_id: int):
        super().__init__(timeout=86400)
        self.requester_id = requester_id
        self.target_id = target_id
        self.guild_id = guild_id
        self.message = None

    async def on_timeout(self):
        if self.message:
            for child in self.children:
                child.disabled = True

            timeout_embed = discord.Embed(
                title="⌛ DM Request Expired",
                description="This DM request expired after 24 hours.",
                color=discord.Color.orange()
            )

            await self.message.edit(embed=timeout_embed, view=self)

    # ✅ BUTTONS MUST LIVE INSIDE CLASS

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):

        if interaction.user.id != self.target_id:
            await interaction.response.send_message(
                "You are not the target of this request.",
                ephemeral=True
            )
            return

        guild = interaction.guild
        requester = guild.get_member(self.requester_id)
        target = guild.get_member(self.target_id)

        if not requester or not target:
            await interaction.response.send_message(
                "Could not resolve users.",
                ephemeral=True
            )
            return

        INTERACTION_PAIRS.setdefault(self.guild_id, set())
        pair_set = INTERACTION_PAIRS[self.guild_id]

        pair_set.add((self.requester_id, self.target_id))
        pair_set.add((self.target_id, self.requester_id))
        save_consent()

        for child in self.children:
            child.disabled = True

        success_embed = discord.Embed(
            title="✅ DM Permission Granted",
            description=(
                f"**{requester.display_name}** ↔ **{target.display_name}**\n\n"
                "Both users may now DM each other.\n"
                "Permission can be revoked with `/dm_revoke`."
            ),
            color=discord.Color.green()
        )

        await safe_dm_user(requester, success_embed)
        await safe_dm_user(target, success_embed)

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
            title="❌ DM Request Denied",
            description="The request was declined.",
            color=discord.Color.red()
        )

        await interaction.response.edit_message(
            embed=deny_embed,
            view=self
        )




# ==============================
# Slash Commands
# ==============================
@bot.tree.command(
    name="dm_help",
    description="Learn how DM request permissions work",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
async def dm_help(interaction: discord.Interaction):

    guild = interaction.guild

    embed = discord.Embed(
        title="📬 DM Relationship System",
        description="Control how users may request DM access with you.",
        color=discord.Color.gold()
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
        inline=False
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
        inline=False
    )

    embed.add_field(
        name="Your Commands",
        value=(
            "`/dm_info` — View your full DM status\n"
            "`/dm_set_mode` — Set your DM preference\n"
            "`/dm_ask @user` — Send DM request\n"
            "`/dm_revoke @user` — Revoke relationship\n"
            "`/dm_status @user` — Check relationship status\n"
        ),
        inline=False
    )

    embed.add_field(
        name="Moderator Tools",
        value=(
            "`/dm_permissions_set` — Manually create relationship\n"
            "`/dm_permissions_remove` — Remove relationship\n"
            "`/dm_permissions_list` — View all stored relationships"
        ),
        inline=False
    )

    embed.set_footer(
        text="DM relationships are logged for audit transparency."
    )

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
        text="Use /dm_ask or /dm_revoke to manage permissions."
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
    dm_roles = [r for r in member.roles if r.name in DM_ROLE_NAMES]

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
    description="Revoke DM consent with another user",
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

    request_channel_id = REQUEST_CHANNELS.get(guild_id)
    if request_channel_id:
        channel = interaction.guild.get_channel(request_channel_id)
        if channel:
            message_id = DM_REQUESTS.get(guild_id, {}).get((interaction.user.id, user.id))
            if message_id:
                try:
                    msg = await channel.fetch_message(message_id)

                    notify_embed = discord.Embed(
                        title="🚫 DM Permission Revoked",
                        description=(
                            f"**{interaction.user.display_name}** ↔ **{user.display_name}**\n\n"
                            "You may no longer DM each other."
                        ),
                        color=discord.Color.red()
                    )

                    await safe_dm_user(interaction.user.id, notify_embed)
                    await safe_dm_user(user2, notify_embed)

                    await msg.edit(embed=revoked_embed, view=None)

                except discord.NotFound:
                    pass

    await log_audit_event(
        interaction.guild,
        f"DM permission revoked: {interaction.user.id} ↔ {user.display_name} (by {interaction.user.display_name})"
    )

    if removed:
        save_consent()
        await interaction.response.send_message(
            f"DM consent revoked with {user.mention}."
        )
    else:
        await interaction.response.send_message(
            "No mutual consent existed.",
            ephemeral=True
        )


@bot.tree.command(
    name="dm_status",
    description="Check DM consent status with a user",
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
        f"**DM permission Status**\n\n"
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
    guild = interaction.guild
    guild_id = guild.id
    requester = interaction.user

    log.info(f"dm_ask triggered {discord.Member}\n")

    # ❌ Self check
    if user.id == requester.id and not DEBUG:
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
    if mode == "open" and not DEBUG:
        await interaction.response.send_message(
            f"{user.display_name} has DMs set to OPEN. No request required.",
            ephemeral=True
        )
        return

    # ❌ Existing relationship
    pair_set = INTERACTION_PAIRS.get(guild_id, set())

    if is_mutual(guild_id, requester.id, user.id):
        await interaction.response.send_message(
            "A permission relationship already exists.",
            ephemeral=True
        )
        return

    # -------------------------------
    # Determine Request Channel
    # -------------------------------
    request_channel_id = REQUEST_CHANNELS.get(guild_id)

    if not request_channel_id:
        await interaction.response.send_message(
            "No DM request channel has been configured. Use `/dm_request_channel_set` first.",
            ephemeral=True
        )
        return

    request_channel = guild.get_channel(request_channel_id)

    if not request_channel:
        await interaction.response.send_message(
            "Configured DM request channel is invalid.",
            ephemeral=True
        )
        return

    # -------------------------------
    # Create Embed
    # -------------------------------
    embed = discord.Embed(
        title="📨 DM Request",
        description=(
            f"{user.mention}\n\n"
            f"**{interaction.user.display_name}** would like permission to DM you!\n\n"
            f"This request will time out in 24 hours"
        ),
        color=discord.Color.gold()
    )

    # Small avatar in top bar only
    embed.set_author(
        name=interaction.user.display_name,
        icon_url=interaction.user.display_avatar.url
    )

    embed.set_footer(text="Permission can be revoked at any time with /dm_revoke")



    # -------------------------------
    # Create View
    # -------------------------------
    view = AskConsentView(
        requester_id=requester.id,
        target_id=user.id,
        guild_id=guild_id
    )

    # -------------------------------
    # Send to Request Channel
    # -------------------------------
    try:
        message = await request_channel.send(
            content=user.mention,
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions(users=[user])
        )

        await message.edit(content=None)

        view.message = message

        DM_REQUESTS.setdefault(guild_id, {})
        DM_REQUESTS[guild_id][(requester.id, user.id)] = message.id
        save_dm_requests()

    except discord.Forbidden:
        await interaction.response.send_message(
            "I do not have permission to send messages in the configured DM request channel.",
            ephemeral=True
        )
        return

    await log_audit_event(
        interaction.guild,
        f"DM request asked: {interaction.user.display_name} ➝ {user.display_name}"
    )

    await interaction.response.send_message(
        f"📨 DM request sent to {request_channel.mention}.",
        ephemeral=True
    )



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
    description="List all stored DM permission permissions",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
async def debug_permissions_list(interaction: discord.Interaction):

    guild_id = interaction.guild.id
    guild = interaction.guild

    pairs = INTERACTION_PAIRS.get(guild_id, set())

    if not pairs:
        await interaction.response.send_message(
            "No stored DM permission permissions exist.",
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
        f"**Stored DM permission Permissions**\n\n{output}",
        ephemeral=True
    )

@bot.tree.command(
    name="debug_permissions_set",
    description="Manually set DM permission permission between two users",
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

    await log_audit_event(
        interaction.guild,
        f"Manual DM permission set: {user1.display_name} ↔ {user2.display_name} (by {interaction.user.display_name})"
    )

    await interaction.response.send_message(
        f"✅ DM permission permission established between "
        f"{user1.mention} and {user2.mention}."
    )

@bot.tree.command(
    name="debug_permissions_remove",
    description="Remove DM permission permission between two users",
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
            f"🗑️ DM permission permission removed between "
            f"{user1.mention} and {user2.mention}."
        )
    else:
        await interaction.response.send_message(
            "No mutual permission existed between those users.",
            ephemeral=True
        )
    
    await log_audit_event(
        interaction.guild,
        f"DM permission removed: {user1.display_name} ↔ {user2.display_name} (by {interaction.user.display_name})"
    )

@bot.tree.command(
    name="dm_set_audit_channel",
    description="Set channel for DM permission audit logs",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
@app_commands.describe(channel="Channel to send audit logs to")
async def dm_set_audit_channel(interaction: discord.Interaction, channel: discord.TextChannel):

    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You do not have permission to configure audit logging.",
            ephemeral=True
        )
        return

    global AUDIT_LOG_CHANNEL_ID
    AUDIT_LOG_CHANNEL_ID = channel.id

    await interaction.response.send_message(
        f"📜 Audit logs will now be sent to {channel.mention}."
    )

@bot.tree.command(
    name="dm_audit_user",
    description="View DM permission audit history for a user",
    guild=discord.Object(id=GUILD_ID) if DEBUG else None
)
@app_commands.describe(
    user="User to inspect",
    limit="Number of recent entries to show (default 10)"
)
async def dm_audit_user(
    interaction: discord.Interaction,
    user: discord.Member,
    limit: app_commands.Range[int, 1, 50] = 10
):

    # Admin only
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You do not have permission to view audit logs.",
            ephemeral=True
        )
        return

    data = load_audit_log()

    if not data:
        await interaction.response.send_message(
            "No audit log entries found.",
            ephemeral=True
        )
        return

    # Filter by guild and user ID appearing in message
    guild_id = interaction.guild.id
    filtered = [
        entry for entry in data
        if entry["guild_id"] == guild_id and str(user.id) in entry["message"]
    ]

    if not filtered:
        await interaction.response.send_message(
            f"No audit entries found for {user.display_name}.",
            ephemeral=True
        )
        return

    # Get most recent entries
    filtered = filtered[-limit:]

    lines = []
    for entry in reversed(filtered):
        lines.append(f"**{entry['timestamp']}**\n{entry['message']}\n")

    output = "\n".join(lines)

    if len(output) > 3500:
        output = output[:3500] + "\n... (truncated)"

    embed = discord.Embed(
        title=f"📜 Audit History — {user.display_name}",
        description=output,
        color=discord.Color.blurple()
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ==============================
# Run Bot
# ==============================

if __name__ == "__main__":
    bot.run(TOKEN)
