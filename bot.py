import discord
from discord import app_commands
from discord.ui import View, Button, Modal, TextInput
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
STATE_FILE    = "channel_state.json"
CALENDAR_FILE = "calendar_state.json"

FARAH_USER_ID  = 1186039817713234123
CARLOS_USER_ID = 1089701097867853865

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


@client.tree.command(name="close", description="Move this channel to Closed Deals")
async def slash_close(interaction: discord.Interaction):
    guild    = interaction.guild
    channel  = interaction.channel
    category = guild.get_channel(CATEGORIES["closed"])

    if category is None:
        await interaction.response.send_message("❗ Closed Deals category not found.", ephemeral=True)
        return

    await channel.edit(category=category)

    # Remove from calendar if present
    cid = str(channel.id)
    if cid in calendar_state.get("contracts", {}):
        del calendar_state["contracts"][cid]
        save_json(CALENDAR_FILE, calendar_state)
        await update_calendar_message()

    await interaction.response.send_message(
        f"🎉 Deal closed! Moved by {interaction.user.mention}\n"
        "Fill in the **Log Closed Deal** form below so it reaches the Revenue tab."
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


# ── Closed-deal reporting ────────────────────────────────────────────────────
# When a channel lands in Closed Deals, a form collects what the deal was worth
# and who gets paid, and posts it to the KPI dashboard. See DEAL_WEBHOOK.md.
#
# A Discord modal allows five fields and this needs six things: address, fee,
# acquisition agent(s), disposition agent, lead manager, and - when back-
# filling an old channel - the date it closed. So the date is a slash-command
# argument rather than a modal field, and the agent fields take "Name: amount"
# a line at a time. That also solves splits: two acquisition agents are two
# lines, not a second form.

KPI_BASE_URL = os.environ.get(
    "KPI_BASE_URL", "https://favoritehomebuyerkpi.up.railway.app").rstrip("/")
DEAL_WEBHOOK_TOKEN = os.environ.get("DEAL_WEBHOOK_TOKEN", "")

# "Carlos: 2000", "Carlos - 2,000", "Carlos $2000" - one person per line.
PERSON_LINE = re.compile(r"^\s*(.+?)\s*[:\-–]?\s*\$?\s*([\d,]+(?:\.\d{1,2})?)\s*$")


def parse_money_text(raw):
    """A typed amount to a number, or None if it is not one."""
    if raw is None:
        return None
    text = str(raw).strip().lstrip("$").replace(",", "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value >= 0 else None


def parse_people_lines(raw, amount_key):
    """``"Carlos: 2000\\nTrevor: 1800"`` to payload entries.

    Returns ``(entries, bad_lines)``. A line that cannot be read is reported
    rather than skipped - a silently dropped line is a silently unpaid person.
    """
    entries, bad = [], []
    for line in (raw or "").splitlines():
        if not line.strip():
            continue
        match = PERSON_LINE.match(line)
        if not match:
            bad.append(line.strip())
            continue
        name = match.group(1).strip().rstrip(":-–").strip()
        amount = parse_money_text(match.group(2))
        if not name or amount is None:
            bad.append(line.strip())
            continue
        entries.append({"person": name, amount_key: amount})
    return entries, bad


def guess_address(channel):
    """A starting point for the address field, for a human to correct.

    Channel names are slugs - "1234-briarwood-drive" - so this un-slugs one
    into something recognisable. It is deliberately only a prefill: guessing
    an address wrong and storing it silently is worse than asking.
    """
    name = (channel.name or "").replace("-", " ").replace("_", " ").strip()
    return " ".join(word.capitalize() if word.islower() else word
                    for word in name.split())[:250]


async def post_deal(payload):
    """Send one deal to the dashboard. Returns ``(ok, detail)``."""
    if not DEAL_WEBHOOK_TOKEN:
        return False, ("DEAL_WEBHOOK_TOKEN is not set on this bot, so there is "
                       "nowhere to send the deal.")
    url = f"{KPI_BASE_URL}/hooks/deal/{DEAL_WEBHOOK_TOKEN}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=30)) as resp:
                body = await resp.text()
                if resp.status == 200:
                    try:
                        return True, json.loads(body)
                    except ValueError:
                        return True, {}
                if resp.status == 404:
                    return False, ("The dashboard did not recognise the webhook "
                                   "token, or the Revenue build is not deployed "
                                   "yet.")
                try:
                    return False, json.loads(body).get("error", body[:300])
                except ValueError:
                    return False, body[:300]
    except Exception as exc:  # noqa: BLE001 - never crash the bot over a deal
        return False, f"Could not reach the dashboard: {exc}"


class ClosedDealModal(Modal, title="Closed Deal"):
    address = TextInput(label="Property address", max_length=250)
    fee = TextInput(label="Assignment fee (what the company made)",
                    placeholder="18500", max_length=20)
    acquisitions = TextInput(
        label="Acquisition agent(s) — one per line",
        style=discord.TextStyle.paragraph, required=False,
        placeholder="Carlos: 2000\nTrevor: 1800   ← two lines for a split deal",
        max_length=400)
    disposition = TextInput(label="Disposition agent", required=False,
                            placeholder="Marcus: 1500", max_length=120)
    lead_manager = TextInput(label="Lead manager bonus", required=False,
                             placeholder="Joy: 500", max_length=120)

    def __init__(self, channel, closed_on=None):
        super().__init__()
        self.channel_id = str(channel.id)
        self.channel_name = channel.name
        self.closed_on = closed_on
        self.address.default = guess_address(channel)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()

        fee = parse_money_text(self.fee.value)
        if fee is None:
            await interaction.followup.send(
                f"❗ I could not read the assignment fee: `{self.fee.value}`. "
                "Enter it as a number, like `18500`.", ephemeral=True)
            return

        acq, bad_acq = parse_people_lines(self.acquisitions.value, "commission")
        dispo, bad_dispo = parse_people_lines(self.disposition.value, "commission")
        lead, bad_lead = parse_people_lines(self.lead_manager.value, "bonus")
        bad = bad_acq + bad_dispo + bad_lead
        if bad:
            await interaction.followup.send(
                "❗ I could not read these lines:\n"
                + "\n".join(f"• `{line}`" for line in bad)
                + "\n\nUse `Name: amount`, one per line — for example "
                  "`Carlos: 2000`.", ephemeral=True)
            return

        payload = {
            "deal_id": self.channel_id,
            "address": self.address.value.strip(),
            "assignment_fee": fee,
            "acquisitions": acq,
            "channel_name": self.channel_name,
            "recorded_by": str(interaction.user),
        }
        if dispo:
            payload["disposition"] = dispo[0]
        if lead:
            payload["lead_manager"] = lead[0]
        if self.closed_on:
            payload["closed_on"] = self.closed_on

        ok, detail = await post_deal(payload)
        if not ok:
            await interaction.followup.send(f"❗ The dashboard refused it: {detail}")
            return

        paid = sum(e["commission"] for e in acq) \
            + sum(e["commission"] for e in dispo) \
            + sum(e["bonus"] for e in lead)
        again = "" if detail.get("created", True) else " *(updated the existing record)*"
        lines = [
            f"💰 **Deal logged**{again}",
            f"**{payload['address']}** — fee **${fee:,.0f}**, "
            f"**${paid:,.0f}** paid out",
        ]
        for entry in acq:
            lines.append(f"• Acquisitions — {entry['person']}: ${entry['commission']:,.0f}")
        for entry in dispo:
            lines.append(f"• Dispositions — {entry['person']}: ${entry['commission']:,.0f}")
        for entry in lead:
            lines.append(f"• Lead manager — {entry['person']}: ${entry['bonus']:,.0f}")
        # Names the dashboard could not place. Worth saying out loud now, while
        # somebody still remembers the deal - an unattributed payout that is
        # never mentioned is one nobody fixes.
        unresolved = (detail or {}).get("unresolved") or []
        if unresolved:
            lines.append("")
            lines.append("⚠️ The dashboard did not recognise: "
                         + ", ".join(f"**{name}**" for name in unresolved)
                         + ". Their payout is recorded but not attributed to "
                           "anyone — re-run `/logdeal` with the spelling the "
                           "dashboard uses.")
        await interaction.followup.send("\n".join(lines))


class ClosedDealView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="💰 Log Closed Deal", style=discord.ButtonStyle.success,
                       custom_id="log_closed_deal")
    async def log_deal(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(ClosedDealModal(interaction.channel))


async def post_closed_deal_form(channel):
    """Post the form once per channel, and remember that it was posted."""
    state = channel_state.setdefault(str(channel.id), {})
    if state.get("deal_form_posted"):
        return
    embed = discord.Embed(
        title="Closed Deal",
        description=("Log what this deal was worth so it lands on the Revenue "
                     "tab.\n\nFor a split, put each acquisition agent on their "
                     "own line."),
        color=0x2ECC71,
    )
    await channel.send(embed=embed, view=ClosedDealView())
    state["deal_form_posted"] = True
    save_json(STATE_FILE, channel_state)


@client.tree.command(name="logdeal",
                     description="Log this closed deal on the Revenue dashboard")
@app_commands.describe(
    closed_on="Only for old deals — the date it closed, MM/DD/YYYY. "
              "Leave blank for today.")
async def slash_logdeal(interaction: discord.Interaction, closed_on: str = ""):
    """The back-fill command, and a way to correct a deal already sent.

    Re-running it on a channel that was already logged corrects the record
    rather than double-counting: the dashboard keys deals on the channel id.
    """
    when = ""
    if closed_on.strip():
        parsed = parse_date(closed_on)
        if parsed is None:
            await interaction.response.send_message(
                "❗ I could not read that date. Use MM/DD/YYYY.", ephemeral=True)
            return
        when = parsed.isoformat()
    await interaction.response.send_modal(
        ClosedDealModal(interaction.channel, closed_on=when))


@client.event
async def on_guild_channel_update(before, after):
    """Catch a channel dragged into Closed Deals, not just /close.

    People move channels by dragging them in the sidebar as often as by using
    the command, and a form that only appears one of those ways is a form that
    quietly misses deals.
    """
    if not isinstance(after, discord.TextChannel):
        return
    if before.category_id == after.category_id:
        return
    if after.category_id != CATEGORIES["closed"]:
        return
    try:
        await post_closed_deal_form(after)
    except Exception as exc:  # noqa: BLE001 - a failed form must not crash the bot
        print(f"could not post the closed-deal form in {after.name}: {exc}")


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
    client.add_view(ClosedDealView())
    nudge_check.start()
    refresh_calendar.start()
    print(f"✅ Logged in as {client.user}")


client.run(TOKEN)
