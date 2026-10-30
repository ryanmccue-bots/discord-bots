import discord
from discord import app_commands
from discord.ui import View, Button, Modal, TextInput, Select
from discord.ext import tasks
import os
import json
import time
import re
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

# Roster for deal form dropdowns
ROSTER = [
    {"label": "Carlos",  "email": "carlos@favoritehomebuyer.com"},
    {"label": "Trevor",  "email": "tdarealestate@gmail.com"},
    {"label": "Joy",     "email": "joy@favoritehomebuyer.com"},
    {"label": "Farah",   "email": "Farah"},
    {"label": "Joe",     "email": "Joe Monteverde"},
    {"label": "None",    "email": None},
]

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
        "last_nudge": 0
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


# ── Deal form helpers ────────────────────────────────────────────────────────

def roster_options(include_none=True):
    """Build Select options from ROSTER."""
    opts = []
    for r in ROSTER:
        if r["email"] is None and not include_none:
            continue
        opts.append(discord.SelectOption(label=r["label"], value=r["label"]))
    return opts

def roster_email(label: str):
    """Return email/name to send in webhook for a given roster label."""
    for r in ROSTER:
        if r["label"] == label:
            return r["email"]
    return label   # unknown — send as-is so dashboard can flag it

async def find_address_in_channel(channel) -> str:
    """Scan channel history for the Tickety bot's address field."""
    pattern = re.compile(
        r"1\.\s*Address of the Property\s*\n(.+)",
        re.IGNORECASE
    )
    async for msg in channel.history(limit=200, oldest_first=True):
        m = pattern.search(msg.content)
        if m:
            return m.group(1).strip()
    return ""

async def post_deal_webhook(payload: dict) -> dict | None:
    """POST payload to the deal webhook. Returns parsed JSON or None on error."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(DEAL_WEBHOOK_URL, json=payload) as resp:
                return {"status": resp.status, "body": await resp.json(content_type=None)}
    except Exception as e:
        return {"status": 0, "body": {"error": str(e)}}


# ── Deal form — step 1: address + assignment fee ─────────────────────────────
class DealStep1Modal(Modal, title="Close Deal — Step 1 of 3"):
    address = TextInput(
        label="Property Address",
        placeholder="Auto-filled from Tickety if blank — edit if wrong",
        max_length=300,
        required=True,
    )
    assignment_fee = TextInput(
        label="Assignment Fee ($)",
        placeholder="e.g. 18500",
        max_length=20,
        required=True,
    )
    notes = TextInput(
        label="Notes (optional)",
        style=discord.TextStyle.short,
        max_length=300,
        required=False,
    )
    backfill_date = TextInput(
        label="Close Date (leave blank = today)",
        placeholder="MM/DD/YYYY — only needed for backfilling old deals",
        max_length=10,
        required=False,
    )

    def __init__(self, prefill_address: str, channel_id: str, channel_name: str, recorder: str):
        super().__init__()
        self.address.default = prefill_address
        self._channel_id   = channel_id
        self._channel_name = channel_name
        self._recorder     = recorder

    async def on_submit(self, interaction: discord.Interaction):
        # Store partial deal in channel state for step 2
        deal_draft = {
            "deal_id":        self._channel_id,
            "address":        self.address.value.strip(),
            "assignment_fee": self.assignment_fee.value.strip(),
            "notes":          self.notes.value.strip(),
            "backfill_date":  self.backfill_date.value.strip(),
            "channel_name":   self._channel_name,
            "recorded_by":    self._recorder,
            "acquisitions":   [],
            "disposition":    None,
            "lead_manager":   None,
        }
        channel_state.setdefault("_deal_drafts", {})[self._channel_id] = deal_draft
        save_json(STATE_FILE, channel_state)

        await interaction.response.send_message(
            "**Step 2 of 3 — Acquisition Agent(s)**\nWho were the acquisition agent(s) on this deal? "
            "Select all that apply, then click **Next**.",
            view=DealStep2View(self._channel_id),
            ephemeral=True,
        )


# ── Deal form — step 2: acquisition agent(s) ─────────────────────────────────
class AcqAgentSelect(Select):
    def __init__(self):
        super().__init__(
            placeholder="Select acquisition agent(s)…",
            min_values=0,
            max_values=len(ROSTER) - 1,  # exclude None
            options=roster_options(include_none=False),
            custom_id="acq_agent_select",
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()


class DealStep2View(View):
    def __init__(self, channel_id: str):
        super().__init__(timeout=300)
        self._channel_id = channel_id
        self.acq_select = AcqAgentSelect()
        self.add_item(self.acq_select)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.primary, row=1)
    async def next_btn(self, interaction: discord.Interaction, button: Button):
        selected = self.acq_select.values
        draft = channel_state.get("_deal_drafts", {}).get(self._channel_id)
        if draft is None:
            await interaction.response.send_message("❗ Deal draft lost — please run /close again.", ephemeral=True)
            return
        draft["_acq_agents"] = selected  # list of labels; commissions collected in modal
        channel_state.setdefault("_deal_drafts", {})[self._channel_id] = draft
        save_json(STATE_FILE, channel_state)

        if selected:
            # Ask commissions for each agent via a modal
            await interaction.response.send_modal(AcqCommissionsModal(self._channel_id, selected))
        else:
            # No acquisition agents — jump to step 3
            await interaction.response.send_message(
                "**Step 3 of 3 — Disposition & Lead Manager**",
                view=DealStep3View(self._channel_id),
                ephemeral=True,
            )


class AcqCommissionsModal(Modal):
    def __init__(self, channel_id: str, agents: list[str]):
        super().__init__(title="Acquisition Commissions")
        self._channel_id = channel_id
        self._agents = agents
        for agent in agents[:5]:  # Modal max 5 inputs
            self.add_item(TextInput(
                label=f"{agent} — Commission ($)",
                placeholder="e.g. 2000",
                max_length=20,
                required=True,
                custom_id=f"acq_{agent}",
            ))

    async def on_submit(self, interaction: discord.Interaction):
        draft = channel_state.get("_deal_drafts", {}).get(self._channel_id)
        if draft is None:
            await interaction.response.send_message("❗ Deal draft lost — please run /close again.", ephemeral=True)
            return

        acquisitions = []
        for item in self.children:
            agent_label = item.label.split(" — ")[0]
            acquisitions.append({
                "person":     roster_email(agent_label),
                "commission": item.value.strip(),
            })
        draft["acquisitions"] = acquisitions
        channel_state["_deal_drafts"][self._channel_id] = draft
        save_json(STATE_FILE, channel_state)

        await interaction.response.send_message(
            "**Step 3 of 3 — Disposition & Lead Manager**",
            view=DealStep3View(self._channel_id),
            ephemeral=True,
        )


# ── Deal form — step 3: dispo agent + lead manager ───────────────────────────
class DispoAgentSelect(Select):
    def __init__(self):
        super().__init__(
            placeholder="Disposition agent (or None)…",
            min_values=1,
            max_values=1,
            options=roster_options(include_none=True),
            custom_id="dispo_agent_select",
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()


class LeadManagerSelect(Select):
    def __init__(self):
        super().__init__(
            placeholder="Lead manager (or None)…",
            min_values=1,
            max_values=1,
            options=roster_options(include_none=True),
            custom_id="lead_manager_select",
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()


class DealStep3View(View):
    def __init__(self, channel_id: str):
        super().__init__(timeout=300)
        self._channel_id = channel_id
        self.dispo_select = DispoAgentSelect()
        self.lm_select    = LeadManagerSelect()
        self.add_item(self.dispo_select)
        self.add_item(self.lm_select)

    @discord.ui.button(label="Submit Deal ✅", style=discord.ButtonStyle.success, row=2)
    async def submit_btn(self, interaction: discord.Interaction, button: Button):
        dispo_label = self.dispo_select.values[0] if self.dispo_select.values else "None"
        lm_label    = self.lm_select.values[0]    if self.lm_select.values    else "None"

        if dispo_label in ("None", ""):
            await interaction.response.send_modal(
                DealCommissionsModal(self._channel_id, dispo_label, lm_label, need_dispo_commission=False)
            )
        else:
            await interaction.response.send_modal(
                DealCommissionsModal(self._channel_id, dispo_label, lm_label, need_dispo_commission=True)
            )


class DealCommissionsModal(Modal):
    def __init__(self, channel_id: str, dispo_label: str, lm_label: str, need_dispo_commission: bool):
        super().__init__(title="Commissions & Bonus")
        self._channel_id          = channel_id
        self._dispo_label         = dispo_label
        self._lm_label            = lm_label
        self._need_dispo_commission = need_dispo_commission

        if need_dispo_commission:
            self.add_item(TextInput(
                label=f"{dispo_label} — Dispo Commission ($)",
                placeholder="e.g. 1500",
                max_length=20,
                required=True,
                custom_id="dispo_commission",
            ))

        if lm_label not in ("None", ""):
            self.add_item(TextInput(
                label=f"{lm_label} — Lead Manager Bonus ($)",
                placeholder="e.g. 500",
                max_length=20,
                required=True,
                custom_id="lm_bonus",
            ))

    async def on_submit(self, interaction: discord.Interaction):
        draft = channel_state.get("_deal_drafts", {}).get(self._channel_id)
        if draft is None:
            await interaction.response.send_message("❗ Deal draft lost — please run /close again.", ephemeral=True)
            return

        # Extract submitted values by custom_id
        values = {item.custom_id: item.value.strip() for item in self.children}

        dispo_commission = values.get("dispo_commission", "0")
        lm_bonus         = values.get("lm_bonus", "0")

        if self._dispo_label not in ("None", ""):
            draft["disposition"] = {
                "person":     roster_email(self._dispo_label),
                "commission": dispo_commission,
            }

        if self._lm_label not in ("None", ""):
            draft["lead_manager"] = {
                "person": roster_email(self._lm_label),
                "bonus":  lm_bonus,
            }

        channel_state["_deal_drafts"][self._channel_id] = draft
        save_json(STATE_FILE, channel_state)

        await interaction.response.defer(ephemeral=True)
        await _finalize_deal(interaction, self._channel_id)


async def _finalize_deal(interaction: discord.Interaction, channel_id: str):
    """Build payload, POST to webhook, move channel, clean up."""
    draft = channel_state.get("_deal_drafts", {}).pop(channel_id, None)
    if draft is None:
        await interaction.followup.send("❗ Deal draft lost — please run /close again.", ephemeral=True)
        return

    # Build closed_on date
    backfill = draft.get("backfill_date", "").strip()
    if backfill:
        parsed_close = parse_date(backfill)
        closed_on = parsed_close.strftime("%Y-%m-%d") if parsed_close else None
    else:
        closed_on = None   # webhook defaults to today

    payload = {
        "deal_id":        channel_id,
        "address":        draft["address"],
        "assignment_fee": draft["assignment_fee"],
        "acquisitions":   draft.get("acquisitions", []),
        "channel_name":   draft["channel_name"],
        "recorded_by":    draft["recorded_by"],
        "notes":          draft.get("notes", ""),
    }
    if draft.get("disposition"):
        payload["disposition"] = draft["disposition"]
    if draft.get("lead_manager"):
        payload["lead_manager"] = draft["lead_manager"]
    if closed_on:
        payload["closed_on"] = closed_on

    # POST to webhook
    result = await post_deal_webhook(payload)
    status = result["status"] if result else 0
    body   = result["body"]   if result else {}

    if status not in (200, 201):
        await interaction.followup.send(
            f"⚠️ Deal form submitted but webhook returned **{status}**: `{body}`. "
            "The channel was NOT moved. Fix the issue and run `/close` again.",
            ephemeral=True,
        )
        return

    # Remember the deal is recorded BEFORE moving the channel, so the move
    # doesn't trigger the "record this deal" prompt for a deal just recorded.
    channel_state.setdefault("_recorded", {})[channel_id] = time.time()
    save_json(STATE_FILE, channel_state)

    # Move channel to Closed Deals
    guild    = interaction.guild
    channel  = guild.get_channel(int(channel_id))
    category = guild.get_channel(CATEGORIES["closed"])
    if channel and category:
        await channel.edit(category=category)

    # Remove from calendar if present
    if channel_id in calendar_state.get("contracts", {}):
        del calendar_state["contracts"][channel_id]
        save_json(CALENDAR_FILE, calendar_state)
        await update_calendar_message()

    save_json(STATE_FILE, channel_state)

    # Build success message
    unresolved = body.get("unresolved", [])
    msg_lines = [f"🎉 **Deal closed!** Recorded by {interaction.user.mention}"]
    if unresolved:
        msg_lines.append(
            f"⚠️ **Heads up — these names couldn't be matched in the dashboard:** "
            f"{', '.join(unresolved)}. Ping Ryan to fix attribution."
        )

    if channel:
        await channel.send("\n".join(msg_lines))
    await interaction.followup.send("✅ Deal submitted and channel moved to Closed Deals.", ephemeral=True)


class _OpenStep1View(View):
    """Ephemeral button that opens the Step 1 modal (needed because modals can't follow a defer)."""
    def __init__(self, prefill_address: str, channel_id: str, channel_name: str, recorder: str):
        super().__init__(timeout=300)
        self._prefill  = prefill_address
        self._cid      = channel_id
        self._name     = channel_name
        self._recorder = recorder

    @discord.ui.button(label="📋 Open Deal Form", style=discord.ButtonStyle.primary)
    async def open_form(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(
            DealStep1Modal(self._prefill, self._cid, self._name, self._recorder)
        )


# ── "Record this deal" prompt (channel moved into Closed Deals by hand) ───────
class RecordDealView(View):
    """Posted when a channel lands in Closed Deals without going through /close.
    Persistent, so the button still works after a redeploy."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📋 Record closed deal", style=discord.ButtonStyle.success,
                       custom_id="record_deal")
    async def record(self, interaction: discord.Interaction, button: Button):
        channel = interaction.channel
        cid = str(channel.id)
        if cid in channel_state.get("_recorded", {}):
            await interaction.response.send_message(
                "✅ This deal is already recorded. Use `/close` to send a correction.", ephemeral=True)
            return
        # The address was looked up when the prompt was posted - a modal has to
        # open within 3 seconds, too soon to scan the channel history.
        prefill = channel_state.get("_prompt_address", {}).get(cid, "")
        await interaction.response.send_modal(
            DealStep1Modal(prefill, cid, channel.name, interaction.user.display_name))


@client.event
async def on_guild_channel_update(before, after):
    if not isinstance(after, discord.TextChannel):
        return
    if before.category_id == after.category_id or after.category_id != CATEGORIES["closed"]:
        return
    cid = str(after.id)
    if cid in channel_state.get("_recorded", {}):
        return   # moved by /close after the deal was sent
    address = await find_address_in_channel(after)
    channel_state.setdefault("_prompt_address", {})[cid] = address
    save_json(STATE_FILE, channel_state)
    embed = discord.Embed(
        title="Closed deal — record the numbers",
        description=("This channel was moved to Closed Deals. Record the assignment fee "
                     "and splits so they show up on the Revenue dashboard."),
        color=0x2ECC71,
    )
    await after.send(embed=embed, view=RecordDealView())


# ── /close command ────────────────────────────────────────────────────────────
@client.tree.command(name="close", description="Close this deal — collect details and move channel to Closed Deals")
async def slash_close(interaction: discord.Interaction):
    channel = interaction.channel
    cid     = str(channel.id)

    # Check Closed Deals category exists
    if interaction.guild.get_channel(CATEGORIES["closed"]) is None:
        await interaction.response.send_message("❗ Closed Deals category not found.", ephemeral=True)
        return

    # Try to auto-fill the address from Tickety bot history
    await interaction.response.defer(ephemeral=True)
    prefill_address = await find_address_in_channel(channel)

    recorder = interaction.user.display_name

    await interaction.followup.send(
        "**Close Deal — Step 1 of 3**\nFill in the address and assignment fee:",
        ephemeral=True,
    )
    # We can't send a modal after followup, so we send a view that opens the modal
    await interaction.followup.send(
        "👇 Click below to open the deal form:",
        view=_OpenStep1View(prefill_address, cid, channel.name, recorder),
        ephemeral=True,
    )


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
    client.add_view(RecordDealView())
    # on_ready fires again after every reconnect; starting a running loop raises.
    if not nudge_check.is_running():
        nudge_check.start()
    if not refresh_calendar.is_running():
        refresh_calendar.start()
    print(f"✅ Logged in as {client.user}")


client.run(TOKEN)
