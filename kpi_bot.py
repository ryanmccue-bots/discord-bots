import discord
from discord import app_commands
import pandas as pd
import os
import io
from datetime import datetime, timedelta

# ── Config ──────────────────────────────────────────────────────────────────
DISCORD_TOKEN    = os.environ["KPI_BOT_TOKEN"]
APPLICATION_ID   = 1514008129472303115
KPI_CHANNEL_ID   = 1513652941515653441

# ── Bot setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)

# ── Helpers ──────────────────────────────────────────────────────────────────
def clean_val(v, default=0):
    try:
        if pd.isna(v): return default
        return int(v)
    except:
        return default

def parse_scorecard(sc: pd.DataFrame) -> dict:
    """Pull per-person metrics from the scorecard."""
    result = {}
    for _, row in sc.iterrows():
        user = str(row.get("User", "")).strip()
        if not user or user.lower() == "total":
            continue
        result[user] = {
            "outbound_calls":     clean_val(row.get("Outbound Calls")),
            "contacts":           clean_val(row.get("Contacts")),
            "appointments":       clean_val(row.get("Appointments Booked")),
            "verbal_offers":      clean_val(row.get("Verbal Offers Made")),
            "contracts_accepted": clean_val(row.get("Contracts Accepted")),
            "dead":               clean_val(row.get("Dead Opportunities")),
        }
    return result

def count_new_leads(crm: pd.DataFrame, start: datetime, end: datetime) -> int:
    """Count leads created within the date range."""
    crm["date_created"] = pd.to_datetime(crm["Date Created"], errors="coerce")
    mask = (crm["date_created"] >= start) & (crm["date_created"] <= end)
    return int(mask.sum())

def get_new_lead_names(crm: pd.DataFrame, start: datetime, end: datetime) -> list:
    """Return names of new leads in the date range."""
    crm["date_created"] = pd.to_datetime(crm["Date Created"], errors="coerce")
    mask = (crm["date_created"] >= start) & (crm["date_created"] <= end)
    rows = crm[mask]
    names = []
    for _, r in rows.iterrows():
        fn = str(r.get("Seller First Name", "")).strip()
        ln = str(r.get("Seller Last Name", "")).strip()
        campaign = str(r.get("Campaign", "")).strip()
        names.append({"name": f"{fn} {ln}".strip(), "campaign": campaign})
    return names

def get_appointment_names(crm: pd.DataFrame, owner_name: str) -> list:
    """Leads with seller-appointment action event for a given owner."""
    mask = (
        crm["Action Event"].str.contains("seller-appointment", case=False, na=False) &
        crm["Owner"].str.contains(owner_name.split()[0], case=False, na=False)
    )
    rows = crm[mask]
    names = []
    for _, r in rows.iterrows():
        fn = str(r.get("Seller First Name", "")).strip()
        ln = str(r.get("Seller Last Name", "")).strip()
        names.append(f"{fn} {ln}".strip())
    return names

def get_offer_names(crm: pd.DataFrame, owner_name: str) -> list:
    """Leads with a logged Date of 1st Offer for a given owner."""
    mask = (
        crm["Date of 1st Offer"].notna() &
        crm["Owner"].str.contains(owner_name.split()[0], case=False, na=False)
    )
    rows = crm[mask]
    names = []
    for _, r in rows.iterrows():
        fn = str(r.get("Seller First Name", "")).strip()
        ln = str(r.get("Seller Last Name", "")).strip()
        offer_date = str(r.get("Date of 1st Offer", "")).strip()
        names.append(f"{fn} {ln} ({offer_date})")
    return names

def get_contract_names(crm: pd.DataFrame, owner_name: str) -> list:
    """Leads in transaction pipeline for a given owner."""
    mask = (
        (crm["Pipeline"] == "transaction") &
        crm["Owner"].str.contains(owner_name.split()[0], case=False, na=False)
    )
    rows = crm[mask]
    names = []
    for _, r in rows.iterrows():
        fn = str(r.get("Seller First Name", "")).strip()
        ln = str(r.get("Seller Last Name", "")).strip()
        names.append(f"{fn} {ln}".strip())
    return names

def get_dead_names(crm: pd.DataFrame, owner_name: str) -> list:
    """Dead leads for a given owner."""
    mask = (
        (crm["Action Event"] == "dead") &
        crm["Owner"].str.contains(owner_name.split()[0], case=False, na=False)
    )
    rows = crm[mask]
    names = []
    for _, r in rows.iterrows():
        fn = str(r.get("Seller First Name", "")).strip()
        ln = str(r.get("Seller Last Name", "")).strip()
        reason = str(r.get("Dead Reason", "")).strip()
        names.append(f"{fn} {ln} — {reason}" if reason and reason != "nan" else f"{fn} {ln}")
    return names

def build_embeds(sc_data: dict, new_leads: int, new_lead_names: list,
                 crm: pd.DataFrame, date_label: str) -> list[discord.Embed]:
    """Build the list of Discord embeds for the KPI report."""
    embeds = []

    # ── Main summary embed ────────────────────────────────────────────────
    summary = discord.Embed(
        title=f"📊 Weekly KPI Report — {date_label}",
        color=0x2ECC71
    )
    summary.add_field(name="🏠 New Leads This Week", value=str(new_leads), inline=False)
    summary.add_field(name="\u200b", value="─" * 40, inline=False)

    # Joy
    joy = sc_data.get("Joy Zika", {})
    summary.add_field(
        name="📞 Joy Zika — Lead Manager",
        value=(
            f"Outbound calls: **{joy.get('outbound_calls', 0)}**\n"
            f"Contacted: **{joy.get('contacts', 0)}**\n"
            f"Appointments set: **{joy.get('appointments', 0)}**\n"
            f"Dead opportunities: **{joy.get('dead', 0)}**"
        ),
        inline=False
    )
    summary.add_field(name="\u200b", value="─" * 40, inline=False)

    # Carlos
    carlos = sc_data.get("Carlos Oliveira", {})
    summary.add_field(
        name="🏠 Carlos Oliveira",
        value=(
            f"Outbound calls: **{carlos.get('outbound_calls', 0)}**\n"
            f"Contacted: **{carlos.get('contacts', 0)}**\n"
            f"Appointments set: **{carlos.get('appointments', 0)}**\n"
            f"Verbal offers made: **{carlos.get('verbal_offers', 0)}**\n"
            f"Contracts accepted: **{carlos.get('contracts_accepted', 0)}**\n"
            f"Dead opportunities: **{carlos.get('dead', 0)}**"
        ),
        inline=False
    )
    summary.add_field(name="\u200b", value="─" * 40, inline=False)

    # Trevor
    trevor = sc_data.get("Trevor Anderson", {})
    summary.add_field(
        name="⚡ Trevor Anderson",
        value=(
            f"Verbal offers on new leads: **{trevor.get('verbal_offers', 0)}**\n"
            f"Contracts accepted: **{trevor.get('contracts_accepted', 0)}**\n"
            f"Dead opportunities: **{trevor.get('dead', 0)}**"
        ),
        inline=False
    )

    summary.set_footer(text="Full lead log in thread below ↓")
    embeds.append(summary)
    return embeds

def build_lead_log(new_lead_names, crm, sc_data) -> str:
    """Build a plain text lead log for the thread."""
    lines = []

    # New leads
    lines.append("**🏠 New Leads This Week**")
    if new_lead_names:
        for l in new_lead_names:
            tag = "📘 FB" if "Facebook" in l["campaign"] else "🔍 PPC" if "Ignite" in l["campaign"] else "—"
            lines.append(f"  {tag} {l['name']}")
    else:
        lines.append("  None")

    # Per-person lead logs
    for name, emoji, show_offers in [
        ("Joy Zika", "📞", False),
        ("Carlos Oliveira", "🏠", True),
        ("Trevor Anderson", "⚡", True),
    ]:
        lines.append(f"\n**{emoji} {name}**")

        appts = get_appointment_names(crm, name)
        lines.append(f"  Appointments ({len(appts)}):")
        for a in appts:
            lines.append(f"    • {a}")
        if not appts:
            lines.append("    none")

        if show_offers:
            offers = get_offer_names(crm, name)
            lines.append(f"  Offers delivered ({len(offers)}):")
            for o in offers:
                lines.append(f"    • {o}")
            if not offers:
                lines.append("    none")

            contracts = get_contract_names(crm, name)
            lines.append(f"  Contracts ({len(contracts)}):")
            for c in contracts:
                lines.append(f"    • {c}")
            if not contracts:
                lines.append("    none")

        dead = get_dead_names(crm, name)
        lines.append(f"  Dead ({len(dead)}):")
        for d in dead[:10]:  # cap at 10 to avoid Discord limit
            lines.append(f"    • {d}")
        if not dead:
            lines.append("    none")
        elif len(dead) > 10:
            lines.append(f"    ... and {len(dead)-10} more")

    return "\n".join(lines)

# ── Slash command ─────────────────────────────────────────────────────────────
@tree.command(
    name="kpireport",
    description="Post the weekly KPI report. Attach scorecard xlsx and CRM xlsx.",
    guild=discord.Object(id=1476808200316653588)
)
@app_commands.describe(
    scorecard="InvestorFuse scorecard export (.xlsx)",
    crm_export="InvestorFuse CRM leads export (.xlsx)",
    date_range="Date range label e.g. 'June 2–8, 2026'",
    week_start="Week start date YYYY-MM-DD (default: last Monday)",
    week_end="Week end date YYYY-MM-DD (default: last Sunday)"
)
async def kpi_report(
    interaction: discord.Interaction,
    scorecard: discord.Attachment,
    crm_export: discord.Attachment,
    date_range: str,
    week_start: str = None,
    week_end: str = None
):
    await interaction.response.defer(ephemeral=True)

    try:
        # Parse date range
        if week_start and week_end:
            start_dt = datetime.strptime(week_start, "%Y-%m-%d")
            end_dt   = datetime.strptime(week_end,   "%Y-%m-%d").replace(hour=23, minute=59, second=59)
        else:
            today    = datetime.utcnow().date()
            last_sun = today - timedelta(days=today.weekday() + 1)
            last_mon = last_sun - timedelta(days=6)
            start_dt = datetime.combine(last_mon, datetime.min.time())
            end_dt   = datetime.combine(last_sun, datetime.max.time())

        # Download and parse files
        sc_bytes  = await scorecard.read()
        crm_bytes = await crm_export.read()
        sc_df     = pd.read_excel(io.BytesIO(sc_bytes))
        crm_df    = pd.read_excel(io.BytesIO(crm_bytes))

        # Parse data
        sc_data        = parse_scorecard(sc_df)
        new_leads      = count_new_leads(crm_df, start_dt, end_dt)
        new_lead_names = get_new_lead_names(crm_df, start_dt, end_dt)

        # Build embeds
        embeds = build_embeds(sc_data, new_leads, new_lead_names, crm_df, date_range)

        # Post to #kpi channel
        channel = client.get_channel(KPI_CHANNEL_ID)
        msg = await channel.send(embeds=embeds)

        # Create thread with lead log
        thread = await msg.create_thread(name=f"Lead Log — {date_range}")
        log_text = build_lead_log(new_lead_names, crm_df, sc_data)

        # Split log into 2000-char chunks (Discord limit)
        chunks = [log_text[i:i+1900] for i in range(0, len(log_text), 1900)]
        for chunk in chunks:
            await thread.send(chunk)

        await interaction.followup.send("✅ KPI report posted to #kpi!", ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
        raise

# ── On ready ─────────────────────────────────────────────────────────────────
@client.event
async def on_ready():
    # Register slash commands to your specific guild for instant sync
    guild = discord.Object(id=1476808200316653588)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    print(f"KPI Bot ready as {client.user}")

client.run(DISCORD_TOKEN)
