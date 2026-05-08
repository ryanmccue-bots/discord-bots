"""
FHB Comp Bot v3
- Tickety creates channel → bot posts condition survey with buttons
- Rep fills in ROOF / HVAC / CONDITION
- Bot runs comp analysis and posts short offer card in channel
- Full comp detail posted in a thread for Alec to review
"""

import discord
from discord.ext import commands
from discord import app_commands
import anthropic
import asyncio
import re
import os
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
DISCORD_TOKEN     = os.environ.get("DISCORD_TOKEN", "YOUR_DISCORD_TOKEN_HERE")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "YOUR_ANTHROPIC_API_KEY_HERE")

TICKETY_BOT_ID    = None       # Set to Tickety's user ID for reliable detection
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
    return "tickety" in message.author.name.lower() or message.author.bot


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


async def extract_lead_data(channel: discord.TextChannel) -> dict | None:
    try:
        pins = await channel.pins()
        for msg in pins:
            if is_tickety_message(msg) and msg.content:
                data = parse_tickety_message(msg.content)
                if data.get("address"):
                    return data
    except Exception:
        pass
    try:
        async for msg in channel.history(limit=10, oldest_first=True):
            if is_tickety_message(msg) and msg.content:
                data = parse_tickety_message(msg.content)
                if data.get("address"):
                    return data
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
# Single message with all 3 questions — prevents other bots from interleaving.
# Row 0: Roof label + answers
# Row 1: HVAC answers
# Row 2: Condition answers
# Label buttons (disabled) use row headers in the message text instead.

def _all_answered(channel_id: int) -> bool:
    state = channel_state.get(channel_id, {})
    return all(state.get(k) for k in ["roof", "hvac", "condition"])


async def _check_and_fire(interaction: discord.Interaction, channel_id: int, view: discord.ui.View):
    """If all three questions answered, lock the view and kick off comp analysis."""
    if _all_answered(channel_id):
        state = channel_state.get(channel_id, {})
        # Disable all remaining buttons
        for item in view.children:
            item.disabled = True
        summary = (
            f"🏠 **Condition Survey — {state.get('address', 'unknown address')}**\n"
            f"✅ All answered — running comp analysis...\n\n"
            f"🏠 Roof: **{state['roof']}**\n"
            f"❄️ HVAC: **{state['hvac']}**\n"
            f"🔨 Condition: **{state['condition']}**"
        )
        await interaction.message.edit(content=summary, view=view)
        if not state.get("address"):
            await interaction.channel.send(
                "⚠️ No address found — use `/comp [full address]` to run the analysis."
            )
            return
        await interaction.channel.send("🔍 Running comp analysis...")
        asyncio.create_task(run_and_post_offers(interaction.channel))


class ConditionSurvey(discord.ui.View):
    """Single-message survey with all 3 questions in one view."""

    def __init__(self, channel_id: int):
        super().__init__(timeout=3600)
        self.channel_id = channel_id

    def _survey_text(self) -> str:
        state = channel_state.get(self.channel_id, {})
        address = state.get("address", "")
        roof = f"**{state['roof']}** ✅" if state.get("roof") else "_tap below_"
        hvac = f"**{state['hvac']}** ✅" if state.get("hvac") else "_tap below_"
        cond = f"**{state['condition']}** ✅" if state.get("condition") else "_tap below_"
        addr_suffix = f" — {address}" if address else ""
        header = f"🏠 **Condition Survey{addr_suffix}**"
        return (
            f"{header}\n"
            f"\n🏠 **1. Roof:** {roof}"
            f"\n❄️ **2. HVAC:** {hvac}"
            f"\n🔨 **3. Condition:** {cond}"
        )

    async def _pick(self, interaction: discord.Interaction, field: str, value: str):
        channel_state[self.channel_id][field] = value
        # Update button styles for this field's row
        field_rows = {"roof": 0, "hvac": 1, "condition": 2}
        target_row = field_rows[field]
        for item in self.children:
            if item.row == target_row:
                item.style = discord.ButtonStyle.success if item.label == value else discord.ButtonStyle.secondary
                item.disabled = True
        await interaction.response.edit_message(content=self._survey_text(), view=self)
        await _check_and_fire(interaction, self.channel_id, self)

    # ── Roof (row 0) ──────────────────────────────────────────────────────────
    @discord.ui.button(label="New", style=discord.ButtonStyle.primary, row=0)
    async def roof_new(self, i: discord.Interaction, b: discord.ui.Button):
        await self._pick(i, "roof", "New")

    @discord.ui.button(label="Good", style=discord.ButtonStyle.primary, row=0)
    async def roof_good(self, i: discord.Interaction, b: discord.ui.Button):
        await self._pick(i, "roof", "Good")

    @discord.ui.button(label="Needs Replacing", style=discord.ButtonStyle.primary, row=0)
    async def roof_replace(self, i: discord.Interaction, b: discord.ui.Button):
        await self._pick(i, "roof", "Needs Replacing")

    # ── HVAC (row 1) ──────────────────────────────────────────────────────────
    @discord.ui.button(label="New", style=discord.ButtonStyle.primary, row=1)
    async def hvac_new(self, i: discord.Interaction, b: discord.ui.Button):
        await self._pick(i, "hvac", "New")

    @discord.ui.button(label="Good", style=discord.ButtonStyle.primary, row=1)
    async def hvac_good(self, i: discord.Interaction, b: discord.ui.Button):
        await self._pick(i, "hvac", "Good")

    @discord.ui.button(label="Needs Replacing", style=discord.ButtonStyle.primary, row=1)
    async def hvac_replace(self, i: discord.Interaction, b: discord.ui.Button):
        await self._pick(i, "hvac", "Needs Replacing")

    # ── Condition (row 2) ─────────────────────────────────────────────────────
    @discord.ui.button(label="Needs Full Rehab", style=discord.ButtonStyle.danger, row=2)
    async def cond_full(self, i: discord.Interaction, b: discord.ui.Button):
        await self._pick(i, "condition", "Needs Full Rehab")

    @discord.ui.button(label="Needs Some Work", style=discord.ButtonStyle.primary, row=2)
    async def cond_some(self, i: discord.Interaction, b: discord.ui.Button):
        await self._pick(i, "condition", "Needs Some Work")

    @discord.ui.button(label="Needs Little Work", style=discord.ButtonStyle.success, row=2)
    async def cond_little(self, i: discord.Interaction, b: discord.ui.Button):
        await self._pick(i, "condition", "Needs Little Work")


# ── Three separate per-question views (used by post_survey) ──────────────────

class RoofView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=3600)
        self.channel_id = channel_id

    async def _pick(self, interaction: discord.Interaction, value: str):
        channel_state[self.channel_id]["roof"] = value
        for item in self.children:
            item.disabled = True
            item.style = discord.ButtonStyle.success if item.label == value else discord.ButtonStyle.secondary
        await interaction.response.edit_message(
            content=f"🏠 **1. Roof:** ✅ {value}", view=self
        )
        await _check_and_fire_simple(interaction.channel, self.channel_id)

    @discord.ui.button(label="New", style=discord.ButtonStyle.primary)
    async def new(self, i: discord.Interaction, b: discord.ui.Button):
        await self._pick(i, "New")

    @discord.ui.button(label="Good", style=discord.ButtonStyle.primary)
    async def good(self, i: discord.Interaction, b: discord.ui.Button):
        await self._pick(i, "Good")

    @discord.ui.button(label="Needs Replacing", style=discord.ButtonStyle.primary)
    async def replace(self, i: discord.Interaction, b: discord.ui.Button):
        await self._pick(i, "Needs Replacing")


class HvacView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=3600)
        self.channel_id = channel_id

    async def _pick(self, interaction: discord.Interaction, value: str):
        channel_state[self.channel_id]["hvac"] = value
        for item in self.children:
            item.disabled = True
            item.style = discord.ButtonStyle.success if item.label == value else discord.ButtonStyle.secondary
        await interaction.response.edit_message(
            content=f"❄️ **2. HVAC:** ✅ {value}", view=self
        )
        await _check_and_fire_simple(interaction.channel, self.channel_id)

    @discord.ui.button(label="New", style=discord.ButtonStyle.primary)
    async def new(self, i: discord.Interaction, b: discord.ui.Button):
        await self._pick(i, "New")

    @discord.ui.button(label="Good", style=discord.ButtonStyle.primary)
    async def good(self, i: discord.Interaction, b: discord.ui.Button):
        await self._pick(i, "Good")

    @discord.ui.button(label="Needs Replacing", style=discord.ButtonStyle.primary)
    async def replace(self, i: discord.Interaction, b: discord.ui.Button):
        await self._pick(i, "Needs Replacing")


class ConditionView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=3600)
        self.channel_id = channel_id

    async def _pick(self, interaction: discord.Interaction, value: str):
        channel_state[self.channel_id]["condition"] = value
        for item in self.children:
            item.disabled = True
            item.style = discord.ButtonStyle.success if item.label == value else discord.ButtonStyle.secondary
        await interaction.response.edit_message(
            content=f"🔨 **3. Condition:** ✅ {value}", view=self
        )
        await _check_and_fire_simple(interaction.channel, self.channel_id)

    @discord.ui.button(label="Needs Full Rehab", style=discord.ButtonStyle.danger)
    async def full(self, i: discord.Interaction, b: discord.ui.Button):
        await self._pick(i, "Needs Full Rehab")

    @discord.ui.button(label="Needs Some Work", style=discord.ButtonStyle.primary)
    async def some(self, i: discord.Interaction, b: discord.ui.Button):
        await self._pick(i, "Needs Some Work")

    @discord.ui.button(label="Needs Little Work", style=discord.ButtonStyle.success)
    async def little(self, i: discord.Interaction, b: discord.ui.Button):
        await self._pick(i, "Needs Little Work")


async def _check_and_fire_simple(channel: discord.abc.Messageable, channel_id: int):
    """Fire comp analysis once all 3 separate question views are answered."""
    if _all_answered(channel_id):
        state = channel_state.get(channel_id, {})
        if not state.get("address"):
            await channel.send(
                "⚠️ No address found — use `/comp [full address]` to run the analysis."
            )
            return
        await channel.send("🔍 Running comp analysis...")
        asyncio.create_task(run_and_post_offers(channel))

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


def build_comp_prompt(address: str, roof: str, hvac: str, condition: str) -> str:
    score, tier = condition_to_score(roof, hvac, condition)
    date_str = datetime.now().strftime("%B %d, %Y")

    return f"""Perform a complete comp analysis for this wholesale lead.

## SUBJECT PROPERTY
- Address: {address}
- Roof: {roof}
- HVAC: {hvac}
- Overall Condition: {condition}
- Estimated Condition Score: {score}/10 ({tier} rehab)

## YOUR TASK
Use web search to find:
1. Subject property details (beds, baths, sqft, year built, lot size) from Zillow/Redfin/public records
2. Market conditions for the zip code (DOM, inventory, sale-to-list ratio)
3. Sold comps — fully renovated, same area, last 6 months where possible

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
    """Extract market type from report."""
    match = re.search(r"Type:\s+(.+)", report)
    if match:
        return match.group(1).strip().lower()
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
    repairs_mid: int,
    market_type: str,
    roof: str,
    hvac: str,
    condition: str,
    confidence: str,
) -> str:
    cash_pct = cash_investment_pct(market_type)
    cash_gross = int(arv * cash_pct)
    cash_offer = max(0, cash_gross - repairs_mid - CASH_FEE)

    lines = [
        f"# 🏠 {address}",
        f"*ARV: ${arv:,} · Repairs (mid): ${repairs_mid:,} · Confidence: {confidence}*",
        f"*Roof: {roof} · HVAC: {hvac} · Condition: {condition}*",
        "",
        "---",
        "",
        f"## 💰 CASH OFFER: **${cash_offer:,}**",
        "```",
        f"ARV:            ${arv:,}",
        f"× {int(cash_pct*100)}%:          ${cash_gross:,}",
        f"− Repairs:      −${repairs_mid:,}",
        f"− Fee:          −${CASH_FEE:,}",
        f"= Cash Offer:   ${cash_offer:,}",
        "```",
    ]

    if novation_eligible(roof, hvac, condition):
        nov_pct = novation_investment_pct(market_type)
        nov_gross = int(arv * nov_pct)
        nov_offer = max(0, nov_gross - NOVATION_FEE)
        lines += [
            "",
            f"## 📋 NOVATION OFFER: **${nov_offer:,}**",
            "```",
            f"ARV:              ${arv:,}",
            f"× {int(nov_pct*100)}%:            ${nov_gross:,}",
            f"(No repair deduction — seller completes at close)",
            f"= Novation Offer: ${nov_offer:,}",
            "```",
        ]
    else:
        lines += [
            "",
            "## 📋 NOVATION OFFER: **Not eligible**",
            "> Roof or HVAC needs replacing, or property needs full rehab.",
        ]

    lines += ["", "---", "*💬 Full comp detail in thread below ↓*"]
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

def run_comp_analysis(address: str, prompt: str) -> str:
    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            # Cache the system prompt — it's identical on every call
            system=[
                {
                    "type": "text",
                    "text": COMP_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt,
                            # Cache the methodology portion of the prompt too
                            # (everything up to the address-specific part is reusable)
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            ],
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

    address  = state["address"]
    roof     = state["roof"]
    hvac     = state["hvac"]
    condition = state["condition"]

    score, tier = condition_to_score(roof, hvac, condition)
    prompt = build_comp_prompt(address, roof, hvac, condition)

    # Run analysis in executor
    loop = asyncio.get_event_loop()
    report = await loop.run_in_executor(None, lambda: run_comp_analysis(address, prompt))

    # Parse ARV and market from report
    arv = parse_arv_from_report(report)
    market_type = parse_market_type(report)

    # Parse confidence
    conf_match = re.search(r"Confidence:\s*(HIGH|MEDIUM|LOW|VERY LOW)", report)
    confidence = conf_match.group(1) if conf_match else "LOW"

    if not arv:
        await channel.send(
            f"⚠️ **Could not parse ARV from comp analysis.**\n"
            f"The full report has been posted in a thread. Please calculate offer manually."
        )
    else:
        # Calculate repair estimate
        _, repairs_mid, _ = repair_range(score)

        # Build and post offer card
        offer_card = build_offer_card(
            address, arv, repairs_mid, market_type,
            roof, hvac, condition, confidence
        )
        offer_msg = await channel.send(offer_card)

        # Pin the offer card
        try:
            await offer_msg.pin()
        except Exception:
            pass

        # Create thread and post full detail
        try:
            thread = await offer_msg.create_thread(
                name=f"Comp Detail — {address[:40]}",
                auto_archive_duration=1440
            )
            chunks = split_report(report)
            for i, chunk in enumerate(chunks):
                await thread.send(chunk)
                await asyncio.sleep(0.4)
        except Exception as e:
            # If thread creation fails, post detail in channel
            await channel.send(f"⚠️ Thread creation failed ({e}). Posting detail here:")
            for chunk in split_report(report):
                await channel.send(chunk)
                await asyncio.sleep(0.4)


# ── Survey Poster ─────────────────────────────────────────────────────────────

async def post_survey(channel: discord.TextChannel, address: str):
    """Post three separate survey questions. Called after other bots have finished posting."""
    channel_state[channel.id] = {
        "address": address,
        "roof": None,
        "hvac": None,
        "condition": None,
    }
    await channel.send("🏠 **1. Roof condition?**", view=RoofView(channel.id))
    await channel.send("❄️ **2. HVAC condition?**", view=HvacView(channel.id))
    await channel.send("🔨 **3. Overall condition?**", view=ConditionView(channel.id))


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
