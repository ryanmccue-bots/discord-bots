import discord
from discord import app_commands
from discord.ui import View, Button, Modal, TextInput
from discord.ext import tasks
import os
import json
import time
import re
from datetime import datetime, date
from zoneinfo import ZoneInfo

# ── Config ──────────────────────────────────────────────────────────────────
TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = int(os.environ.get("GUILD_ID", "0"))  # set this in Railway variables

TIMEZONE = ZoneInfo("America/New_York")

def today_local():
    return datetime.now(TIMEZONE).date()

def days_until(d: date):
    return (d - today_local()).days

CATEGORIES = {
    "active":    1476810735194607647,
    "delivered": 1480672015278014514,
    "declined":  1480672102775390299,
    "contract":  1481057598026289284,
    "cold":      1480607284387250187,
}

NUDGES = {
    CATEGORIES["active"]:    "Did you comp this out yet?? DROP THE OFFER! 🔥",
    CATEGORIES["delivered"]: "Did we lock this up?? 🔒",
}

DISPO_ROLE_ID       = 1477051979317510246
CALENDAR_CHANNEL_ID = 1489343322798559242
EXEMPT_USER_ID      = 622805991242596362
NUDGE_INTERVAL_HOURS = 24
STATE_FILE    = "channel_state.json"
CALENDAR_FILE = "calendar_state.json"

DISPO_PERMISSIONS = discord.PermissionOverwrite(
    view_channel=True,
    send_messages=True,
    embed_links=True,
    attach_files=True,
    add_reactions=True,
    read_message_history=True,
)

# ── State persistence ────────────────────────────────────────────────────────
def load_json(path):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)

channel_state  = load_json(STATE_FILE)
calendar_state = load_json(CALENDAR_FILE)

# ── Bot setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

class FHBBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

client = FHBBot()

# ── Calendar helpers ─────────────────────────────────────────────────────────
def parse_date(s):
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None

def build_calendar_text(guild=None):
    contracts = calendar_state.get("contracts", {})
    if not contracts:
        return "📅 **CONTRACT DEADLINES**\n\nNo contracts yet."

    today = today_local()
    entries = []
    dead = []
    for cid, data in contracts.items():
        if guild and guild.get_channel(int(cid)) is None:
            dead.append(cid)
            continue
        closing = parse_date(data["closing"])
        if closing is None:
            continue
        entries.append((closing, cid, data))

    if dead:
        for cid in dead:
            del calendar_state["contracts"][cid]
        save_json(CALENDAR_FILE, calendar_state)

    entries.sort(key=lambda x: x[0])

    this_week, this_month, later = [], [], []
    for closing, cid, data in entries:
        diff = days_until(closing)
        if diff < 0:
            continue
        signed      = data.get("signed", "?")
        closing_str = f"{closing.month}/{closing.day}/{closing.year}"
        line = f"⏳ **{diff} days** — <#{cid}>\n   Signed {signed}  |  Closes {closing_str}"
        if diff <= 7:
            this_week.append(line)
        elif closing.month == today.month and closing.year == today.year:
            this_month.append(line)
        else:
            later.append(line)

    lines = ["📅 **CONTRACT DEADLINES**"]
    if this_week:
        lines += ["", "🚨 **CLOSING THIS WEEK**", "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬", ""]
        lines += ["\n\n".join(this_week)]
    if this_month:
        lines += ["", "🟡 **CLOSING THIS MONTH**", "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬", ""]
        lines += ["\n\n".join(this_month)]
    if later:
        lines += ["", "🟢 **CLOSING LATER**", "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬", ""]
        lines += ["\n\n".join(later)]

    return "\n".join(lines).strip()


async def update_calendar_message():
    cal_channel = client.get_channel(CALENDAR_CHANNEL_ID)
    if cal_channel is None:
        return

    guild = cal_channel.guild
    text = build_calendar_text(guild)
    msg_id = calendar_state.get("message_id")

    if msg_id:
        try:
            msg = await cal_channel.fetch_message(int(msg_id))
            await msg.edit(content=text)
            return
        except discord.NotFound:
            pass

    msg = await cal_channel.send(text)
    calendar_state["message_id"] = str(msg.id)
    save_json(CALENDAR_FILE, calendar_state)


# ── Contract date modal ──────────────────────────────────────────────────────
class ContractDatesModal(Modal, title="Enter Contract Dates"):
    signed_date  = TextInput(label="Signed Date",  placeholder="MM/DD/YYYY", max_length=10)
    closing_date = TextInput(label="Closing Date", placeholder="MM/DD/YYYY", max_length=10)

    def __init__(self, channel_name: str, channel_id: str):
        super().__init__()
        self.channel_name = channel_name
        self.channel_id   = channel_id

    async def on_submit(self, interaction: discord.Interaction):
        signed  = self.signed_date.value.strip()
        closing = self.closing_date.value.strip()

        if parse_date(signed) is None or parse_date(closing) is None:
            await interaction.response.send_message(
                "❗ Invalid date format. Please use MM/DD/YYYY.", ephemeral=True
            )
            return

        if "contracts" not in calendar_state:
            calendar_state["contracts"] = {}

        calendar_state["contracts"][self.channel_id] = {
            "name":    self.channel_name,
            "signed":  signed,
            "closing": closing,
        }
        save_json(CALENDAR_FILE, calendar_state)
        await update_calendar_message()

        await interaction.response.send_message(
            f"✅ Contract dates saved! Signed **{signed}** | Closes **{closing}**"
        )


# ── Views ────────────────────────────────────────────────────────────────────
class CalendarManagerViewPersistent(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📅 Input Contract Dates", style=discord.ButtonStyle.primary, custom_id="input_dates")
    async def input_dates(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(
            ContractDatesModal(interaction.channel.name, str(interaction.channel.id))
        )


class ReminderSettingsView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔔 Reminders On", style=discord.ButtonStyle.success, custom_id="reminders_on")
    async def reminders_on(self, interaction: discord.Interaction, button: Button):
        cid = str(interaction.channel.id)
        if cid not in channel_state:
            channel_state[cid] = {"category_id": interaction.channel.category_id, "since": time.time(), "last_nudge": 0}
        channel_state[cid]["reminders_off"] = False
        save_json(STATE_FILE, channel_state)
        await interaction.response.send_message("🔔 Reminders turned **on** for this channel.", ephemeral=True)

    @discord.ui.button(label="🔕 Reminders Off", style=discord.ButtonStyle.secondary, custom_id="reminders_off")
    async def reminders_off(self, interaction: discord.Interaction, button: Button):
        cid = str(interaction.channel.id)
        if cid not in channel_state:
            channel_state[cid] = {"category_id": interaction.channel.category_id, "since": time.time(), "last_nudge": 0}
        channel_state[cid]["reminders_off"] = True
        save_json(STATE_FILE, channel_state)
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
    guild   = interaction.guild
    channel = interaction.channel
    cat_id  = CATEGORIES[category_key]
    category = guild.get_channel(cat_id)

    if category is None:
        await interaction.response.send_message("❗ Category not found.", ephemeral=True)
        return

    await channel.edit(category=category)
    await interaction.response.send_message(f"✅ Moved to **{label}** by {interaction.user.mention}")

    if cat_id == CATEGORIES["contract"]:
        member_ids = [m.id for m in channel.members]
        if EXEMPT_USER_ID not in member_ids:
            dispo_role = guild.get_role(DISPO_ROLE_ID)
            if dispo_role:
                await channel.set_permissions(dispo_role, overwrite=DISPO_PERMISSIONS)

        cal_embed = discord.Embed(
            title="Calendar Manager",
            description="Enter the signed and closing dates for this contract:",
            color=0xF1C40F
        )
        await channel.send(embed=cal_embed, view=CalendarManagerViewPersistent())

    channel_state[str(channel.id)] = {
        "category_id": cat_id,
        "since": time.time(),
        "last_nudge": 0
    }
    save_json(STATE_FILE, channel_state)


# ── Slash commands ───────────────────────────────────────────────────────────
@client.tree.command(name="panel", description="Post the Lead Status panel in this channel")
async def slash_panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Lead Status",
        description="Move this lead to a different stage:",
        color=0x5865F2
    )
    await interaction.channel.send(embed=embed, view=LeadStatusView())
    await interaction.response.send_message("✅ Panel posted.", ephemeral=True)


@client.tree.command(name="reminders", description="Post the Reminder Settings panel in this channel")
async def slash_reminders(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Reminder Settings",
        description="Toggle 24-hour nudges for this channel:",
        color=0x2b2d31
    )
    await interaction.channel.send(embed=embed, view=ReminderSettingsView())
    await interaction.response.send_message("✅ Reminder panel posted.", ephemeral=True)


@client.tree.command(name="calendar", description="Post the Calendar Manager panel in this channel")
async def slash_calendar(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Calendar Manager",
        description="Enter the signed and closing dates for this contract:",
        color=0xF1C40F
    )
    await interaction.channel.send(embed=embed, view=CalendarManagerViewPersistent())
    await interaction.response.send_message("✅ Calendar panel posted.", ephemeral=True)


@client.tree.command(name="cleancalendar", description="Remove closed/deleted channels from the calendar")
async def slash_cleancalendar(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    contracts = calendar_state.get("contracts", {})
    removed = []
    for cid in list(contracts.keys()):
        ch = interaction.guild.get_channel(int(cid))
        if ch is None:
            removed.append(cid)
            del contracts[cid]
    calendar_state["contracts"] = contracts
    save_json(CALENDAR_FILE, calendar_state)
    await update_calendar_message()
    if removed:
        await interaction.followup.send(f"🧹 Removed **{len(removed)}** dead entries and refreshed the calendar.", ephemeral=True)
    else:
        await interaction.followup.send("✅ No dead entries found.", ephemeral=True)


@client.tree.command(name="recovercalendar", description="Scan Under Contract channels and rebuild calendar from history")
async def slash_recovercalendar(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    contract_category = guild.get_channel(CATEGORIES["contract"])
    if contract_category is None:
        await interaction.followup.send("❗ Could not find the Under Contract category.", ephemeral=True)
        return

    if "contracts" not in calendar_state:
        calendar_state["contracts"] = {}

    # Regex to match "Contract dates saved! Signed MM/DD/YY | Closes MM/DD/YY"
    pattern = re.compile(
        r"Contract dates saved! Signed \*\*(.+?)\*\* \| Closes \*\*(.+?)\*\*",
        re.IGNORECASE
    )

    recovered = 0
    skipped   = 0

    for channel in contract_category.text_channels:
        cid = str(channel.id)
        # Skip if already in calendar
        if cid in calendar_state["contracts"]:
            skipped += 1
            continue

        # Scan message history for the confirmation message
        found = False
        async for msg in channel.history(limit=200):
            if msg.author.bot:
                match = pattern.search(msg.content)
                if match:
                    signed  = match.group(1).strip()
                    closing = match.group(2).strip()
                    if parse_date(signed) and parse_date(closing):
                        calendar_state["contracts"][cid] = {
                            "name":    channel.name,
                            "signed":  signed,
                            "closing": closing,
                        }
                        recovered += 1
                        found = True
                        break

        if not found and cid not in calendar_state["contracts"]:
            skipped += 1

    save_json(CALENDAR_FILE, calendar_state)
    await update_calendar_message()
    await interaction.followup.send(
        f"✅ Recovered **{recovered}** contracts from channel history. "
        f"**{skipped}** channels skipped (already saved or no history found).",
        ephemeral=True
    )


# ── Background tasks ─────────────────────────────────────────────────────────
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
        trigger_time = max(since, last_nudge) if last_nudge else since
        if now - trigger_time >= interval:
            channel = client.get_channel(int(channel_id))
            if channel:
                await channel.send(f"⏰ {NUDGES[cat_id]}")
                channel_state[channel_id]["last_nudge"] = now
                save_json(STATE_FILE, channel_state)


@tasks.loop(hours=24)
async def refresh_calendar():
    await update_calendar_message()


# ── Auto-panel on new channel ────────────────────────────────────────────────
@client.event
async def on_guild_channel_create(channel):
    if not isinstance(channel, discord.TextChannel):
        return
    if channel.category_id not in CATEGORIES.values():
        return

    channel_state[str(channel.id)] = {
        "category_id": channel.category_id,
        "since": time.time(),
        "last_nudge": 0
    }
    save_json(STATE_FILE, channel_state)

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


# ── On ready ─────────────────────────────────────────────────────────────────
@client.event
async def on_ready():
    client.add_view(LeadStatusView())
    client.add_view(ReminderSettingsView())
    client.add_view(CalendarManagerViewPersistent())
    nudge_check.start()
    refresh_calendar.start()
    print(f"✅ Logged in as {client.user}")


client.run(TOKEN)
