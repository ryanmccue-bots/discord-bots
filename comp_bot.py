"""
FHB Comp Bot v3
- Tickety creates channel → bot posts condition survey with buttons
- Rep fills in ROOF / HVAC / CONDITION
- Bot runs comp analysis and posts short offer card in channel
- Full comp detail posted in a thread for Alec to review
"""

import discord
from discord.ext import commands
import anthropic
import asyncio
import re
import os
import urllib.request
import urllib.parse
import json as json_lib
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
DISCORD_TOKEN     = os.environ.get("DISCORD_TOKEN", "YOUR_DISCORD_TOKEN_HERE")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "YOUR_ANTHROPIC_API_KEY_HERE")
RENTCAST_API_KEY  = os.environ.get("RENTCAST_API_KEY", "")

TICKETY_BOT_ID    = 718493970652594217  # Tickety's Discord user ID
WATCH_CATEGORIES  = []         # e.g. ["Leads"] — leave empty for all categories
CASH_FEE          = 15_000     # Wholesale fee for cash offers
NOVATION_FEE      = 0          # No fee deducted on novation (seller pays at close)

# ── Bot Setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# In-memory store: channel_id → {address, roof, hvac, condition, survey_message_id}
channel_state: dict[int, dict] = {}


# ── Tickety Parsing ───────────────────────────────────────────────────────────

def is_tickety_message(message: discord.Message) -> bool:
    if TICKETY_BOT_ID:
        return message.author.id == TICKETY_BOT_ID
    # Fall back to name matching — never match on author.bot alone as that catches all bots
    name = message.author.name.lower()
    return "tickety" in name


def extract_field_lines(content: str, label_pattern: str, max_lines: int = 2) -> str | None:
    pattern = rf"(?:{label_pattern})[^\n]*\n(.*?)(?=\n\s*\n\d+\.|\n\d+\.|\Z)"
    match = re.search(pattern, content, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    raw_block = match.group(1).strip()
    lines = [ln.strip() for ln in raw_block.split("\n") if ln.strip()][:max_lines]
    return " ".join(lines) if lines else None


_US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC",
    "alabama","alaska","arizona","arkansas","california","colorado","connecticut",
    "delaware","florida","georgia","hawaii","idaho","illinois","indiana","iowa",
    "kansas","kentucky","louisiana","maine","maryland","massachusetts","michigan",
    "minnesota","mississippi","missouri","montana","nebraska","nevada",
    "new hampshire","new jersey","new mexico","new york","north carolina",
    "north dakota","ohio","oklahoma","oregon","pennsylvania","rhode island",
    "south carolina","south dakota","tennessee","texas","utah","vermont",
    "virginia","washington","west virginia","wisconsin","wyoming",
}

def validate_address(address: str) -> tuple[bool, str]:
    if not address or len(address.strip()) < 8:
        return False, "too short"
    addr = address.strip()
    if not re.match(r"^\d{1,6}[-\d]*\s", addr):
        return False, "no street number found"
    words = re.findall(r"[a-zA-Z]{2,}", addr)
    if not words:
        return False, "no street name found"
    tokens = {t.strip(",.") for t in re.split(r"[\s,]+", addr)}
    has_state = (
        any(t.upper() in _US_STATES for t in tokens)
        or any(s in addr.lower() for s in _US_STATES if len(s) > 3)
    )
    if not has_state:
        return True, "no state detected"
    return True, ""


def parse_tickety_message(content: str) -> dict:
    data: dict = {}
    raw_address = extract_field_lines(
        content,
        label_pattern=r"street address of the property|address of the property|property address|address",
        max_lines=2,
    )
    if raw_address:
        normalized = re.sub(r"\s+", " ", raw_address).strip()
        data["address"] = normalized
        valid, warning = validate_address(normalized)
        data["address_valid"] = valid
        data["address_warning"] = warning
    else:
        data["address"] = None
        data["address_valid"] = False
        data["address_warning"] = "address field not found"
    return data


def parse_tickety_embed(embed: discord.Embed) -> dict:
    """Parse lead data from a Tickety embed's fields or description."""
    data: dict = {}
    text_parts = []

    # Collect all text from the embed
    if embed.description:
        text_parts.append(embed.description)
    for field in embed.fields:
        text_parts.append(f"{field.name}\n{field.value}")
    if embed.footer and embed.footer.text:
        text_parts.append(embed.footer.text)

    combined = "\n".join(text_parts)

    # Try to extract address
    raw_address = extract_field_lines(
        combined,
        label_pattern=r"street address of the property|address of the property|property address|address",
        max_lines=2,
    )
    if raw_address:
        normalized = re.sub(r"\s+", " ", raw_address).strip()
        data["address"] = normalized
        valid, warning = validate_address(normalized)
        data["address_valid"] = valid
        data["address_warning"] = warning

    return data


async def extract_lead_data(channel: discord.TextChannel) -> dict | None:
    async def try_message(msg: discord.Message) -> dict | None:
        if not is_tickety_message(msg):
            return None
        # Try plain text content first
        if msg.content:
            data = parse_tickety_message(msg.content)
            if data.get("address"):
                return data
        # Fall back to embeds
        for embed in msg.embeds:
            data = parse_tickety_embed(embed)
            if data.get("address"):
                return data
        return None

    try:
        async for msg in channel.pins():
            result = await try_message(msg)
            if result:
                return result
    except Exception:
        pass
    try:
        async for msg in channel.history(limit=10, oldest_first=True):
            result = await try_message(msg)
            if result:
                return result
    except Exception:
        pass
    return None


def is_watched_channel(channel: discord.TextChannel) -> bool:
    if WATCH_CATEGORIES:
        if not channel.category:
            return False
        if channel.category.name.lower() not in [c.lower() for c in WATCH_CATEGORIES]:
            return False
    return True


# ── Survey UI ─────────────────────────────────────────────────────────────────

# ── Repair Cost Modal ──────────────────────────────────────────────────────────

class RepairCostModal(discord.ui.Modal, title="Estimated Repair Cost"):
    repair_cost = discord.ui.TextInput(
        label="Repair Cost ($)",
        placeholder="e.g. 25000",
        required=True,
        max_length=12,
    )

    def __init__(self, channel_id: int, offer_type: str):
        super().__init__()
        self.channel_id = channel_id
        self.offer_type = offer_type

    async def on_submit(self, interaction: discord.Interaction):
        # Parse repair cost — strip $, commas, spaces
        raw = self.repair_cost.value.strip().replace("$", "").replace(",", "").replace(" ", "")
        try:
            repairs = int(float(raw))
        except ValueError:
            await interaction.response.send_message(
                f"⚠️ Couldn't parse `{self.repair_cost.value}` as a number. Please try again.",
                ephemeral=True
            )
            return

        state = channel_state.get(self.channel_id, {})
        if state.get("fired"):
            await interaction.response.send_message(
                "⚠️ Comp already running — no need to resubmit.",
                ephemeral=True
            )
            return

        channel_state[self.channel_id]["repairs"] = repairs
        channel_state[self.channel_id]["offer_type"] = self.offer_type
        channel_state[self.channel_id]["fired"] = True

        fmt_repairs = f"${repairs:,}"
        offer_label = {"cash": "Cash only", "novation": "Novation only", "both": "Cash + Novation"}[self.offer_type]

        await interaction.response.edit_message(
            content=(
                f"💰 **Offer type:** {offer_label}\n"
                f"🔨 **Repairs:** {fmt_repairs}\n\n"
                f"✅ Running comp analysis..."
            ),
            view=None
        )
        asyncio.create_task(run_and_post_offers(interaction.channel))


# ── Offer Type Selector ────────────────────────────────────────────────────────

class OfferTypeView(discord.ui.View):
    """Step 1: Rep picks which offer types to generate, then gets repair modal."""

    def __init__(self, channel_id: int):
        super().__init__(timeout=3600)
        self.channel_id = channel_id

    async def _launch_modal(self, interaction: discord.Interaction, offer_type: str):
        state = channel_state.get(self.channel_id, {})
        if not state.get("address"):
            await interaction.response.send_message(
                "⚠️ No address found yet — use `/comp [full address]` instead.",
                ephemeral=True
            )
            return
        await interaction.response.send_modal(RepairCostModal(self.channel_id, offer_type))

    @discord.ui.button(label="💰 Cash Offer", style=discord.ButtonStyle.primary)
    async def cash(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._launch_modal(interaction, "cash")

    @discord.ui.button(label="📋 Novation Offer", style=discord.ButtonStyle.primary)
    async def novation(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._launch_modal(interaction, "novation")

    @discord.ui.button(label="✨ Both", style=discord.ButtonStyle.success)
    async def both(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._launch_modal(interaction, "both")



COMP_SYSTEM_PROMPT = """You are an elite real estate comping analyst for a real estate wholesaling company.
You have deep knowledge of ARV calculation, repair estimation, and MAO formulas.
You are conservative and data-driven.

CRITICAL OUTPUT RULES:
1. Your response must start INSTANTLY with the # title header. Zero text before it.
2. Every blockquote (>) line max 100 characters. Cut ruthlessly.
3. Every flag (🔴🟡🟢) on ONE line, max 100 chars, emoji and text together.
4. COMPS section: header line + code block + link per comp. Zero prose between comps.
5. Adjustments and Active listings in code blocks.
6. No Weighted ARV tables. No Methodology Notes. No pipe tables.
7. When in doubt, be shorter.
8. DO NOT include any MAO calculation, MAO calculator, or offer price in your report. The offer is calculated separately by the system. Only provide ARV, repairs, market data, comps, and flags."""


def condition_to_score(roof: str, hvac: str, condition: str) -> tuple[int, str]:
    """Convert survey answers to a condition score and repair tier."""
    score = 5  # baseline

    if roof == "New":
        score += 1
    elif roof == "Needs Replacing":
        score -= 1

    if hvac == "New":
        score += 1
    elif hvac == "Needs Replacing":
        score -= 1

    if condition == "Needs Little Work":
        score += 1
        tier = "light"
    elif condition == "Needs Some Work":
        tier = "medium"
    elif condition == "Needs Full Rehab":
        score -= 2
        tier = "heavy"
    else:
        tier = "medium"

    score = max(1, min(10, score))
    return score, tier


def repair_range(score: int, sqft_estimate: int = 1200) -> tuple[int, int, int]:
    """Return low/mid/high repair estimates based on condition score."""
    if score >= 8:
        per_sqft = (5, 12)
    elif score >= 6:
        per_sqft = (15, 25)
    elif score >= 4:
        per_sqft = (25, 40)
    elif score >= 2:
        per_sqft = (40, 60)
    else:
        per_sqft = (60, 100)

    low = int(sqft_estimate * per_sqft[0])
    high = int(sqft_estimate * per_sqft[1])
    mid = int((low + high) / 2)
    return low, mid, high


def novation_eligible(roof: str, hvac: str, condition: str) -> bool:
    """Novation only if roof+hvac are New/Good AND condition is not full rehab."""
    roof_ok = roof in ("New", "Good")
    hvac_ok = hvac in ("New", "Good")
    cond_ok = condition != "Needs Full Rehab"
    return roof_ok and hvac_ok and cond_ok


# ── Rentcast API ──────────────────────────────────────────────────────────────

def rentcast_value_estimate(address: str, comp_count: int = 10) -> dict | None:
    """
    Call Rentcast Value Estimate endpoint.
    Returns the full response dict, or None on failure.
    """
    if not RENTCAST_API_KEY:
        return None
    params = urllib.parse.urlencode({
        "address": address,
        "compCount": comp_count,
        "lookupSubjectAttributes": "true",
    })
    url = f"https://api.rentcast.io/v1/avm/value?{params}"
    req = urllib.request.Request(url, headers={"X-Api-Key": RENTCAST_API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json_lib.loads(resp.read().decode())
    except Exception as e:
        print(f"Rentcast API error: {e}")
        return None


def format_rentcast_data(data: dict) -> str:
    """
    Convert Rentcast API response into a structured text block
    for Claude to analyze — no web searching needed.
    """
    if not data:
        return "No Rentcast data available."

    lines = []

    # Subject property
    subj = data.get("subjectProperty", {})
    if subj:
        lines.append("## SUBJECT PROPERTY (from Rentcast)")
        lines.append(f"Address: {subj.get('formattedAddress', 'Unknown')}")
        lines.append(f"Beds: {subj.get('bedrooms', 'Unknown')} | Baths: {subj.get('bathrooms', 'Unknown')} | Sqft: {subj.get('squareFootage', 'Unknown')}")
        lines.append(f"Year Built: {subj.get('yearBuilt', 'Unknown')} | Lot: {subj.get('lotSize', 'Unknown')} sqft")
        lines.append(f"Property Type: {subj.get('propertyType', 'Unknown')}")
        lines.append("")

    # AVM estimate
    price = data.get("price")
    price_low = data.get("priceLow")
    price_high = data.get("priceHigh")
    if price:
        lines.append("## RENTCAST AVM ESTIMATE")
        lines.append(f"Estimated Value: ${price:,}")
        if price_low and price_high:
            lines.append(f"Range: ${price_low:,} – ${price_high:,}")
        lines.append("")

    # Comps
    comps = data.get("comparables", [])
    if comps:
        lines.append(f"## COMPARABLE SALES ({len(comps)} found by Rentcast)")
        for i, comp in enumerate(comps, 1):
            addr = comp.get("formattedAddress", "Unknown")
            price_c = comp.get("price", 0)
            sqft_c = comp.get("squareFootage", 0)
            ppsf = int(price_c / sqft_c) if sqft_c else 0
            beds_c = comp.get("bedrooms", "?")
            baths_c = comp.get("bathrooms", "?")
            year_c = comp.get("yearBuilt", "?")
            status = comp.get("status", "Unknown")
            listed = comp.get("listedDate", "")[:10] if comp.get("listedDate") else ""
            removed = comp.get("removedDate", "")[:10] if comp.get("removedDate") else ""
            dist = comp.get("distance", 0)
            corr = comp.get("correlation", 0)
            lines.append(
                f"Comp {i}: {addr} | ${price_c:,} | {sqft_c} sqft | ${ppsf}/sqft | "
                f"{beds_c}bd/{baths_c}ba | Built {year_c} | {status} | "
                f"Listed: {listed} | Sold: {removed} | {dist:.2f}mi away | "
                f"Rentcast correlation: {corr:.2f}"
            )
        lines.append("")

    return "\n".join(lines)


def build_comp_prompt(address: str, repairs: int, offer_type: str, rentcast_data: str = "") -> str:
    date_str = datetime.now().strftime("%B %d, %Y")
    data_section = rentcast_data if rentcast_data else "No Rentcast data available — use web search as fallback."
    offer_label = {"cash": "Cash only", "novation": "Novation only", "both": "Cash + Novation"}.get(offer_type, "Cash")

    return f"""Perform a complete comp analysis for this wholesale lead.

## SUBJECT PROPERTY
- Address: {address}
- Estimated Repairs: ${repairs:,} (provided by rep)
- Offer Type Requested: {offer_label}

## YOUR TASK
You have been provided with structured property and comp data from the Rentcast API below.
Use this data as your primary source — DO NOT use web search for comps or property details.
You may use web search only for market conditions (DOM, inventory, sale-to-list ratio) if not available in the Rentcast data.

## RENTCAST DATA
{data_section}

Then produce the analysis in this EXACT format:

---

# 🏠 COMP REPORT — {address}
*{date_str} · Confidence: [HIGH / MEDIUM / LOW / VERY LOW]*

---

## 💰 ARV
```
Conservative:  $[X]
Most Likely:   $[X]
Optimistic:    $[X]
Spread:        [X]% → [HIGH/MEDIUM/LOW/VERY LOW] confidence
```
> [One sentence, max 100 chars — comp quality and $/sqft basis]

---

## 🔨 CONDITION & REPAIRS
```
Condition:  {score}/10 — {condition} · Roof: {roof} · HVAC: {hvac}
Low:        $[X]
Mid:        $[X]
High:       $[X]
```
> [One sentence, max 100 chars — what's driving the estimate]

---

## 📊 MARKET
```
Type:          [Buyer/Seller/Neutral]
Avg DOM:       [X] days
Sale-to-List:  [X]%
Inventory:     [X] months
```
> [One sentence, max 100 chars — market implication]

---

## 🏡 COMPS

[If rural extension applies:]
⚠️ Rural Extension — Tier [X] comps · No confirmed sold comps within [X] miles

[For each comp — EXACT pattern, no variations:]
> **[Address]** · Score [X]/100 · Tier [X]
```
Sold: $[price] · [Mon YYYY] · [sqft] sqft · $[X]/sqft
Style: [style] · [beds]bd/[baths]ba · [key feature]
```
> 🔗 [Zillow/Redfin URL — or "Link not found"]

**Adjustments:**
```
• [Feature] · ±$[X] · [reason — max 8 words]
```

**Active listings:**
```
[Address] — $[price] · [X] days · [ARV impact]
```

---

## 🚩 FLAGS

> 🔴 [Critical risk — ONE line, max 100 chars]

> 🟡 [Important note — ONE line, max 100 chars]

> 🟢 [Upside/positive — ONE line, max 100 chars]

---
*⚡ FHB Comp Bot · Always verify before offering · Confidence: [HIGH/MEDIUM/LOW/VERY LOW]*"""


# ── Offer Calculator ──────────────────────────────────────────────────────────

def parse_arv_from_report(report: str) -> int | None:
    """Extract Most Likely ARV from the report text."""
    match = re.search(r"Most Likely:\s+\$([0-9,]+)", report)
    if match:
        return int(match.group(1).replace(",", ""))
    return None


def parse_market_type(report: str) -> str:
    """
    Extract market type from report. Looks for the Type: line inside
    the MARKET code block and normalises to lowercase for matching.
    Falls back to keyword scanning the whole report, then 'neutral'.
    """
    # Primary: match inside market code block
    match = re.search(r"Type:\s+([^\n`]+)", report)
    if match:
        return match.group(1).strip().lower()
    # Secondary: keyword scan
    lower = report.lower()
    if "seller" in lower and ("hot" in lower or "extreme" in lower):
        return "hot seller's"
    if "seller" in lower:
        return "seller's"
    if "buyer" in lower:
        return "buyer's"
    return "neutral"


def cash_investment_pct(market_type: str) -> float:
    """Return cash offer investment % based on market."""
    if "seller" in market_type and ("hot" in market_type or "extreme" in market_type):
        return 0.75
    elif "seller" in market_type:
        return 0.72
    elif "buyer" in market_type:
        return 0.60
    elif "rural" in market_type or "very rural" in market_type:
        return 0.60
    else:
        return 0.68


def novation_investment_pct(market_type: str) -> float:
    """Return novation offer investment % based on market."""
    if "seller" in market_type and ("hot" in market_type or "extreme" in market_type):
        return 0.85
    elif "seller" in market_type:
        return 0.82
    elif "buyer" in market_type:
        return 0.75
    else:
        return 0.78


def build_offer_card(
    address: str,
    arv: int,
    repairs: int,
    market_type: str,
    offer_type: str,
    confidence: str,
    data_source: str = "web search",
) -> str:
    cash_pct = cash_investment_pct(market_type)
    cash_gross = int(arv * cash_pct)
    cash_offer = max(0, cash_gross - repairs - CASH_FEE)

    lines = [
        f"# 🏠 {address}",
        f"*ARV: ${arv:,} · Repairs: ${repairs:,} · Confidence: {confidence}*",
        "",
        "---",
        "",
    ]

    if offer_type in ("cash", "both"):
        lines += [
            f"## 💰 CASH OFFER: **${cash_offer:,}**",
            "```",
            f"ARV:            ${arv:,}",
            f"× {int(cash_pct*100)}%:          ${cash_gross:,}",
            f"− Repairs:      −${repairs:,}",
            f"− Fee:          −${CASH_FEE:,}",
            f"= Cash Offer:   ${cash_offer:,}",
            "```",
            "",
        ]

    if offer_type in ("novation", "both"):
        nov_pct = novation_investment_pct(market_type)
        nov_gross = int(arv * nov_pct)
        nov_offer = max(0, nov_gross - NOVATION_FEE)
        lines += [
            f"## 📋 NOVATION OFFER: **${nov_offer:,}**",
            "```",
            f"ARV:              ${arv:,}",
            f"× {int(nov_pct*100)}%:            ${nov_gross:,}",
            f"(No repair deduction — seller pays at close)",
            f"= Novation Offer: ${nov_offer:,}",
            "```",
            "",
        ]

    lines += ["---", f"*💬 Full comp detail in thread below ↓ · Data: {data_source}*"]
    return "\n".join(lines)


# ── Report Splitter ───────────────────────────────────────────────────────────

def strip_preamble(text: str) -> str:
    for i, line in enumerate(text.split("\n")):
        if line.startswith("# "):
            return "\n".join(text.split("\n")[i:])
    return text


def split_report(report: str, max_len: int = 1900) -> list[str]:
    """
    Split report into Discord-safe chunks with three hard rules:
    1. Never split inside a code block.
    2. Never split between a closing ``` and the > 🔗 link line after it.
    3. COMPS and FLAGS sections always start a new message.
    """
    FORCED_BREAKS = {"## 🏡 COMPS", "## 🚩 FLAGS"}

    chunks = []
    current = ""
    in_code_block = False
    just_closed_code = False

    for line in report.split("\n"):
        stripped = line.strip()

        if stripped.startswith("```"):
            if in_code_block:
                in_code_block = False
                just_closed_code = True
            else:
                in_code_block = True
                just_closed_code = False
        else:
            just_closed_code = False

        is_forced = not in_code_block and any(
            stripped.startswith(fb) for fb in FORCED_BREAKS
        )
        is_link_line = stripped.startswith("> 🔗")
        over_limit = len(current + line + "\n") > max_len

        if is_forced:
            if current.strip():
                chunks.append(current.strip())
            current = line + "\n"
        elif over_limit and not in_code_block and not just_closed_code and not is_link_line:
            if current.strip():
                chunks.append(current.strip())
            current = line + "\n"
        else:
            current += line + "\n"

    if current.strip():
        chunks.append(current.strip())

    safe = []
    for chunk in chunks:
        while len(chunk) > max_len:
            safe.append(chunk[:max_len])
            chunk = chunk[max_len:]
        if chunk.strip():
            safe.append(chunk)

    return safe or [report[:max_len]]


# ── Core Analysis Runner ──────────────────────────────────────────────────────

def run_comp_analysis(address: str, prompt: str, use_web: bool = True) -> str:
    try:
        tools = [{"type": "web_search_20250305", "name": "web_search"}] if use_web else []
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            system=[{"type": "text", "text": COMP_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": [{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}]}],
        )
        parts = [block.text for block in response.content if hasattr(block, "text")]
        raw = "\n".join(parts).strip()
        return strip_preamble(raw) or "⚠️ No analysis generated."
    except Exception as e:
        return f"⚠️ Analysis error: {e}"


async def run_and_post_offers(channel: discord.TextChannel):
    """Run comp analysis and post offer card + thread with full detail."""
    state = channel_state.get(channel.id)
    if not state:
        return

    address    = state["address"]
    repairs    = state["repairs"]        # Rep-provided repair cost
    offer_type = state["offer_type"]     # "cash", "novation", or "both"

    # Fetch Rentcast data first (15s timeout)
    loop = asyncio.get_running_loop()
    try:
        rentcast_raw = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: rentcast_value_estimate(address, comp_count=15)),
            timeout=15.0
        )
    except asyncio.TimeoutError:
        rentcast_raw = None
        print(f"Rentcast timed out for {address}")

    if rentcast_raw:
        rentcast_str = format_rentcast_data(rentcast_raw)
        data_source = "📡 Rentcast API"
    else:
        rentcast_str = ""
        data_source = "🌐 web search (Rentcast unavailable)"

    await channel.send(f"🔍 Running comp analysis via {data_source}...")

    prompt = build_comp_prompt(address, repairs, offer_type, rentcast_str)

    # Run Claude analysis (120s timeout)
    try:
        report = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: run_comp_analysis(address, prompt, use_web=not bool(rentcast_raw))),
            timeout=120.0
        )
    except asyncio.TimeoutError:
        await channel.send("⚠️ Comp analysis timed out. Please try `/recomp` again.")
        return

    # Parse ARV and market from report
    arv = parse_arv_from_report(report)
    market_type = parse_market_type(report)

    # Parse confidence
    conf_match = re.search(r"Confidence:\s*(HIGH|MEDIUM|LOW|VERY LOW)", report)
    confidence = conf_match.group(1) if conf_match else "LOW"

    if arv:
        offer_card = build_offer_card(
            address, arv, repairs, market_type,
            offer_type, confidence, data_source
        )
        offer_msg = await channel.send(offer_card)
        try:
            await offer_msg.pin()
        except Exception:
            pass
    else:
        offer_msg = await channel.send(
            "⚠️ **Could not parse ARV** — see thread for full comp detail. Calculate offer manually."
        )

    # Always create thread with full comp detail
    try:
        thread = await offer_msg.create_thread(
            name=f"Comp Detail — {address[:40]}",
            auto_archive_duration=1440
        )
        await thread.send(f"*Data source: {data_source}*")
        chunks = split_report(report)
        for chunk in chunks:
            await thread.send(chunk)
            await asyncio.sleep(0.4)
    except Exception as e:
        await channel.send(f"⚠️ Thread creation failed ({e}). Posting detail here:")
        for chunk in split_report(report):
            await channel.send(chunk)
            await asyncio.sleep(0.4)

    # Clean up state — no longer needed after comp completes
    channel_state.pop(channel.id, None)


# ── Survey Poster ─────────────────────────────────────────────────────────────

async def post_survey(channel: discord.TextChannel, address: str):
    """Post the offer type selector. Rep picks type, then gets repair cost modal."""
    channel_state[channel.id] = {
        "address": address,
        "repairs": None,
        "offer_type": None,
        "fired": False,
    }
    await channel.send(
        f"🏠 **{address}**\nWhat type of offer do you want to generate?",
        view=OfferTypeView(channel.id)
    )


# ── Bot Events ────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅ FHB Comp Bot v3 online as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"   Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"   Slash sync failed: {e}")


@bot.event
async def on_guild_channel_create(channel):
    if not isinstance(channel, discord.TextChannel):
        return
    if not is_watched_channel(channel):
        return

    # Wait for Tickety to post its lead message
    await asyncio.sleep(8)
    lead = await extract_lead_data(channel)

    if not lead or not lead.get("address"):
        await asyncio.sleep(7)
        lead = await extract_lead_data(channel)

    # Use whatever address we got — partial is fine, Claude handles it
    address = lead["address"] if lead and lead.get("address") else "Unknown address"
    await post_survey(channel, address)


# ── Slash Commands ────────────────────────────────────────────────────────────

@bot.tree.command(name="comp", description="Run a comp analysis on a property address")
async def comp_slash(interaction: discord.Interaction, address: str):
    valid, warning = validate_address(address)
    if not valid:
        await interaction.response.send_message(
            f"⚠️ Address `{address}` looks invalid ({warning}). Please provide a full address.",
            ephemeral=True
        )
        return
    if warning:
        await interaction.response.send_message(
            f"📋 Starting survey for `{address}` _(note: {warning})_"
        )
    else:
        await interaction.response.send_message(
            f"📋 Starting condition survey for `{address}`..."
        )
    await post_survey(interaction.channel, address)


@bot.tree.command(name="recomp", description="Re-run survey for this lead channel")
async def recomp_slash(interaction: discord.Interaction):
    lead = await extract_lead_data(interaction.channel)
    if not lead or not lead.get("address"):
        await interaction.response.send_message(
            "❌ No address found. Use `/comp [address]` instead.", ephemeral=True
        )
        return
    await interaction.response.send_message(f"🔄 Restarting survey for `{lead['address']}`...")
    await post_survey(interaction.channel, lead["address"])


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
