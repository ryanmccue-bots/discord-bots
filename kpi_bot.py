import discord
from discord import app_commands
import pandas as pd
import os
import io
from datetime import datetime, timedelta
import re

# ── Config ──────────────────────────────────────────────────────────────────
DISCORD_TOKEN    = os.environ["KPI_BOT_TOKEN"]
APPLICATION_ID   = 1514008129472303115
KPI_CHANNEL_ID   = 1513652941515653441

# ── Bot setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)

# ── Owner email mapping ───────────────────────────────────────────────────────
OWNER_EMAILS = {
    "Joy Zika":       "joy@favoritehomebuyer.com",
    "Carlos Oliveira":"carlos@favoritehomebuyer.com",
    "Trevor Anderson":"tdarealestate@gmail.com",
}

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
    crm["date_created"] = pd.to_datetime(crm["Date Created"], errors="coerce")
    mask = (crm["date_created"] >= start) & (crm["date_created"] <= end)
    return int(mask.sum())

def get_new_lead_names(crm: pd.DataFrame, start: datetime, end: datetime) -> list:
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
    email = OWNER_EMAILS.get(owner_name, "")
    mask = (
        crm["Action Event"].str.contains("seller-appointment", case=False, na=False) &
        (crm["Owner"] == email)
    )
    rows = crm[mask]
    names = []
    for _, r in rows.iterrows():
        fn = str(r.get("Seller First Name", "")).strip()
        ln = str(r.get("Seller Last Name", "")).strip()
        names.append(f"{fn} {ln}".strip())
    return names

def get_offer_names(crm: pd.DataFrame, owner_name: str) -> list:
    email = OWNER_EMAILS.get(owner_name, "")
    mask = (
        crm["Date of 1st Offer"].notna() &
        (crm["Owner"] == email)
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
    email = OWNER_EMAILS.get(owner_name, "")
    mask = (
        (crm["Pipeline"] == "transaction") &
        (crm["Owner"] == email)
    )
    rows = crm[mask]
    names = []
    for _, r in rows.iterrows():
        fn = str(r.get("Seller First Name", "")).strip()
        ln = str(r.get("Seller Last Name", "")).strip()
        names.append(f"{fn} {ln}".strip())
    return names

def get_dead_names(crm: pd.DataFrame, owner_name: str) -> list:
    email = OWNER_EMAILS.get(owner_name, "")
    mask = (
        (crm["Action Event"] == "dead") &
        (crm["Owner"] == email)
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
            f"Appointments set: **{joy.get('appointments', 0)}**"
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
            f"Outbound calls: **{trevor.get('outbound_calls', 0)}**\n"
            f"Contacted: **{trevor.get('contacts', 0)}**\n"
            f"Verbal offers made: **{trevor.get('verbal_offers', 0)}**\n"
            f"Contracts accepted: **{trevor.get('contracts_accepted', 0)}**\n"
            f"Dead opportunities: **{trevor.get('dead', 0)}**"
        ),
        inline=False
    )

    summary.add_field(
        name="📄 Full Report",
        value="See attached HTML file — download and open in browser for full lead log.",
        inline=False
    )

    summary.set_footer(text=f"FHB Pipeline · {date_label}")
    embeds.append(summary)
    return embeds

def generate_html_report(sc_data: dict, new_leads: int, new_lead_names: list,
                          crm: pd.DataFrame, date_label: str) -> str:
    """Generate a full HTML pipeline report."""

    def lead_rows(names, badge_class="badge-fb"):
        if not names:
            return "<tr><td colspan='2' class='none'>None</td></tr>"
        rows = ""
        for n in names:
            if isinstance(n, dict):
                tag = "FB" if "Facebook" in n["campaign"] else "PPC" if "Ignite" in n["campaign"] else "—"
                cls = "badge-fb" if tag == "FB" else "badge-ppc"
                rows += f"<tr><td><span class='badge {cls}'>{tag}</span> {n['name']}</td></tr>"
            else:
                rows += f"<tr><td>{n}</td></tr>"
        return rows

    def section(emoji, name, rows_html):
        return f"""
        <div class='section'>
          <div class='section-header'>{emoji} {name}</div>
          <table>{rows_html}</table>
        </div>"""

    joy    = sc_data.get("Joy Zika", {})
    carlos = sc_data.get("Carlos Oliveira", {})
    trevor = sc_data.get("Trevor Anderson", {})

    appts_joy     = get_appointment_names(crm, "Joy Zika")
    appts_carlos  = get_appointment_names(crm, "Carlos Oliveira")
    offers_carlos = get_offer_names(crm, "Carlos Oliveira")
    offers_trevor = get_offer_names(crm, "Trevor Anderson")
    contracts_carlos = get_contract_names(crm, "Carlos Oliveira")
    contracts_trevor = get_contract_names(crm, "Trevor Anderson")
    dead_joy     = get_dead_names(crm, "Joy Zika")
    dead_carlos  = get_dead_names(crm, "Carlos Oliveira")
    dead_trevor  = get_dead_names(crm, "Trevor Anderson")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>FHB KPI Report — {date_label}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Inter', sans-serif; background: #f5f4f0; color: #1a1a18; padding: 2rem 1rem; }}
  .report {{ max-width: 780px; margin: 0 auto; background: #fff; border-radius: 16px; border: 0.5px solid #dddbd3; padding: 2rem; }}
  h1 {{ font-size: 22px; font-weight: 600; color: #1a1a18; margin-bottom: 4px; }}
  .sub {{ font-size: 13px; color: #76756e; }}
  .period {{ display: inline-block; font-size: 12px; background: #f1efe8; border: 0.5px solid #d3d1c7; border-radius: 20px; padding: 3px 14px; color: #76756e; margin-top: 8px; }}
  .header {{ text-align: center; margin-bottom: 2rem; }}
  .new-leads {{ text-align: center; font-size: 48px; font-weight: 600; color: #1a1a18; margin: 1rem 0 0.25rem; }}
  .new-leads-label {{ text-align: center; font-size: 13px; color: #76756e; margin-bottom: 1.5rem; }}
  .divider {{ border-top: 0.5px solid #dddbd3; margin: 1.5rem 0; }}
  .section {{ margin-bottom: 1.5rem; }}
  .section-header {{ font-size: 14px; font-weight: 600; color: #1a1a18; margin-bottom: 10px; padding-bottom: 6px; border-bottom: 0.5px solid #dddbd3; }}
  .kpi-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 12px; }}
  .kpi-card {{ background: #f8f7f3; border-radius: 10px; padding: 12px 14px; }}
  .kpi-val {{ font-size: 28px; font-weight: 600; color: #1a1a18; line-height: 1; }}
  .kpi-label {{ font-size: 10px; font-weight: 600; color: #9e9d96; text-transform: uppercase; letter-spacing: 0.06em; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 8px; }}
  th {{ text-align: left; font-size: 10px; font-weight: 600; color: #9e9d96; text-transform: uppercase; letter-spacing: 0.06em; padding: 0 0 6px; }}
  td {{ padding: 5px 0; border-top: 0.5px solid #f1efe8; color: #444441; }}
  td.none {{ color: #9e9d96; font-style: italic; }}
  .badge {{ display: inline-block; font-size: 9px; font-weight: 600; padding: 1px 6px; border-radius: 6px; }}
  .badge-fb {{ background:#E6F1FB; color:#0C447C; }}
  .badge-ppc {{ background:#FAEEDA; color:#633806; }}
  .sub-label {{ font-size: 11px; font-weight: 600; color: #76756e; margin: 10px 0 4px; }}
</style>
</head>
<body>
<div class="report">
  <div class="header">
    <h1>FHB Weekly KPI Report</h1>
    <div class="sub">Favorite Home Buyer</div>
    <div class="period">{date_label}</div>
  </div>

  <div class="new-leads">{new_leads}</div>
  <div class="new-leads-label">New leads this week</div>

  <table>
    <thead><tr><th>Name</th><th>Campaign</th></tr></thead>
    <tbody>{lead_rows(new_lead_names)}</tbody>
  </table>

  <div class="divider"></div>

  <!-- Joy -->
  <div class="section">
    <div class="section-header">📞 Joy Zika — Lead Manager</div>
    <div class="kpi-grid">
      <div class="kpi-card"><div class="kpi-val">{joy.get('outbound_calls',0)}</div><div class="kpi-label">Outbound Calls</div></div>
      <div class="kpi-card"><div class="kpi-val">{joy.get('contacts',0)}</div><div class="kpi-label">Contacted</div></div>
      <div class="kpi-card"><div class="kpi-val">{joy.get('appointments',0)}</div><div class="kpi-label">Appointments</div></div>
    </div>
    <div class="sub-label">Appointments</div>
    <table><tbody>{lead_rows(appts_joy)}</tbody></table>
  </div>

  <div class="divider"></div>

  <!-- Carlos -->
  <div class="section">
    <div class="section-header">🏠 Carlos Oliveira</div>
    <div class="kpi-grid">
      <div class="kpi-card"><div class="kpi-val">{carlos.get('outbound_calls',0)}</div><div class="kpi-label">Outbound Calls</div></div>
      <div class="kpi-card"><div class="kpi-val">{carlos.get('contacts',0)}</div><div class="kpi-label">Contacted</div></div>
      <div class="kpi-card"><div class="kpi-val">{carlos.get('appointments',0)}</div><div class="kpi-label">Appointments</div></div>
      <div class="kpi-card"><div class="kpi-val">{carlos.get('verbal_offers',0)}</div><div class="kpi-label">Verbal Offers Made</div></div>
      <div class="kpi-card"><div class="kpi-val">{carlos.get('contracts_accepted',0)}</div><div class="kpi-label">Contracts</div></div>
      <div class="kpi-card"><div class="kpi-val">{carlos.get('dead',0)}</div><div class="kpi-label">Dead</div></div>
    </div>
    <div class="sub-label">Appointments</div>
    <table><tbody>{lead_rows(appts_carlos)}</tbody></table>
    <div class="sub-label">Offers Delivered ({len(offers_carlos)})</div>
    <table><tbody>{lead_rows(offers_carlos)}</tbody></table>
    <div class="sub-label">Contracts ({len(contracts_carlos)})</div>
    <table><tbody>{lead_rows(contracts_carlos)}</tbody></table>
    <div class="sub-label">Dead ({len(dead_carlos)})</div>
    <table><tbody>{lead_rows(dead_carlos)}</tbody></table>
  </div>

  <div class="divider"></div>

  <!-- Trevor -->
  <div class="section">
    <div class="section-header">⚡ Trevor Anderson</div>
    <div class="kpi-grid">
      <div class="kpi-card"><div class="kpi-val">{trevor.get('outbound_calls',0)}</div><div class="kpi-label">Outbound Calls</div></div>
      <div class="kpi-card"><div class="kpi-val">{trevor.get('contacts',0)}</div><div class="kpi-label">Contacted</div></div>
      <div class="kpi-card"><div class="kpi-val">{trevor.get('verbal_offers',0)}</div><div class="kpi-label">Verbal Offers Made</div></div>
      <div class="kpi-card"><div class="kpi-val">{trevor.get('contracts_accepted',0)}</div><div class="kpi-label">Contracts</div></div>
      <div class="kpi-card"><div class="kpi-val">{trevor.get('dead',0)}</div><div class="kpi-label">Dead</div></div>
    </div>
    <div class="sub-label">Offers Delivered ({len(offers_trevor)})</div>
    <table><tbody>{lead_rows(offers_trevor)}</tbody></table>
    <div class="sub-label">Contracts ({len(contracts_trevor)})</div>
    <table><tbody>{lead_rows(contracts_trevor)}</tbody></table>
    <div class="sub-label">Dead ({len(dead_trevor)})</div>
    <table><tbody>{lead_rows(dead_trevor)}</tbody></table>
  </div>

</div>
</body>
</html>"""


# ── Slash command ─────────────────────────────────────────────────────────────
@tree.command(
    name="kpireport",
    description="Post the weekly KPI report. Attach scorecard xlsx and CRM xlsx.",
    guild=discord.Object(id=1476808200316653588)
)
@app_commands.describe(
    scorecard="InvestorFuse scorecard export (.xlsx)",
    crm_export="InvestorFuse CRM leads export (.xlsx)",
)
async def kpi_report(
    interaction: discord.Interaction,
    scorecard: discord.Attachment,
    crm_export: discord.Attachment,
):
    await interaction.response.defer(ephemeral=True)

    try:
        # Parse date range from scorecard filename
        # Expected format: investorfuse-scorecard-custom-YYYY-MM-DD-to-YYYY-MM-DD.xlsx
        fname = scorecard.filename
        match = re.search(r'(\d{4}-\d{2}-\d{2})-to-(\d{4}-\d{2}-\d{2})', fname)
        if match:
            start_dt = datetime.strptime(match.group(1), "%Y-%m-%d")
            end_dt   = datetime.strptime(match.group(2), "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            # Format nicely e.g. "June 1–7, 2026"
            s = start_dt.strftime("%B %-d")
            e = end_dt.strftime("%-d, %Y")
            date_range = f"{s}–{e}"
        else:
            # Fallback: last Monday to Sunday
            today    = datetime.utcnow().date()
            last_sun = today - timedelta(days=today.weekday() + 1)
            last_mon = last_sun - timedelta(days=6)
            start_dt = datetime.combine(last_mon, datetime.min.time())
            end_dt   = datetime.combine(last_sun, datetime.max.time())
            date_range = f"{start_dt.strftime('%B %-d')}–{end_dt.strftime('%-d, %Y')}"

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

        # Generate HTML report
        html = generate_html_report(sc_data, new_leads, new_lead_names, crm_df, date_range)
        html_file = discord.File(
            fp=io.BytesIO(html.encode("utf-8")),
            filename=f"FHB_KPI_Report_{date_range.replace(' ', '_').replace(',', '').replace('–','-')}.html"
        )

        # Post to #kpi channel with HTML attached
        channel = client.get_channel(KPI_CHANNEL_ID)
        await channel.send(embeds=embeds, file=html_file)

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
