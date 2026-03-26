import discord
from discord.ui import View, Button
from discord.ext import tasks
import os
import json
import time

# ── Config ──────────────────────────────────────────────────────────────────
TOKEN = os.environ.get("DISCORD_TOKEN")

CATEGORIES = {
    "active":    1476810735194607647,
    "delivered": 1480672015278014514,
    "declined":  1480672102775390299,
    "contract":  1481057598026289284,
    "cold":      1480607284387250187,
}

# Categories that trigger nudge messages and what to say
NUDGES = {
    CATEGORIES["active"]:    "Did you comp this out yet?? DROP THE OFFER! 🔥",
    CATEGORIES["delivered"]: "Did we lock this up?? 🔒",
}

DISPO_ROLE_ID = 1477051979317510246
DISPO_PERMISSIONS = discord.PermissionOverwrite(
    view_channel=True,
    send_messages=True,
    embed_links=True,
    attach_files=True,
    add_reactions=True,
    read_message_history=True,
)


STATE_FILE = "channel_state.json"

# ── State persistence ────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

# channel_state = { "channel_id": { "category_id": int, "since": float, "last_nudge": float } }
channel_state = load_state()

# ── Bot setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
client = discord.Client(intents=intents)

# ── Status panel view (buttons) ──────────────────────────────────────────────
class ReminderSettingsView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔔 Reminders On", style=discord.ButtonStyle.success, custom_id="reminders_on")
    async def reminders_on(self, interaction: discord.Interaction, button: Button):
        cid = str(interaction.channel.id)
        if cid not in channel_state:
            channel_state[cid] = {"category_id": interaction.channel.category_id, "since": time.time(), "last_nudge": 0}
        channel_state[cid]["reminders_off"] = False
        save_state(channel_state)
        await interaction.response.send_message("🔔 Reminders turned **on** for this channel.", ephemeral=True)

    @discord.ui.button(label="🔕 Reminders Off", style=discord.ButtonStyle.secondary, custom_id="reminders_off")
    async def reminders_off(self, interaction: discord.Interaction, button: Button):
        cid = str(interaction.channel.id)
        if cid not in channel_state:
            channel_state[cid] = {"category_id": interaction.channel.category_id, "since": time.time(), "last_nudge": 0}
        channel_state[cid]["reminders_off"] = True
        save_state(channel_state)
        await interaction.response.send_message("🔕 Reminders turned **off** for this channel.", ephemeral=True)


class LeadStatusView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📤 Offer Delivered", style=discord.ButtonStyle.primary, custom_id="move_delivered")
    async def move_delivered(self, interaction: discord.Interaction, button: Button):
        await move_channel(interaction, "delivered", "📤 Offer Delivered")

    @discord.ui.button(label="🚫 Offer Declined / Follow-Up!", style=discord.ButtonStyle.danger, custom_id="move_declined")
    async def move_declined(self, interaction: discord.Interaction, button: Button):
        await move_channel(interaction, "declined", "🚫 Offer Declined / Follow-Up!")

    @discord.ui.button(label="📝 Under Contract", style=discord.ButtonStyle.success, custom_id="move_contract")
    async def move_contract(self, interaction: discord.Interaction, button: Button):
        await move_channel(interaction, "contract", "📝 Under Contract")

    @discord.ui.button(label="🥶 Not Interested", style=discord.ButtonStyle.secondary, custom_id="move_cold")
    async def move_cold(self, interaction: discord.Interaction, button: Button):
        await move_channel(interaction, "cold", "🥶 Not Interested")


async def move_channel(interaction: discord.Interaction, category_key: str, label: str):
    guild    = interaction.guild
    channel  = interaction.channel
    cat_id   = CATEGORIES[category_key]
    category = guild.get_channel(cat_id)

    if category is None:
        await interaction.response.send_message("❗ Category not found.", ephemeral=True)
        return

    await channel.edit(category=category)
    await interaction.response.send_message(f"✅ Moved to **{label}** by {interaction.user.mention}")

    # If moved to Under Contract, grant Dispo role access
    if cat_id == CATEGORIES["contract"]:
        dispo_role = interaction.guild.get_role(DISPO_ROLE_ID)
        if dispo_role:
            await channel.set_permissions(dispo_role, overwrite=DISPO_PERMISSIONS)

    # Update state — reset timer when channel is moved
    channel_state[str(channel.id)] = {
        "category_id": cat_id,
        "since": time.time(),
        "last_nudge": 0
    }
    save_state(channel_state)


# ── 24-hour nudge task ───────────────────────────────────────────────────────
@tasks.loop(hours=1)
async def nudge_check():
    now = time.time()
    interval = NUDGE_INTERVAL_HOURS * 3600

    for channel_id, data in list(channel_state.items()):
        cat_id     = data.get("category_id")
        since      = data.get("since", now)
        last_nudge = data.get("last_nudge", 0)

        if cat_id not in NUDGES:
            continue

        if data.get("reminders_off"):
            continue

        # Fire if 24h has passed since move or last nudge
        trigger_time = max(since, last_nudge) if last_nudge else since
        if now - trigger_time >= interval:
            channel = client.get_channel(int(channel_id))
            if channel:
                await channel.send(f"⏰ {NUDGES[cat_id]}")
                channel_state[channel_id]["last_nudge"] = now
                save_state(channel_state)


# ── Auto-panel on new channel ────────────────────────────────────────────────
@client.event
async def on_guild_channel_create(channel):
    if not isinstance(channel, discord.TextChannel):
        return
    if channel.category_id not in CATEGORIES.values():
        return

    # Track the new channel
    channel_state[str(channel.id)] = {
        "category_id": channel.category_id,
        "since": time.time(),
        "last_nudge": 0
    }
    save_state(channel_state)

    embed = discord.Embed(
        title="Lead Status",
        description="Move this lead to a different stage:",
        color=0x5865F2
    )
    await channel.send(embed=embed, view=LeadStatusView())

    reminder_embed = discord.Embed(
        title="Reminder Settings",
        description="Toggle 24-hour nudges for this channel:",
        color=0x2b2d31
    )
    await channel.send(embed=reminder_embed, view=ReminderSettingsView())


# ── Commands ─────────────────────────────────────────────────────────────────
@client.event
async def on_ready():
    client.add_view(LeadStatusView())
    client.add_view(ReminderSettingsView())
    nudge_check.start()
    print(f"✅ Logged in as {client.user}")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if message.content.strip().lower() == "!panel":
        embed = discord.Embed(
            title="Lead Status",
            description="Move this lead to a different stage:",
            color=0x5865F2
        )
        await message.channel.send(embed=embed, view=LeadStatusView())
        await message.delete()

    if message.content.strip().lower() == "!reminders":
        embed = discord.Embed(
            title="Reminder Settings",
            description="Toggle 24-hour nudges for this channel:",
            color=0x2b2d31
        )
        await message.channel.send(embed=embed, view=ReminderSettingsView())
        await message.delete()


client.run(TOKEN)
