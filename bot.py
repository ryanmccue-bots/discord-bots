import discord
from discord import app_commands
from discord.ui import View, Button, Modal, TextInput
from discord.ext import tasks
import os
import json
import time
import re
import asyncio
import aiohttp
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
    "closed":    1513595660836929658,
}

NUDGES = {
    CATEGORIES["active"]:    "Did you comp this out yet?? DROP THE OFFER! 🔥",
    CATEGORIES["delivered"]: "Did we lock this up?? 🔒",
}

DISPO_ROLE_ID       = 1477051979317510246
CALENDAR_CHANNEL_ID = 1489343322798559242
EXEMPT_USER_ID      = 622805991242596362
NUDGE_INTERVAL_HOURS = 24
# Where the state files live. Point DATA_DIR at a Railway volume (e.g. /data)
# and the calendar, reminder settings and half-finished deal forms survive
# redeploys - no more /recovercalendar. Unset, they sit next to the code and
# are wiped on every deploy, as before.
DATA_DIR      = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE    = os.path.join(DATA_DIR, "channel_state.json")
CALENDAR_FILE = os.path.join(DATA_DIR, "calendar_state.json")

FARAH_USER_ID  = 1186039817713234123
CARLOS_USER_ID = 1089701097867853865

DEAL_WEBHOOK_TOKEN = os.environ.get("DEAL_WEBHOOK_TOKEN", "")
DEAL_WEBHOOK_URL   = f"https://favoritehomebuyerkpi.up.railway.app/hooks/deal/{DEAL_WEBHOOK_TOKEN}"
CONTRACT_WEBHOOK_URL = f"https://favoritehomebuyerkpi.up.railway.app/hooks/contract/{DEAL_WEBHOOK_TOKEN}"


FARAH_PERMISSIONS = discord.PermissionOverwrite(
    view_channel=True,
    send_messages=True,
    embed_links=True,
    attach_files=True,
    add_reactions=True,
    read_message_history=True,
)

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
    # Write to a temp file and swap it in, so a crash mid-write can't leave a
    # half-written file that loses every contract date on the next start.
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)

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
        # The Contracts tab on the dashboard. After the reply, so a slow
        # dashboard can never make Discord time the form out.
        asyncio.create_task(send_contract(interaction.channel, "dates", signed, closing))


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

    # If moved to Offer Delivered or Declined and Carlos is a member, add Farah
    if cat_id in (CATEGORIES["delivered"], CATEGORIES["declined"]):
        member_ids = [m.id for m in channel.members]
        if CARLOS_USER_ID in member_ids:
            farah = guild.get_member(FARAH_USER_ID)
            if farah:
                await channel.set_permissions(farah, overwrite=FARAH_PERMISSIONS)

    if cat_id == CATEGORIES["contract"]:
        cal_embed = discord.Embed(
            title="Calendar Manager",
            description="Enter the signed and closing dates for this contract:",
            color=0xF1C40F
        )
        await channel.send(embed=cal_embed, view=CalendarManagerViewPersistent())

    channel_state[str(channel.id)] = {
        "category_id": cat_id,
        "since": time.time(),
        "last_nudge": 0,
        # Keep the channel's Reminders On/Off choice across stage moves.
        "reminders_off": channel_state.get(str(channel.id), {}).get("reminders_off", False),
    }
    save_json(STATE_FILE, channel_state)


# ── Slash commands ───────────────────────────────────────────────────────────
@client.tree.command(name="addfarah", description="Grant Farah access to all Carlos Offer Delivered/Declined channels")
async def slash_addfarah(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    farah = guild.get_member(FARAH_USER_ID)
    if not farah:
        await interaction.followup.send("❗ Couldn't find Farah in the server.", ephemeral=True)
        return

    target_categories = (CATEGORIES["delivered"], CATEGORIES["declined"])
    added = 0

    for cat_id in target_categories:
        category = guild.get_channel(cat_id)
        if category is None:
            continue
        for channel in category.text_channels:
            member_ids = [m.id for m in channel.members]
            if CARLOS_USER_ID in member_ids:
                await channel.set_permissions(farah, overwrite=FARAH_PERMISSIONS)
                added += 1

    await interaction.followup.send(f"✅ Added Farah to **{added}** channels.", ephemeral=True)


# ── Closed deals → Revenue dashboard ─────────────────────────────────────────
# The dashboard's "Add closed deal" form replaced the old 3-step Discord form.
# Discord just tells the dashboard a deal closed (address + channel, no
# numbers); the fee and splits are filled in on the Revenue tab. Sending the
# same channel twice is harmless - the dashboard never overwrites a deal that
# already has numbers.
REVENUE_URL = "https://favoritehomebuyerkpi.up.railway.app/revenue"

_ADDRESS_RE = re.compile(r"address of the property[^\n]*\n+\s*(.+)", re.IGNORECASE)
_SELLER_RE  = re.compile(r"name of the seller[^\n]*\n+\s*(.+)", re.IGNORECASE)


def _answer(regex, text: str) -> str:
    m = regex.search(text or "")
    return m.group(1).strip().strip("*_` ").strip() if m else ""


def _address_from_text(text: str) -> str:
    return _answer(_ADDRESS_RE, text)


def lead_from_channel_name(name: str) -> dict:
    """"lori-pumphrey-47-s-3rd-st-sharpsville-pa-16150-carlimmm" ->
    seller "Lori Pumphrey", address "47 S 3rd St Sharpsville PA 16150".
    The seller is the words before the house number; the address runs from
    the house number to the ZIP (what follows the ZIP is the agent)."""
    words = [w for w in name.replace("_", "-").split("-") if w]
    first_num = next((i for i, w in enumerate(words) if w[0].isdigit()), None)
    if first_num is None:
        return {"seller": "", "address": " ".join(w.capitalize() for w in words)}
    zips = [i for i, w in enumerate(words) if i > first_num and re.fullmatch(r"\d{5}", w)]
    stop = zips[-1] + 1 if zips else len(words)
    addr = words[first_num:stop]
    if zips and len(addr) >= 2 and len(addr[-2]) == 2 and addr[-2].isalpha():
        addr[-2] = addr[-2].upper()          # the state
    fix = lambda w: w if w.isupper() else (w if w[0].isdigit() else w.capitalize())
    return {"seller": " ".join(w.capitalize() for w in words[:first_num]),
            "address": " ".join(fix(w) for w in addr)}


async def read_lead(channel) -> dict:
    """Address and seller name from Tickety's opening message (text or embed:
    "1. Address of the Property" / "2. Name of the Seller"), falling back to
    the channel name for whatever isn't found there."""
    found = {"address": "", "seller": ""}
    try:
        async for msg in channel.history(limit=50, oldest_first=True):
            texts = [msg.content or ""]
            for embed in msg.embeds:
                texts.append("\n".join([embed.description or ""]
                                       + [f"{f.name}\n{f.value}" for f in embed.fields]))
            for text in texts:
                found["address"] = found["address"] or _answer(_ADDRESS_RE, text)
                found["seller"] = found["seller"] or _answer(_SELLER_RE, text)
            if found["address"] and found["seller"]:
                break
    except Exception:
        pass
    fallback = lead_from_channel_name(channel.name)
    return {"address": (found["address"] or fallback["address"])[:300],
            "seller": (found["seller"] or fallback["seller"])[:120]}


async def find_address_in_channel(channel) -> str:
    return (await read_lead(channel))["address"]


async def post_deal_webhook(payload: dict) -> dict:
    """POST to the dashboard. {"status": http status, "body": parsed JSON}."""
    if not DEAL_WEBHOOK_TOKEN:
        return {"status": 0, "body": {"error": "DEAL_WEBHOOK_TOKEN is not set"}}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(DEAL_WEBHOOK_URL, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=20)) as resp:
                return {"status": resp.status, "body": await resp.json(content_type=None)}
    except Exception as e:
        return {"status": 0, "body": {"error": str(e)}}


def _closed_day(channel) -> str:
    """When a deal channel went quiet: its last message, else when it was made.
    Used for back-filling channels that were closed before this existed."""
    when = channel.created_at
    if channel.last_message_id:
        when = discord.utils.snowflake_time(channel.last_message_id)
    return when.astimezone(TIMEZONE).date().isoformat()


async def post_contract_webhook(payload: dict) -> dict:
    if not DEAL_WEBHOOK_TOKEN:
        return {"status": 0, "body": {"error": "DEAL_WEBHOOK_TOKEN is not set"}}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(CONTRACT_WEBHOOK_URL, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=20)) as resp:
                return {"status": resp.status, "body": await resp.json(content_type=None)}
    except Exception as e:
        return {"status": 0, "body": {"error": str(e)}}


async def send_contract(channel, action: str, signed: str | None = None,
                        closing: str | None = None) -> dict:
    """Put this channel on the dashboard's Contracts tab.
    action: "dates" (just entered), "entered" (moved into Under Contract),
    "backfill" (start-up sweep), "dropped" (moved out, not closed)."""
    payload = {"contract_id": str(channel.id), "action": action,
               "channel_name": channel.name}
    if action != "dropped":
        payload.update(await read_lead(channel))
        payload["signed"] = signed
        payload["closing"] = closing
    result = await post_contract_webhook(payload)
    if result["status"] not in (200, 201):
        print(f"contract {action} for {channel.name} failed: {result}")
    return result


_DATES_RE = re.compile(r"Contract dates saved! Signed \*\*(.+?)\*\* \| Closes \*\*(.+?)\*\*",
                       re.IGNORECASE)


async def dates_from_history(channel):
    """(signed, closing) from the bot's own "Contract dates saved!" reply, newest first."""
    try:
        async for msg in channel.history(limit=200):
            if msg.author.bot:
                m = _DATES_RE.search(msg.content or "")
                if m and parse_date(m.group(1)) and parse_date(m.group(2)):
                    return m.group(1).strip(), m.group(2).strip()
    except Exception:
        pass
    return None, None


async def sync_contract_channels(guild) -> tuple[int, int]:
    """Every Under Contract channel onto the Contracts tab, with its dates
    (from the calendar, else from the channel's history - which also puts
    them back on the Discord calendar). (sent, failed)."""
    category = guild.get_channel(CATEGORIES["contract"])
    if category is None:
        return 0, 0
    calendar_state.setdefault("contracts", {})
    sent = failed = 0
    restored = False
    ids = []
    for channel in category.text_channels:
        cid = str(channel.id)
        ids.append(cid)
        saved = calendar_state["contracts"].get(cid)
        signed, closing = (saved["signed"], saved["closing"]) if saved else await dates_from_history(channel)
        if closing and not saved:
            calendar_state["contracts"][cid] = {"name": channel.name, "signed": signed, "closing": closing}
            restored = True
        result = await send_contract(channel, "backfill", signed, closing)
        if result["status"] in (200, 201):
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.5)
    if restored:
        save_json(CALENDAR_FILE, calendar_state)
        await update_calendar_message()
    # Contracts on the dashboard whose channel left Under Contract while the
    # bot was down. Only when the read worked: an empty list proves nothing.
    if ids and not failed:
        await post_contract_webhook({"action": "sync", "ids": ids})
    return sent, failed


async def send_to_revenue(channel, closed_on: str | None = None) -> dict:
    """Tell the dashboard this channel's deal closed. No numbers."""
    address = await find_address_in_channel(channel)
    payload = {"deal_id": str(channel.id), "address": address,
               "channel_name": channel.name, "pending": True,
               "closed_on": closed_on or today_local().isoformat()}
    result = await post_deal_webhook(payload)
    if result["status"] in (200, 201):
        channel_state.setdefault("_sent", {})[str(channel.id)] = time.time()
        save_json(STATE_FILE, channel_state)
    return result


def _drop_from_calendar(channel_id: str) -> bool:
    if channel_id in calendar_state.get("contracts", {}):
        del calendar_state["contracts"][channel_id]
        save_json(CALENDAR_FILE, calendar_state)
        return True
    return False


@client.event
async def on_guild_channel_update(before, after):
    """Category changes. Into Closed Deals: same as /close. Into Under
    Contract: onto the Contracts tab. Out of Under Contract to anywhere but
    Closed Deals: off the Contracts tab (it fell through)."""
    if not isinstance(after, discord.TextChannel):
        return
    if before.category_id == after.category_id:
        return
    # A channel dragged to another category (not via the buttons) starts its
    # new stage fresh too: one reminder 24 hours in, reminder setting kept.
    prev = channel_state.get(str(after.id), {})
    channel_state[str(after.id)] = {"category_id": after.category_id, "since": time.time(),
                                    "last_nudge": 0, "reminders_off": prev.get("reminders_off", False)}
    save_json(STATE_FILE, channel_state)
    if after.category_id == CATEGORIES["contract"]:
        await send_contract(after, "entered")
        return
    if before.category_id == CATEGORIES["contract"] and after.category_id != CATEGORIES["closed"]:
        await send_contract(after, "dropped")
        return
    if after.category_id != CATEGORIES["closed"]:
        return
    if str(after.id) in channel_state.get("_sent", {}):
        return   # /close already sent it
    result = await send_to_revenue(after)
    if _drop_from_calendar(str(after.id)):
        await update_calendar_message()
    if result["status"] in (200, 201):
        await after.send(f"📋 Added to the Revenue dashboard. Fill in the fee and splits there: {REVENUE_URL}")


# ── /close command ────────────────────────────────────────────────────────────
@client.tree.command(name="close", description="Close this deal: add it to the Revenue dashboard and move it to Closed Deals")
async def slash_close(interaction: discord.Interaction):
    channel = interaction.channel
    category = interaction.guild.get_channel(CATEGORIES["closed"])
    if category is None:
        await interaction.response.send_message("❗ Closed Deals category not found.", ephemeral=True)
        return
    await interaction.response.defer()
    result = await send_to_revenue(channel)
    if result["status"] not in (200, 201):
        await interaction.followup.send(
            f"⚠️ Couldn't reach the dashboard ({result['status']}: {result['body']}). "
            "The channel was NOT moved. Try `/close` again in a minute.")
        return
    if channel.category_id != CATEGORIES["closed"]:
        await channel.edit(category=category)
    if _drop_from_calendar(str(channel.id)):
        await update_calendar_message()
    await interaction.followup.send(
        f"🎉 Deal closed by {interaction.user.mention}. Fill in the fee and splits on the "
        f"Revenue dashboard: {REVENUE_URL}")


async def sync_closed_channels(guild) -> tuple[int, int]:
    """Send every channel in Closed Deals to the dashboard. (sent, failed)."""
    category = guild.get_channel(CATEGORIES["closed"])
    if category is None:
        return 0, 0
    sent = failed = 0
    for channel in category.text_channels:
        result = await send_to_revenue(channel, closed_on=_closed_day(channel))
        if result["status"] in (200, 201):
            sent += 1
        else:
            failed += 1
            print(f"syncclosed: {channel.name} failed: {result}")
        await asyncio.sleep(0.5)
    return sent, failed


@client.tree.command(name="syncclosed", description="Add every Closed Deals channel to the Revenue dashboard (missing ones only)")
async def slash_syncclosed(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    sent, failed = await sync_closed_channels(interaction.guild)
    await interaction.followup.send(
        f"✅ Checked {sent + failed} closed channels: {sent} on the dashboard"
        + (f", {failed} failed (see the bot logs)." if failed else "."), ephemeral=True)



@client.tree.command(name="synccontracts", description="Put every Under Contract channel on the dashboard's Contracts tab")
async def slash_synccontracts(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    sent, failed = await sync_contract_channels(interaction.guild)
    await interaction.followup.send(
        f"✅ Checked {sent + failed} Under Contract channels: {sent} on the dashboard"
        + (f", {failed} failed (see the bot logs)." if failed else "."), ephemeral=True)


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
        # One reminder per stage, never a daily repeat. Moving the channel to
        # another stage starts fresh (last_nudge back to 0), so the next stage
        # gets its own single reminder 24 hours in.
        if last_nudge:
            continue
        if now - since >= interval:
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
        "last_nudge": 0,
        "reminders_off": True   # off by default; toggle via Reminder Settings panel
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
    # on_ready fires again after every reconnect; starting a running loop raises.
    if not nudge_check.is_running():
        nudge_check.start()
    if not refresh_calendar.is_running():
        refresh_calendar.start()
    print(f"✅ Logged in as {client.user}")
    # Back-fill: every Closed Deals channel onto the Revenue dashboard. Runs on
    # each start; the dashboard ignores channels it already has.
    if not getattr(client, "_synced_closed", False):
        client._synced_closed = True
        guild = client.get_guild(GUILD_ID)
        if guild:
            sent, failed = await sync_closed_channels(guild)
            print(f"   closed deals on the dashboard: {sent} ok, {failed} failed")
            sent, failed = await sync_contract_channels(guild)
            print(f"   contracts on the dashboard: {sent} ok, {failed} failed")


client.run(TOKEN)
