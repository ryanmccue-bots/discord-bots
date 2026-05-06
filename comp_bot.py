"""
FHB Comp Bot — Full Version
Triggers on new channels created by Tickety bot.
Reads structured lead data from the pinned Tickety message.
Runs a full comp analysis via Claude + web search.
Posts and pins the report in the channel.

Extensions included:
  - Rural/suburban market comping
  - Institutional-grade analysis (weighted scoring, confidence intervals, sensitivity)
  - Negotiation intelligence (offer bracket, scripts, deal scoring)
  - Condo/townhome/attached housing
  - Land/zoning/highest-and-best-use
"""

import discord
from discord.ext import commands
import anthropic
import asyncio
import re
import os
from datetime import datetime

# ── Config ───────────────────────────────────────────────────────────────────
DISCORD_TOKEN   = os.environ.get("DISCORD_TOKEN", "YOUR_DISCORD_TOKEN_HERE")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "YOUR_ANTHROPIC_API_KEY_HERE")

# Tickety bot user ID — find by right-clicking Tickety in Discord → Copy User ID
# Leave as None to match by name instead
TICKETY_BOT_ID = None  # e.g. 1234567890123456789

# Only run comps in these category names (case-insensitive). Leave empty = all categories.
WATCH_CATEGORIES: list[str] = []  # e.g. ["Leads", "Active Pipeline"]

# Wholesale fee used in MAO formula
WHOLESALE_FEE = 12_500

# ── Bot Setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ── Lead Data Extraction ──────────────────────────────────────────────────────

def is_tickety_message(message: discord.Message) -> bool:
    """Return True if this message was posted by the Tickety bot."""
    if TICKETY_BOT_ID:
        return message.author.id == TICKETY_BOT_ID
    return "tickety" in message.author.name.lower() or message.author.bot


def extract_field_lines(content: str, label_pattern: str, max_lines: int = 2) -> str | None:
    """
    Find a labeled field in a Tickety message and return up to `max_lines` of content
    after the label. Stops at the next numbered field, blank line pair, or end of string.

    Example — label "street address of the property" followed by:
        2202 Glenwood Ave        ← line 1
        Saginaw, MI 48601        ← line 2 (split address)
    → returns "2202 Glenwood Ave Saginaw, MI 48601"
    """
    pattern = rf"(?:{label_pattern})[^\n]*\n(.*?)(?=\n\s*\n\d+\.|\n\d+\.|\Z)"
    match = re.search(pattern, content, re.IGNORECASE | re.DOTALL)
    if not match:
        return None

    raw_block = match.group(1).strip()
    # Take up to max_lines non-empty lines
    lines = [ln.strip() for ln in raw_block.split("\n") if ln.strip()][:max_lines]
    return " ".join(lines) if lines else None


# US state abbreviations and full names for address validation
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
    """
    Check that an extracted address looks like a real US property address.
    Returns (is_valid, reason_if_invalid).

    Accepts wide format variation — commas/no commas, full state/abbreviation,
    zip code present or absent. Claude handles normalization from here.
    """
    if not address or len(address.strip()) < 8:
        return False, "too short"

    addr = address.strip()

    # Must start with a street number (handles ranges like "1111-1113" too)
    if not re.match(r"^\d{1,6}[-\d]*\s", addr):
        return False, "no street number found at start"

    # Must contain at least one word that looks like a street name (2+ letters)
    words = re.findall(r"[a-zA-Z]{2,}", addr)
    if len(words) < 1:
        return False, "no street name found"

    # Should contain a recognizable US state (abbreviation or full name)
    # Split on spaces/commas to get individual tokens for abbreviation matching
    tokens = {t.strip(",.") for t in re.split(r"[\s,]+", addr)}
    has_state = (
        any(t.upper() in _US_STATES for t in tokens)          # abbrev match
        or any(s in addr.lower() for s in _US_STATES          # full name match
               if len(s) > 3)
    )
    if not has_state:
        # Soft warning — don't reject, but flag it. Some rural addresses lack state.
        return True, "no state detected (will search anyway — verify if comp quality is low)"

    return True, ""


def parse_tickety_message(content: str) -> dict:
    """
    Parse a Tickety lead message. Handles multi-line addresses and wide
    formatting variation (commas, full state names, abbreviations, zip optional).

    Example input:
        1. Street Address of the Property
        2202 Glenwood Ave, Saginaw, MI 48601

        2. Name of the Seller
        Marsha

        3. Seller's Asking Price
        15000

    Returns a dict with keys: address, address_valid, address_warning,
    seller_name, asking_price, asking_price_raw, and any optional fields found.
    """
    data: dict = {}

    # ── Address (grab up to 2 lines to handle split addresses) ──────────────
    raw_address = extract_field_lines(
        content,
        label_pattern=r"street address of the property|property address|address",
        max_lines=2,
    )

    if raw_address:
        # Normalize: collapse internal newlines → single space, strip extra whitespace
        normalized = re.sub(r"\s+", " ", raw_address).strip()
        data["address"] = normalized

        valid, reason = validate_address(normalized)
        data["address_valid"] = valid
        data["address_warning"] = reason if reason else ""
    else:
        data["address"] = None
        data["address_valid"] = False
        data["address_warning"] = "address field not found in message"

    # ── Seller name ──────────────────────────────────────────────────────────
    seller_raw = extract_field_lines(content, r"name of the seller|seller name|seller", max_lines=1)
    if seller_raw:
        data["seller_name"] = seller_raw.strip()

    # ── Asking price ─────────────────────────────────────────────────────────
    price_raw = extract_field_lines(content, r"asking price|seller.{0,10}price|list price", max_lines=1)
    if price_raw:
        data["asking_price_raw"] = price_raw.strip()
        numeric = re.sub(r"[^\d.]", "", price_raw)
        data["asking_price"] = float(numeric) if numeric else None

    # ── Optional fields Tickety may or may not include ───────────────────────
    for label, key in [
        (r"bed(?:room)?s?",          "beds"),
        (r"bath(?:room)?s?",         "baths"),
        (r"sq(?:uare)?\s?f(?:oo)?t", "sqft"),
        (r"year built",              "year_built"),
        (r"property type",           "property_type"),
        (r"condition",               "condition"),
        (r"lot size",                "lot_size"),
        (r"notes?|additional info",  "notes"),
    ]:
        val = extract_field_lines(content, label, max_lines=1)
        if val:
            data[key] = val.strip()

    return data


async def extract_lead_data(channel: discord.TextChannel) -> dict | None:
    """
    Read the channel's pinned messages and recent history to find the Tickety
    lead message. Returns parsed lead data dict or None if not found.
    """
    # Try pinned messages first
    try:
        pins = await channel.pins()
        for msg in pins:
            if is_tickety_message(msg) and msg.content:
                data = parse_tickety_message(msg.content)
                if data.get("address"):
                    return data
    except Exception:
        pass

    # Fall back to channel history (first 10 messages)
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
    """Return True if we should run a comp for this channel."""
    if WATCH_CATEGORIES:
        if not channel.category:
            return False
        if channel.category.name.lower() not in [c.lower() for c in WATCH_CATEGORIES]:
            return False
    return True


# ── Prompt Builder ────────────────────────────────────────────────────────────

COMP_SYSTEM_PROMPT = """You are an elite real estate comping analyst for a real estate wholesaling company.
You have deep knowledge of professional appraisal methodology, ARV calculation, and MAO formulas.
You are thorough, conservative, and data-driven. You flag uncertainty honestly."""


def build_comp_prompt(lead: dict, wholesale_fee: int = WHOLESALE_FEE) -> str:
    address       = lead.get("address", "Unknown")
    seller_name   = lead.get("seller_name", "Unknown")
    asking_price  = lead.get("asking_price")
    asking_raw    = lead.get("asking_price_raw", "Unknown")
    beds          = lead.get("beds", "unknown")
    baths         = lead.get("baths", "unknown")
    sqft          = lead.get("sqft", "unknown")
    year_built    = lead.get("year_built", "unknown")
    prop_type     = lead.get("property_type", "unknown")
    condition     = lead.get("condition", "unknown")

    asking_str = f"${asking_price:,.0f}" if asking_price else asking_raw

    date_str = datetime.now().strftime("%B %d, %Y")

    return f"""Perform a complete comp analysis for this wholesale lead. Use web search to pull Zillow/Redfin data.

## SUBJECT PROPERTY
- Address: {address}
- Seller: {seller_name}
- Seller's Asking Price: {asking_str}
- Beds/Baths: {beds} bed / {baths} bath
- Sqft: {sqft}
- Year Built: {year_built}
- Property Type: {prop_type}
- Condition (if known): {condition}

---

## STEP 1 — GATHER PROPERTY DATA
Use web search to find public listing data for {address}:
- Beds, baths, sqft, year built, lot size, construction type, garage, pool
- Any prior MLS listing history, prior sale price, days on market
- Property photos to estimate condition (score 1–10: 1=Tear Down, 10=Newly Remodeled)

## STEP 2 — MARKET CONDITIONS
Search for market data in the subject's zip code / city:
- Months of inventory (buyer's <3 / neutral 3-6 / seller's 6+)
- Average days on market
- Sale-to-list price ratio
- Price trend (QoQ, YoY)
- Absorption rate: (sold last 6 months / 6) = monthly absorption; active / monthly = months of inventory
- Pending ratio: pending / (pending + active). >50% = strong demand.

## STEP 3 — FIND COMPS
Pull SOLD comps from Zillow/Redfin. Apply filters IN ORDER:
1. Same subdivision, same zip, no major road crossings, within 0.5–1 mile
2. Same property type and style (ranch with ranch, etc.)
3. Within ±10% sqft, ±10 years age, ±2,500 sqft lot
4. Match construction material (wood frame vs block/brick)
5. Comps must be FULLY RENOVATED (condition 8–10)
6. Buyer's market: 90 days max. Seller's market: up to 6 months.

**If fewer than 3 comps exist within 1 mile → RURAL EXTENSION applies:**
- Tier 1 (best): same area, same type, within 12 months, within 2 miles — no discount
- Tier 2 (acceptable): same county, within 12 months, within 5 miles — multiply comp by 0.95
- Tier 3 (last resort): adjacent counties, within 18 months, within 10 miles — multiply comp by 0.90
- Apply acreage adjustments, well/septic adjustments if applicable

**SCORE each comp 0-100:**
| Factor | Weight | Scoring |
|--------|--------|---------|
| Recency | 25% | 100 = sold this month, -10 per month older |
| Proximity | 25% | 100 = same street, -5 per 0.1 mile |
| Size match | 20% | 100 = exact sqft, -2 per 50 sqft diff |
| Style match | 15% | 100 = identical, 70 = similar, 40 = different |
| Condition match | 15% | 100 = same condition, -10 per grade diff |

Also pull ACTIVE and PENDING listings for the active comp check.

## STEP 4 — ACTIVE COMP CHECK
- If renovated active listing sits 90+ days: ARV = listing price × 0.90
- DOM 60–90: overpriced 5–10%. DOM 90+: overpriced 10%+.
- Pending ratio check. If property sat 60+ days before pending, contract price likely 10–20% below list.

## STEP 5 — ADJUSTMENTS
Apply these adjustments relative to subject property:

**Feature adjustments (under $600K):**
- Bedroom: ±$10K–$25K each
- Full bathroom: ±$10K; half bath ±$5K
- 2-car garage: ±$10K–$25K (±$25K in extreme climates)
- Carport: ±$5K–$10K
- In-ground pool: ±$10K–$30K. Above-ground pool = $0.
- Pool only rule: if subject is only one WITHOUT pool vs all comps: -$30K under $600K
- Lot size: ±$5K–$10K per 5,000 sqft (under $600K)
- Proximity to highway/commercial/multifamily: -$15K (siding), -$20K (backing), -$30K (fronting)
- Construction material mismatch (wood vs block): -10% to -20%
- Waterfront/landlocked mismatch: comp × 0.85

**Well & Septic (if applicable):**
- Low-yield well (<3 GPM): -$5K to -$15K
- Well needing replacement: -$10K to -$30K
- Failed septic: -$20K to -$40K
- Comp has city water/sewer, subject has well/septic: -$10K to -$20K combined

**Rural lot acreage (if applicable):**
- First acre: full value
- Acres 2–5: $3–$8/sqft of usable land
- Acres 5–10: $1–$4/sqft
- Acres 10+: raw land value only

**School district (if applicable):**
- Each rating point difference = ±2% adjustment

**ADU/Guest House:**
- Separate parcel + built: 100% of $/sqft value
- No parcel + finished: 50% of $/sqft value
- No parcel, not built: $0
- Unfinished: $0

**Condo/Townhome specific (if applicable):**
- Floor level: ±2–3% per floor
- End unit vs interior: +3–5%
- View side vs parking lot: +5–10%
- HOA fee difference >$100/mo: −$12K–$15K per $100/mo extra
- Parking type: private garage +$15K–$30K; street only -$10K–$20K

## STEP 6 — ARV CALCULATION
1. Calculate weighted average: each comp's price × its score, then divide by total scores
2. Cross-check with $/sqft as secondary reference
3. Cross-check against active comp analysis
4. Produce THREE ARV estimates:
   - Conservative (Low): worst-case comp, for buyer's market calculations
   - Most Likely (Mid): weighted average, use for standard MAO
   - Optimistic (High): best-case scenario
   - If spread >15%: low confidence, use 65% or lower

**Check for land/zoning upside (if lot unusually large or area is transitioning):**
- Is there ADU potential? Addition play? Lot split? Teardown economics?
- Teardown math: lot value = new construction price - build cost (~$150–$350/sqft)

**Seasonal adjustment:**
- Spring (Mar–May): prices ~3–5% above annual avg
- Summer (Jun–Aug): ~1–3% above
- Fall (Sep–Nov): at or slightly below avg
- Winter (Dec–Feb): 3–7% below peak
Note current season and whether comps are from a different season than planned exit.

**Market trend overlay:**
- If all 4 indicators (price trend, DOM trend, list-to-sale trend, inventory trend) are bullish: +3–5% to ARV
- All bearish: -3–5%
- Mixed: no adjustment, note uncertainty

## STEP 7 — REPAIR ESTIMATE
Estimate condition 1–10 from listing photos/description:
- 8–10 (clean/remodeled): $0–$5/sqft
- 6–7 (dated/rentable): $15–$25/sqft
- 4–5 (dirty/hoarder): $25–$40/sqft
- 3 (needs everything): $40–$60/sqft
- 1–2 (teardown/fire/foundation): $60–$100+/sqft
- Luxury renovation: $75–$120+/sqft

## STEP 8 — MAO + SENSITIVITY TABLE

**Market % to use:**
- Hot seller's: 75–80% | Normal: 70% | Buyer's: 65% | Risky/rural: 60–65%
- Very rural (30+ min from nearest city): 60%
- Agricultural zoning: 65%

**MAO formula:**
MAO = (ARV_mid × Investment%) − Repairs_mid − ${wholesale_fee:,} wholesale fee

**Sensitivity table:**
| Scenario | ARV | Repairs | MAO at 70% | Buyer Profit |
|----------|-----|---------|-----------|-------------|
| Best case | ARV+5% | Low | calculated | calculated |
| Base case | ARV_mid | Mid | calculated | calculated |
| Worst case | ARV-5% | High | calculated | calculated |
| Disaster | ARV-10% | High+20% | calculated | calculated |

Buyer profit = ARV − Repairs − Closing costs (~8%) − MAO

## NEGOTIATION SUMMARY
Based on the seller's asking price of {asking_str}:

**Offer bracket:**
| Level | Formula | Amount |
|-------|---------|--------|
| Anchor (open with) | MAO − 10% | $X |
| Target (ideal) | MAO | $X |
| Stretch (only if deal has extra value) | MAO + 5% | $X |
| Walk-Away (absolute ceiling) | MAO + 10% | $X |

**Seller motivation signals:** Assess based on property type, vacancy, listing history, and any details available.

**Verdict on asking price:** Is {asking_str} below Anchor / between Anchor and Target / between Target and Walk-Away / above Walk-Away?

---

## OUTPUT FORMAT

Use Discord markdown. Post ALL sections below.

---
**🏠 COMP REPORT — {address}**
*Generated {date_str} · Seller: {seller_name} · Asking: {asking_str}*

**📍 SUBJECT PROPERTY**
[Address, beds/baths/sqft/year built, lot size, construction, garage, pool, condition score X/10]

**📊 MARKET CONDITIONS**
[Market type | Avg DOM | Sale-to-list ratio | Absorption rate | Months of inventory | Pending ratio | Demand level | Trend: bullish/bearish/mixed]

**🏡 COMPARABLE SALES**
[Each comp: address, sale price, sqft, $/sqft, date, score/100, tier (1/2/3 if rural), similarity notes]

**⚠️ ACTIVE COMP CHECK**
[Active/pending listings and DOM. Any contradictions flagged with adjusted ARV impact. Or: "No contradictions found."]

**🔧 ADJUSTMENTS APPLIED**
[Each adjustment: feature, dollar amount, reason]

**💰 ARV**
Conservative: $[X] | **Most Likely: $[X]** | Optimistic: $[X]
Spread: [X]% → Confidence: **[HIGH / MEDIUM / LOW / VERY LOW]**
[2–3 sentence rationale. Note seasonal adjustment if applicable.]

**🔨 REPAIR ESTIMATE**
Condition: [X/10] — Low: $[X] / Mid: $[X] / High: $[X]
*(Based on [sqft] sqft × $[X]–$[X]/sqft)*

**📋 MAO CALCULATION**
Investment %: [X]% ([market type] market)
ARV $[X] × [X]% = $[X]
− Repairs (mid): $[X]
− Wholesale fee: ${wholesale_fee:,}
**= MAO: $[X]**

**📊 SENSITIVITY TABLE**
| Scenario | ARV | Repairs | MAO | Buyer Profit |
|----------|-----|---------|-----|-------------|
| Best | | | | |
| Base | | | | |
| Worst | | | | |
| Disaster | | | | |

**💬 NEGOTIATION BRIEF**
Asking: {asking_str}
Anchor: $[X] | Target: $[X] | Stretch: $[X] | Walk-Away: $[X]
Verdict: [Asking is below Anchor ✅ / between Anchor–Target ✅ / between Target–Walk-Away ⚠️ / above Walk-Away 🚫]
Seller motivation signals: [assessment]
Opening script: "[One sentence tailored to this deal's comp data]"

**🏗️ LAND / ZONING FLAGS** *(if applicable)*
[ADU potential, addition play, lot split, teardown economics, zoning upside — or "None identified."]

**🌾 RURAL FLAGS** *(if applicable)*
[Well/septic exposure, buyer pool depth, exit strategy viability — or "N/A — urban/suburban market."]

**✅ GO / 🚫 NO-GO**
[Clear verdict] — [2–3 sentences on key risks or green flags. Include whether asking price is dealable.]

---
*⚡ FHB Comp Bot · Always verify comps manually before making an offer · Confidence: [HIGH/MEDIUM/LOW/VERY LOW]*
"""


# ── Analysis Runner ───────────────────────────────────────────────────────────

def run_comp_analysis(address: str, prompt: str) -> str:
    """Call Claude with web search. Returns the report text."""
    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            system=COMP_SYSTEM_PROMPT,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}],
        )
        parts = [block.text for block in response.content if hasattr(block, "text")]
        return "\n".join(parts).strip() or "⚠️ No analysis generated — try `/comp` manually."
    except Exception as e:
        return f"⚠️ Analysis error: {e}"


def split_report(report: str, max_len: int = 1990) -> list[str]:
    """Split a long report into Discord-safe chunks at section headers."""
    section_starters = ["**🏠", "**📍", "**📊", "**🏡", "**⚠️", "**🔧",
                        "**💰", "**🔨", "**📋", "**💬", "**🏗️", "**🌾",
                        "**✅", "**🚫"]
    chunks, current = [], ""
    for line in report.split("\n"):
        is_header = any(line.startswith(h) for h in section_starters)
        if is_header and len(current) + len(line) > max_len:
            if current.strip():
                chunks.append(current.strip())
            current = line + "\n"
        elif len(current) + len(line) + 1 > max_len:
            if current.strip():
                chunks.append(current.strip())
            current = line + "\n"
        else:
            current += line + "\n"
    if current.strip():
        chunks.append(current.strip())
    return chunks or [report[:1990]]


async def post_comp_report(channel: discord.TextChannel, lead: dict):
    """Run analysis and post the report to the channel."""
    address = lead.get("address", "Unknown address")
    asking  = lead.get("asking_price_raw") or lead.get("asking_price") or "unknown"

    # Loading message
    loading = await channel.send(
        f"🔍 **Running comp analysis...**\n"
        f"📍 `{address}` · Asking: `${asking}`\n"
        f"⏳ Pulling Zillow data — usually 30–60 sec..."
    )

    try:
        prompt = build_comp_prompt(lead)
        loop   = asyncio.get_event_loop()
        report = await loop.run_in_executor(None, lambda: run_comp_analysis(address, prompt))
    except Exception as e:
        report = f"⚠️ Analysis failed: {e}"

    try:
        await loading.delete()
    except Exception:
        pass

    # Post report (split if needed)
    first_msg = None
    chunks = split_report(report) if len(report) > 1990 else [report]
    for i, chunk in enumerate(chunks):
        msg = await channel.send(chunk)
        if i == 0:
            first_msg = msg
        await asyncio.sleep(0.4)

    # Pin the first chunk
    if first_msg:
        try:
            await first_msg.pin()
        except Exception:
            pass


# ── Bot Events ────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅ FHB Comp Bot online as {bot.user}")
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

    # Wait for Tickety to post its message
    await asyncio.sleep(5)

    lead = await extract_lead_data(channel)

    # No address at all
    if not lead or not lead.get("address"):
        await channel.send(
            "🏠 **FHB Comp Bot** — Channel created but no address found in the Tickety message.\n"
            "Use `/comp [full address]` to run the comp manually."
        )
        return

    # Address found but failed hard validation (e.g. no street number)
    if not lead.get("address_valid", True):
        reason = lead.get("address_warning", "unknown issue")
        await channel.send(
            f"⚠️ **FHB Comp Bot** — Address parsed as `{lead['address']}` but it looks incomplete ({reason}).\n"
            f"If that's wrong, use `/comp [full address]` to run manually."
        )
        return

    # Soft warning (e.g. no state detected) — run but flag it
    if lead.get("address_warning"):
        await channel.send(
            f"⚠️ **FHB Comp Bot** — Address parsed as `{lead['address']}` — note: {lead['address_warning']}\n"
            f"Running comp anyway. Use `/comp [full address]` to override if incorrect."
        )

    await post_comp_report(channel, lead)


# ── Slash Commands ────────────────────────────────────────────────────────────

@bot.tree.command(name="comp", description="Run a comp analysis on a property address")
async def comp_slash(
    interaction: discord.Interaction,
    address: str,
    asking_price: str = "unknown",
    seller_name: str = "unknown",
):
    await interaction.response.send_message(
        f"🔍 Running comp for `{address}` (Asking: `{asking_price}`)..."
    )
    lead = {
        "address": address,
        "asking_price_raw": asking_price,
        "seller_name": seller_name,
    }
    # Try to parse numeric asking price
    numeric = re.sub(r"[^\d.]", "", asking_price)
    if numeric:
        lead["asking_price"] = float(numeric)
    await post_comp_report(interaction.channel, lead)


@bot.tree.command(name="recomp", description="Re-run comp analysis using this channel's Tickety data")
async def recomp_slash(interaction: discord.Interaction):
    await interaction.response.send_message("🔄 Re-pulling lead data and running fresh comp...")
    lead = await extract_lead_data(interaction.channel)
    if not lead or not lead.get("address"):
        await interaction.followup.send(
            "❌ Couldn't find address in this channel. Use `/comp [address]` instead."
        )
        return
    await post_comp_report(interaction.channel, lead)


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
