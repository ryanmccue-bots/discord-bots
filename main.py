"""FastAPI app: password gate, JSON API, and the dashboard itself."""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import secrets
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, time as dtime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer

import funnel as funnel_mod
import response as response_mod
import hashlib
import leadcsv
from googleclient import GoogleAdsClient, GoogleAdsError
from collector import Collector
from config import load_settings
from ghlclient import GHLClient
from ifclient import InvestorFuseClient
from metaclient import MetaClient, MetaError, leads_from
import metaclient
import metacopy
import accounts
import deals as deals_mod
import revenue as revenue_mod
import staff as staff_mod
from normalize import (
    CLAIMED_PARTS,
    DEFAULT_HIDDEN_METRICS,
    CLOSER_METRICS,
    LEAD_TREND_METRICS,
    METRIC_LABELS,
    DURATION_METRICS,
    ROLE_ORDER,
    TEAM_ROLES,
    REVENUE_EXTRA_PEOPLE,
    TREND_METRICS,
    VA_METRICS,
    display_name,
    is_real_user,
    _user_of as normalize_user_of,
    parse_sms_user_map,
    role_of,
    sms_user_email,
    with_claimed,
)
from normalize import TEAM_NAMES as normalize_team_names
from store import Store

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger("fhb-kpi")

BASE_DIR = Path(__file__).parent
COOKIE_NAME = "fhb_kpi_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

settings = load_settings()
store = Store(settings.database_url, settings.sqlite_path)
store.if_cutover_day = settings.ghl_cutover or None
# The roster comes from the Staff tab. Loaded before anything that reads
# TEAM_ROLES starts, so the first poll already counts the right people.
try:
    staff_mod.seed(store)
except Exception:  # noqa: BLE001 - the built-in roster still works
    log.exception("could not seed the staff table")
staff_mod.apply(store)
client = InvestorFuseClient(
    refresh_token=settings.refresh_token,
    firebase_api_key=settings.firebase_api_key,
    team_id=settings.team_id,
    api_base=settings.api_base,
    timeout=settings.request_timeout,
)
# GoHighLevel, when configured, is where leads come from. The InvestorFuse
# client above is still built either way - its diagnostic endpoints just
# report an error once its credentials are gone - but the collector only
# calls it while InvestorFuse is configured.
ghl_client = (GHLClient(settings.ghl_token, settings.ghl_location_id,
                        timeout=settings.request_timeout)
              if settings.ghl_configured else None)
import googleupload
conversion_sync = googleupload.ConversionSync(
    store, googleupload.DataManagerClient(settings.google_service_account_json,
                                          timeout=settings.request_timeout), settings)
collector = Collector(client, store, settings, ghl=ghl_client, uploader=conversion_sync)


def lead_url(lead: dict) -> str:
    """Where a lead row links to: the GHL contact, or the InvestorFuse opp.

    GHL rows link to the contact rather than the opportunity, because the
    contact page shows the opportunity, the conversation and the attribution
    together - which is what anyone clicking through actually wants.
    """
    if lead.get("crm") == "ghl":
        base = f"{settings.ghl_app_base}/v2/location/{settings.ghl_location_id}"
        if lead.get("contact_id"):
            return f"{base}/contacts/detail/{lead['contact_id']}"
        return f"{base}/opportunities/list"
    return (f"{settings.if_app_base}/app/teams/{settings.team_id}"
            f"/opportunities/{lead['id']}")
signer = URLSafeTimedSerializer(settings.session_secret, salt="fhb-kpi-login")

# Crude but effective brute-force brake for a single shared password.
_login_attempts: dict[str, list[float]] = defaultdict(list)
MAX_ATTEMPTS = 10
ATTEMPT_WINDOW = 300  # seconds


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Correcting the sender mapping should fix the texts already collected,
    # not just the next ones, so it is re-applied on every boot.
    try:
        changed = store.reattribute_sms(lambda name: sms_user_email(name, SMS_USER_MAP))
        if changed:
            for day in store.sms_days():
                _sync_sms_day(day)
            log.info("Re-attributed texts for %d sender(s)", changed)
    except Exception:  # noqa: BLE001 - never let this stop the app booting
        log.exception("Could not re-attribute stored texts")
    # Roles live in the environment, so the roster is reconciled every boot.
    _sync_revenue_roster()
    collector.start()
    yield
    collector.stop()


app = FastAPI(title="FHB KPI Dashboard", lifespan=lifespan, docs_url=None, redoc_url=None)

# No static mount on purpose. Both pages inline all their own CSS and JS, and
# in this flat layout the project root holds the source files - mounting it
# would serve config.py and friends over HTTP.


# ----------------------------------------------------------------------
# Auth helpers
# ----------------------------------------------------------------------
def _is_authed(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    try:
        signer.loads(token, max_age=SESSION_MAX_AGE)
        return True
    except BadSignature:
        return False
    except Exception:  # noqa: BLE001 - expired signature and friends
        return False


def _same_secret(given: str, expected: str) -> bool:
    """Constant-time compare that survives non-ASCII input.

    secrets.compare_digest raises TypeError on a str containing non-ASCII,
    which turned a password or webhook token with an accent in it into an
    unhandled 500 on public endpoints. Comparing the UTF-8 bytes keeps the
    timing property and accepts anything.
    """
    return secrets.compare_digest((given or "").encode("utf-8"),
                                  (expected or "").encode("utf-8"))


def _client_ip(request: Request) -> str:
    """Who is asking, as well as can be known behind Railway's proxy.

    ``request.client.host`` is the PROXY on Railway, identical for everybody,
    which quietly turned every per-IP limit below into a single global one:
    five wrong PINs from anyone locked the whole company out of signing in.
    The real address is the first entry of X-Forwarded-For.

    That header is client-supplied, so an attacker can rotate it and slip the
    per-IP limit. That is the deliberate trade: the per-IP counter exists to
    slow down one guesser, and ``PIN_ACCOUNT_GLOBAL_MAX`` is the backstop that
    still stops a spread-out attack. Trusting it costs an evadable limit;
    not trusting it costs Alec being unable to sign in because a stranger
    guessed wrong five times.
    """
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else "?"


def _rate_limited(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _login_attempts[ip] if now - t < ATTEMPT_WINDOW]
    _login_attempts[ip] = attempts
    return len(attempts) >= MAX_ATTEMPTS


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    # app.css / app.js carry no data - the login page needs them too.
    # The SMS webhook has no session to present - smrtPhone's servers call it.
    # Its own secret in the path is what authenticates it, checked in the route.
    public = (path in ("/login", "/healthz", "/favicon.ico", "/app.css", "/app.js")
              or path.startswith("/hooks/sms/")
              or path.startswith("/hooks/deal/")
              or path.startswith("/hooks/ghl/")
              or path.startswith("/hooks/ghl-call/"))
    if not public and not _is_authed(request):
        if path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse("/login", status_code=302)
    return await call_next(request)


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    # Public, so it says whether the last pull worked and nothing else. The
    # stored error text is upstream response bodies verbatim - team ids,
    # refusal messages, and one Firebase response-shape change away from a
    # live token. The detail is still on /api/sms-status and in the banner,
    # both of which are behind the site password.
    return {
        "ok": True,
        "last_success": store.get_meta("last_success"),
        "healthy": not store.get_meta("last_error"),
    }


# ----------------------------------------------------------------------
# Text messages, pushed in by smrtPhone
#
# InvestorFuse counts calls and talk time but has no SMS counter at all, so
# texts cannot arrive with everything else on the 15-minute poll. smrtPhone
# posts each message here as it is sent instead.
#
# Consequences worth remembering: there is no history to backfill, so counts
# begin the day the webhook is switched on, and a text only appears if
# smrtPhone knew who sent it - see sms_user_email for the name matching.
# ----------------------------------------------------------------------
SMS_USER_MAP = parse_sms_user_map(settings.sms_user_map)

# smrtPhone's payload keys are not documented and have changed at least once,
# so every field is looked up under the spellings it plausibly uses rather
# than one exact name. An unrecognised payload is still recorded.
_SMS_FIELDS = {
    "id": ("id", "sms_id", "smsId", "message_id", "messageId", "sid", "uuid"),
    "user": ("userName", "user_name", "user", "agent", "agentName",
             "agent_name", "from_user", "fromUser", "owner", "callerIdName"),
    "from": ("from", "from_number", "fromNumber", "sender", "fromPhone"),
    "date": ("date", "dateSent", "date_sent", "created_at", "createdAt",
             "timestamp", "sent_at", "sentAt"),
    "direction": ("direction", "type", "event", "eventType", "event_type"),
}


def _sms_field(payload: dict, which: str) -> str:
    for key in _SMS_FIELDS[which]:
        value = payload.get(key)
        if value not in (None, "", []):
            return str(value).strip()
    return ""


def _sms_day(raw: str) -> str:
    """The report-timezone day a text belongs to.

    smrtPhone documents ``date`` as YYYYMMDD but sends ISO timestamps and epoch
    values in other places, so all three are accepted. Anything unparseable
    falls back to today, which keeps the count right even when the format
    changes - the message is never dropped for having an odd date.
    """
    text = (raw or "").strip()
    digits = "".join(c for c in text if c.isdigit())
    if len(digits) == 8 and text[:8].isdigit():
        try:
            return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8])).isoformat()
        except ValueError:
            pass
    if text:
        try:
            stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if stamp.tzinfo is not None:
                stamp = stamp.astimezone(settings.tz)
            return stamp.date().isoformat()
        except ValueError:
            pass
        if digits and len(digits) >= 10:
            try:
                seconds = int(digits[:13]) / 1000 if len(digits) >= 13 else int(digits[:10])
                return datetime.fromtimestamp(seconds, settings.tz).date().isoformat()
            except (ValueError, OSError, OverflowError):
                pass
    return datetime.now(settings.tz).date().isoformat()


def _sms_direction(payload: dict) -> str:
    """"out" unless the payload clearly says the message came in."""
    hint = " ".join(
        str(payload.get(key, "")) for key in _SMS_FIELDS["direction"]
    ).lower()
    incoming = ("incoming", "inbound", "received", "in_bound")
    return "in" if any(word in hint for word in incoming) else "out"


def _sync_sms_day(day: str) -> None:
    """Recompute one day's Texts row from the stored events.

    Recomputed rather than incremented so that a webhook delivered twice, or a
    sender whose mapping was fixed later, both land on the right number.
    """
    # On days GHL owns, texts come from GHL's own message log. smrtPhone's
    # count written on top would overwrite it - and for one more day both
    # phone systems may be in use.
    if settings.ghl_owns_day(day):
        return
    counts = store.sms_counts(day)
    if counts:
        store.upsert_day(day, [("textMessages", email, value)
                               for email, value in counts])


# Keys GHL might put an opportunity id under. Custom Data first: if it was
# added to the webhook action it is the unambiguous one.
_GHL_OPP_ID_KEYS = ("opportunity_id", "opportunityId", "opportunityID", "opp_id")
# The stage name, as GHL's default payload spells it - "pipleline_stage" is
# GHL's own typo, documented that way - plus the obvious spellings.
_GHL_STAGE_KEYS = ("pipleline_stage", "pipeline_stage", "pipelineStage", "stage")


def _ghl_hook_fields(payload: dict) -> tuple[str | None, str | None]:
    """(opportunity id, stage name) from a GHL workflow webhook body."""
    merged = dict(payload)
    for wrapper in ("customData", "custom_data", "data", "payload"):
        inner = payload.get(wrapper)
        if isinstance(inner, dict):
            merged = {**merged, **inner}
    opp = next((str(merged[k]).strip() for k in _GHL_OPP_ID_KEYS
                if merged.get(k)), None)
    # The default payload puts the opportunity's own fields at the root,
    # including a bare "id". Only trust it when opportunity fields are there
    # too - on a contact-only payload that id would be the contact's.
    if not opp and merged.get("id") and any(
            k in merged for k in ("pipeline_id", "pipleline_stage",
                                  "pipeline_stage", "opportunity_name")):
        opp = str(merged["id"]).strip()
    stage = next((str(merged[k]).strip() for k in _GHL_STAGE_KEYS
                  if merged.get(k)), None)
    return opp, stage


@app.api_route("/hooks/ghl/{token}", methods=["GET", "POST"])
async def ghl_stage_webhook(token: str, request: Request):
    """GHL's "Pipeline Stage Changed" workflow posts here.

    GHL's webhook says a card moved, and little else reliable: no previous
    stage, no time of the move, and the "user" in it is the opportunity's
    owner rather than whoever dragged the card. So this reads the
    opportunity id and asks the GHL API where the opportunity is now and
    exactly when it got there. The 15-minute sync does the same comparison,
    so a webhook that never arrives costs precision, never data.
    """
    if not settings.ghl_webhook_token or not _same_secret(token, settings.ghl_webhook_token):
        return JSONResponse({"error": "not found"}, status_code=404)
    if request.method == "GET":
        return {"ok": True, "listening": True,
                "note": "Point the GHL Pipeline Stage Changed workflow's "
                        "Webhook action at this URL."}
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - any malformed body
        try:
            payload = dict(await request.form())
        except Exception:  # noqa: BLE001
            payload = {}
    if not isinstance(payload, dict):
        payload = {}

    now = datetime.now(settings.tz).isoformat()
    store.set_meta("ghl_last_hook", now)
    # The shape, never the values: the body carries a seller's name, phone
    # and email, and the field NAMES are all that's needed to debug a
    # mapping. Readable on /api/ghl/verify.
    keys = sorted(payload.keys())
    for wrapper in ("customData", "custom_data"):
        if isinstance(payload.get(wrapper), dict):
            keys += [f"{wrapper}.{k}" for k in sorted(payload[wrapper])]
    store.set_meta("ghl_last_hook_keys", json.dumps(keys)[:2000])

    opp_id, hook_stage = _ghl_hook_fields(payload)
    if not opp_id:
        store.set_meta("ghl_last_hook_error", f"{now}: no opportunity id in payload")
        # 200, not 4xx: a retry would carry the same body and fail the same way.
        return {"ok": False, "error": "no opportunity id found",
                "keysSeen": keys[:40]}
    if ghl_client is None:
        return {"ok": False, "error": "GHL_TOKEN / GHL_LOCATION_ID not set"}

    try:
        import asyncio
        row = await asyncio.to_thread(collector.ingest_opportunity, opp_id)
    except Exception as exc:  # noqa: BLE001 - never 500 at GHL
        log.warning("GHL webhook: could not read opportunity %s: %s", opp_id, exc)
        store.set_meta("ghl_last_hook_error", f"{now}: {exc}"[:500])
        return {"ok": False, "opportunity": opp_id, "error": str(exc)[:200]}

    if row is None:
        store.set_meta("ghl_last_hook_error", f"{now}: opportunity {opp_id} not found")
        return {"ok": False, "opportunity": opp_id, "error": "opportunity not found"}

    # The payload's stage is what GHL saw when it fired; the API's is now.
    # They differ only when the card moved again in between - worth counting,
    # because that is the one case where an intermediate stage goes unrecorded.
    if hook_stage and row.get("substage") and hook_stage.strip().lower() != row["substage"].strip().lower():
        n = int(store.get_meta("ghl_hook_stage_mismatch") or 0) + 1
        store.set_meta("ghl_hook_stage_mismatch", str(n))
    store.set_meta("ghl_last_hook_error", "")
    return {"ok": True, "opportunity": opp_id, "stage": row.get("substage")}


@app.api_route("/hooks/ghl-call/{token}", methods=["GET", "POST"])
async def ghl_call_webhook(token: str, request: Request):
    """GHL's "call ended" workflow posts here; the call-in gets an owner.

    Same secret as the stage webhook. Reads only the contact id from the
    body, then asks GHL for that seller's calls and opportunities - so a
    mis-mapped payload can't assign anything wrong, only nothing at all.
    """
    if not settings.ghl_webhook_token or not _same_secret(token, settings.ghl_webhook_token):
        return JSONResponse({"error": "not found"}, status_code=404)
    assigner = getattr(collector, "call_assigner", None)
    if request.method == "GET":
        return {"ok": True, "listening": True,
                "mode": assigner.mode if assigner else "unavailable"}
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        try:
            payload = dict(await request.form())
        except Exception:  # noqa: BLE001
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    merged = {**payload}
    for wrapper in ("customData", "custom_data", "contact"):
        if isinstance(payload.get(wrapper), dict):
            merged = {**payload[wrapper], **merged}
    contact_id = next((str(merged[k]).strip() for k in ("contact_id", "contactId")
                       if merged.get(k)), None)
    if not contact_id and isinstance(payload.get("contact"), dict):
        contact_id = str(payload["contact"].get("id") or "").strip() or None
    store.set_meta("ghl_last_call_hook", datetime.now(settings.tz).isoformat())
    if not contact_id:
        return {"ok": False, "error": "no contact id in payload", "keysSeen": sorted(payload)[:40]}
    if assigner is None or assigner.mode not in ("dry", "live"):
        return {"ok": True, "skipped": "CALL_ASSIGN_MODE is off"}

    async def _later(cid: str) -> None:
        import asyncio
        # GHL writes the call's length and who answered a few seconds after
        # the workflow fires; asking straight away can find the call unfinished.
        await asyncio.sleep(20)
        try:
            await asyncio.to_thread(assigner.run_contact, cid)
        except Exception as exc:  # noqa: BLE001
            log.warning("call webhook: assignment failed for a contact: %s", exc)

    import asyncio
    asyncio.create_task(_later(contact_id))
    return {"ok": True, "queued": True}


@app.api_route("/hooks/sms/{token}", methods=["GET", "POST"])
async def sms_webhook(token: str, request: Request):
    # Constant-time compare, and a 404 rather than a 403 so a wrong token
    # cannot be told apart from the feature being switched off.
    if not settings.sms_webhook_token or not _same_secret(token, settings.sms_webhook_token
    ):
        return JSONResponse({"error": "not found"}, status_code=404)

    # A browser visit, for checking the URL is right before wiring it up.
    if request.method == "GET":
        return {"ok": True, "listening": True,
                "note": "Point smrtPhone's Outgoing SMS webhook at this URL."}

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - any malformed body, not just JSON errors
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    # Some senders wrap the event in {"data": {...}} or {"payload": {...}}.
    for wrapper in ("data", "payload", "body", "message"):
        inner = payload.get(wrapper)
        if isinstance(inner, dict) and not _sms_field(payload, "id"):
            payload = {**payload, **inner}

    sender = _sms_field(payload, "user")
    direction = _sms_direction(payload)
    day = _sms_day(_sms_field(payload, "date"))
    email = sms_user_email(sender, SMS_USER_MAP)
    # The id is what makes a repeat delivery harmless. Without one from
    # smrtPhone there is no way to tell a retry from a second text, so a
    # substitute is generated - marked "gen:" so /api/sms-status can report
    # how many events are counted on trust rather than on an id.
    real_id = _sms_field(payload, "id")
    event_id = real_id or (
        f"gen:{day}:{sender}:{_sms_field(payload, 'from')}:{secrets.token_hex(8)}"
    )

    fresh = store.record_sms(
        event_id, day, sender or "(unknown)", email,
        _sms_field(payload, "from"), direction,
    )
    if fresh and direction == "out" and email:
        _sync_sms_day(day)
    if fresh and not email and direction == "out":
        log.warning("SMS from unmapped sender %r - see /api/sms-status", sender)

    store.set_meta("sms_last_hook", datetime.now(settings.tz).isoformat())
    return {"ok": True, "counted": bool(fresh and email and direction == "out"),
            "day": day, "sender": sender or None, "matched": email or None}


@app.get("/api/sms-status")
async def api_sms_status():
    """Is the webhook working, and is every sender recognised?

    The one place to look when the Texts row is empty or a name is missing.
    """
    summary = store.sms_summary()
    summary["enabled"] = bool(settings.sms_webhook_token)
    summary["lastHook"] = store.get_meta("sms_last_hook")
    summary["unmapped"] = sorted(
        {s["name"] for s in summary["senders"]
         if not s["email"] and s["direction"] == "out"}
    )
    return summary


@app.get("/login", response_class=HTMLResponse)
async def login_page(error: str = ""):
    html = (BASE_DIR / "login.html").read_text(encoding="utf-8")
    banner = (
        '<p class="error" role="alert">Incorrect password.</p>' if error else ""
    )
    return HTMLResponse(html.replace("<!--ERROR-->", banner))


@app.post("/login")
async def login(request: Request, password: str = Form("")):
    ip = _client_ip(request)
    if _rate_limited(ip):
        return RedirectResponse("/login?error=1", status_code=302)
    _login_attempts[ip].append(time.time())

    if _same_secret(password, settings.dashboard_password):
        response = RedirectResponse("/", status_code=302)
        response.set_cookie(
            COOKIE_NAME,
            signer.dumps({"ts": time.time()}),
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=settings.cookie_secure,
        )
        _login_attempts[ip].clear()
        return response
    return RedirectResponse("/login?error=1", status_code=302)


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=302)
    # Every gate, not just the site session. Clearing only the site cookie
    # left the Revenue identity (14 days) and the Meta unlock (12 hours)
    # behind, so on a shared machine the next person to type the site
    # password inherited whoever last used it - admin, company revenue and
    # all. That is precisely what the second factors exist to prevent.
    response.delete_cookie(COOKIE_NAME)
    response.delete_cookie(USER_COOKIE)
    response.delete_cookie(META_COOKIE)
    return response


@app.get("/", response_class=HTMLResponse)
async def overview_page():
    # Through _page like every other page, so it gets the report-timezone tag.
    return _page("overview.html")


def _freshness() -> dict:
    # Texts from a sender nobody recognises are the one fault that looks like
    # good news: the count just reads zero, the way a quiet week does. Joy's
    # went uncounted for days before anyone noticed the box. Say it out loud.
    unmapped: list[str] = []
    if settings.sms_webhook_token:
        try:
            since = (datetime.now(settings.tz).date() - timedelta(days=14)).isoformat()
            unmapped = [name for name, _count in store.sms_unmapped_since(since)]
        except Exception:  # noqa: BLE001 - a warning must never break the page
            log.exception("Could not check for unmapped text senders")
    return {
        "lastSuccess": store.get_meta("last_success"),
        "lastError": store.get_meta("last_error") or None,
        "lastErrorAt": store.get_meta("last_error_at"),
        "smsUnmapped": unmapped,
        "pollMinutes": settings.poll_minutes,
        "timezone": str(settings.tz),
        "serverTime": datetime.now(settings.tz).isoformat(),
    }


def _parse_day(value: str | None, fallback: date) -> date:
    if not value:
        return fallback
    try:
        return date.fromisoformat(value)
    except ValueError:
        return fallback


def _range(request: Request, default_days: int = 30) -> tuple[date, date]:
    """The ``from``/``to`` pair a request is asking about.

    Missing or unparseable dates fall back to the last ``default_days``, and a
    backwards range is swapped rather than returning nothing - somebody picking
    the end date first should see data, not an empty page.
    """
    today = datetime.now(settings.tz).date()
    day_to = _parse_day(request.query_params.get("to"), today)
    day_from = _parse_day(
        request.query_params.get("from"), day_to - timedelta(days=default_days - 1)
    )
    return (day_to, day_from) if day_from > day_to else (day_from, day_to)


@app.get("/api/kpis")
async def kpis(request: Request):
    today = datetime.now(settings.tz).date()
    day_to = _parse_day(request.query_params.get("to"), today)
    day_from = _parse_day(
        request.query_params.get("from"), day_to - timedelta(days=29)
    )
    if day_from > day_to:
        day_from, day_to = day_to, day_from

    rows = _scorecard_rows(day_from, day_to, with_old=True)

    by_user: dict[str, dict[str, float]] = {}
    totals: dict[str, float] = {}
    by_day: dict[str, dict[str, float]] = {}

    for day, user, metric, value in rows:
        by_user.setdefault(user, {})
        by_user[user][metric] = by_user[user].get(metric, 0.0) + value
        if metric.startswith("old."):
            continue    # per-person split only; never a total or a trend line
        totals[metric] = totals.get(metric, 0.0) + value
        by_day.setdefault(day, {})
        by_day[day][metric] = by_day[day].get(metric, 0.0) + value

    # Every day in range appears, so a zero day is a visible gap in the trend
    # rather than a line that quietly skips it.
    daily = []
    cursor = day_from
    while cursor <= day_to:
        iso = cursor.isoformat()
        daily.append({"day": iso, "metrics": by_day.get(iso, {})})
        cursor += timedelta(days=1)

    # Only people with a role get a column. The API also reports the owner's
    # own login and whoever is signed in looking at the dashboard - real
    # accounts with a little incidental activity, but not people whose numbers
    # anyone manages. Give someone a role in TEAM_ROLES and they appear.
    # The top row is COHORT data, not activity data. The scorecard counts a
    # contact whenever a rep marks one, however old the lead - so "contacted"
    # could exceed "new leads" for the same window. These numbers instead ask:
    # of the leads CREATED in this range, how many have been contacted, got an
    # offer, went under contract? That can't exceed 100%, and it matches the
    # opportunities list exactly, since both filter leads by created date.
    cohort_leads = store.leads_between(day_from.isoformat(), day_to.isoformat())
    cohort = funnel_mod.build(cohort_leads)

    # Split by owner once, so a lead manager's card can borrow the numbers from
    # the closer they book for without rebuilding the funnel per person.
    cohort_by_owner: dict[str, list[dict]] = {}
    for lead in cohort_leads:
        cohort_by_owner.setdefault(
            (lead.get("owner_id") or "").strip().lower(), []).append(lead)

    reps = []
    tracked_totals: dict[str, float] = {}
    # Activity from logins that are real people but have no role in
    # TEAM_ROLES. It is deliberately excluded from the team totals - those sum
    # the columns actually shown, so the row adds up on screen - but it is NOT
    # thrown away. Dropping it silently is how the scorecard came to read lower
    # than the cohort tiles above it with nothing on the page to explain why.
    untracked: dict[str, dict[str, float]] = {}
    # Everyone with a role gets a row, even on a day with nothing logged: a
    # person who silently vanishes reads as "not on the team", when zeros say
    # "nothing counted for them" - and that is the thing worth chasing.
    roster = {**{email: {} for email in TEAM_ROLES}, **by_user}
    for email, raw in roster.items():
        info = role_of(email)
        metrics = {k: v for k, v in raw.items() if not k.startswith("old.")}
        if info.get("role", "other") == "other":
            enriched = with_claimed(metrics)
            if any(enriched.get(key) for key in CLOSER_METRICS + VA_METRICS):
                untracked[email] = enriched
            continue
        enriched = with_claimed(metrics)
        for key, value in enriched.items():
            tracked_totals[key] = tracked_totals.get(key, 0.0) + value
        # Work on imported old leads is stored again as "old.<metric>"; the
        # plain metrics are the total. Split for the two Overview scorecards.
        old_part = {k[4:]: v for k, v in raw.items() if k.startswith("old.")}
        new_part = {k: max(0.0, v - old_part.get(k, 0.0)) for k, v in metrics.items()}
        reps.append({
            "metricsNew": with_claimed(new_part),
            "metricsOld": with_claimed(old_part),
            "email": email,
            "name": display_name(email),
            "role": info.get("role", "other"),
            "title": info.get("title", ""),
            "booksFor": display_name(info["books_for"]) if info.get("books_for") else None,
            "metrics": enriched,
            # Kept out of `metrics`, which is all counts that get summed into
            # the Team column - this carries an average and must not be.
            "callAttempted": _call_attempted(
                cohort_by_owner.get(info["books_for"], [])
            ) if info.get("books_for") else None,
        })
    # VAs first, then closers, then everyone else; busiest first within a role.
    reps.sort(key=lambda r: (ROLE_ORDER.get(r["role"], 2),
                             -r["metrics"].get("userTouches", 0)))

    # Team is the sum of the columns shown, so the row adds up on screen. An
    # untracked login's activity is left out of both.
    totals = with_claimed(tracked_totals)

    # Whose work is missing from the scorecard, and how much of it. The page
    # shows this as a note rather than a row: these people have no role, so
    # there is no column set that would be the right one to show them in.
    untracked_summary = sorted(
        ({"email": email,
          "name": display_name(email),
          "metrics": {k: v for k, v in metrics.items()
                      if v and k in set(CLOSER_METRICS + VA_METRICS)}}
         for email, metrics in untracked.items()),
        key=lambda row: row["name"] or row["email"],
    )

    return {
        "range": {"from": day_from.isoformat(), "to": day_to.isoformat()},
        "totals": totals,
        "reps": reps,
        "untracked": untracked_summary,
        "daily": daily,
        "cohort": {
            "total": cohort["total"],
            "contacted": cohort["contacted"],
            "contactRate": cohort["contactedPct"],
            "offers": cohort["offers"],
            "contracts": cohort["contracts"],
            "contractsSent": _contracts_sent_count(cohort_leads),
            "closed": cohort["closed"],
            "qualified": cohort["buckets"].get("qualified", 0),
            "crm": cohort["crm"],
        },
        "response": _response_block(cohort_leads, day_from, day_to),
        "metricLabels": METRIC_LABELS,
        "defaultHidden": DEFAULT_HIDDEN_METRICS,
        "roleMetrics": {"va": VA_METRICS, "closer": CLOSER_METRICS},
        "durationMetrics": sorted(DURATION_METRICS),
        "freshness": _freshness(),
    }


# ----------------------------------------------------------------------
# Speed to lead, needs attention, missed Google calls (see response.py)
# ----------------------------------------------------------------------
def _people_maps() -> tuple[dict, dict]:
    """``({user_id: email}, {phone: email})`` - the collector's merged view."""
    if ghl_client is None:
        return {}, {}
    try:
        _names, ids, numbers = collector._reference()
        return ids, numbers
    except Exception as exc:  # noqa: BLE001 - names are nice-to-have here
        log.warning("could not read GHL users for response metrics: %s", exc)
        return {}, {}


def _history_start_ms() -> int | None:
    """When GHL call and text history begins - the activity cutover."""
    day = settings.ghl_activity_cutover
    if not day:
        return None
    try:
        return int(datetime.combine(date.fromisoformat(day), dtime.min)
                   .replace(tzinfo=settings.tz).timestamp() * 1000)
    except ValueError:
        return None


def _speed(leads: list[dict]) -> dict | None:
    ghl_leads = [l for l in leads if l.get("crm") == "ghl"]
    if not settings.ghl_configured or not ghl_leads:
        return None
    ids, numbers = _people_maps()
    msgs = store.messages_for_contacts(l.get("contact_id") for l in ghl_leads)
    out = response_mod.speed_to_lead(
        ghl_leads, msgs, ids, numbers, settings.tz,
        (settings.business_open, settings.business_close), _history_start_ms())
    out["byRep"] = [{"email": e, "name": display_name(e), **s}
                    for e, s in sorted(out["byRep"].items(),
                                       key=lambda kv: -kv[1]["n"])
                    if e in TEAM_ROLES]
    return out


def _response_block(cohort_leads: list[dict], day_from: date, day_to: date) -> dict | None:
    """Everything the Overview's response section shows. None before GHL.

    Never takes the page down with it: a failure here comes back as an error
    for the section to show, and the tiles and scorecard render regardless.
    """
    if not settings.ghl_configured:
        return None
    try:
        return _response_block_inner(cohort_leads, day_from, day_to)
    except Exception as exc:  # noqa: BLE001
        log.exception("response metrics failed")
        return {"error": str(exc)[:300]}


def _response_block_inner(cohort_leads: list[dict], day_from: date, day_to: date) -> dict:
    tz = settings.tz
    hours = (settings.business_open, settings.business_close)
    now_ms = int(datetime.now(tz).timestamp() * 1000)
    out: dict[str, Any] = {"speed": _speed(cohort_leads), "hours": list(hours)}

    # Needs attention is "as of now", whatever range is picked.
    open_leads = [l for l in store.all_leads()
                  if l.get("crm") == "ghl" and not l.get("dead_reason")]
    msgs = store.messages_for_contacts(l.get("contact_id") for l in open_leads)
    last_stage = store.last_stage_times([l["id"] for l in open_leads])
    attention = response_mod.needs_attention(
        open_leads, msgs, last_stage, now_ms, tz,
        settings.stuck_new_minutes, settings.stuck_qualified_days, hours,
        _history_start_ms())
    for group in ("untouched", "stalled"):
        for row in attention[group]:
            row["ownerName"] = display_name(row["owner"]) if row.get("owner") else None
            row["url"] = lead_url({"crm": "ghl", "contact_id": row.get("contactId")})
            row.pop("contactId", None)
    out["attention"] = attention

    # Missed calls in the picked range; callbacks counted up to now.
    start_ms = int(datetime.combine(day_from, dtime.min).replace(tzinfo=tz).timestamp() * 1000)
    end_ms = int(datetime.combine(day_to, dtime.max).replace(tzinfo=tz).timestamp() * 1000)
    missed = response_mod.missed_calls(
        store.messages_between(start_ms, now_ms), settings.ghl_google_number,
        start_ms, end_ms, tz, hours)
    by_contact = {l.get("contact_id"): l for l in store.all_leads(include_old=True)
                  if l.get("crm") == "ghl" and l.get("contact_id")}
    for row in missed["open"]:
        lead = by_contact.get(row.get("contactId"))
        row["sellerName"] = (lead or {}).get("seller_name")
        row["url"] = (lead_url({"crm": "ghl", "contact_id": row["contactId"]})
                      if row.get("contactId") else None)
        row.pop("contactId", None)
    out["missed"] = missed
    out["googleNumber"] = settings.ghl_google_number[-4:]

    # Old leads (the InvestorFuse import) that moved forward in the range.
    moves = store.stage_moves_between(start_ms, end_ms)
    progress = response_mod.old_lead_progress(
        moves, store.leads_by_id(list(moves)), start_ms, end_ms)
    for row in progress["leads"]:
        row["ownerName"] = display_name(row["owner"]) if row.get("owner") else None
        row["url"] = lead_url({"crm": "ghl", "contact_id": row.get("contactId")})
        row.pop("contactId", None)
    progress["stored"] = store.old_lead_summary()["stored"]
    out["oldLeads"] = progress
    return out


@app.get("/api/funnel")
async def api_funnel(request: Request):
    """The monthly report's funnel, for any date range."""
    today = datetime.now(settings.tz).date()
    default_from = today.replace(day=1)
    day_to = _parse_day(request.query_params.get("to"), today)
    day_from = _parse_day(request.query_params.get("from"), default_from)
    if day_from > day_to:
        day_from, day_to = day_to, day_from

    leads = store.leads_between(day_from.isoformat(), day_to.isoformat())
    payload = funnel_mod.build(
        leads, resurfaced=collector.resurfaced_in(day_from, day_to)
    )
    payload["range"] = {"from": day_from.isoformat(), "to": day_to.isoformat()}
    payload["repNames"] = {
        owner: display_name(owner) for owner in payload.get("owners", {})
    }
    payload["freshness"] = {**_freshness(), "leadsStored": store.lead_count()}
    payload["months"] = store.months()
    return payload



@app.post("/api/funnel/month/{month}/rebuild")
async def api_rebuild_month(month: str, request: Request):
    """Recompute a frozen monthly archive from the leads still on record.

    Admin only. Everything else behind the shared site password is a read;
    this one overwrites a frozen record permanently and there is no undo, so
    it needs to be an identified person rather than whoever has the password.

    Archives are normally written once and left alone - that is the point of
    freezing them. This exists because the definition of an offer changed
    after August was archived: the old count included leads parked in the
    offer sub-stage with no offer recorded, so the stored 43 is wrong rather
    than merely old, and a wrong number nobody can correct is worse than a
    rewritten one.

    It recomputes from the lead table as it stands today, so a lead whose
    stage has moved since will be counted where it is now, not where it was in
    August. For a closed month that is a small effect and the honest trade.
    """
    blocked = _admin_only(request)
    if blocked:
        return blocked
    if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
        return JSONResponse({"error": "month must look like 2026-08"},
                            status_code=400)
    before = store.get_month(month) or {}
    rebuilt = collector.archive_month(month)
    if not rebuilt:
        return JSONResponse(
            {"error": f"no leads on record for {month}, nothing to rebuild"},
            status_code=404)
    after = store.get_month(month) or {}
    return {
        "month": month,
        "offersBefore": before.get("offers"),
        "offersAfter": after.get("offers"),
        "offerStage": after.get("offerStage"),
        "contractsBefore": before.get("contracts"),
        "contractsAfter": after.get("contracts"),
        "totalBefore": before.get("total"),
        "totalAfter": after.get("total"),
    }


@app.get("/api/funnel/month/{month}")
async def api_funnel_month(month: str):
    """A frozen monthly archive, exactly as it stood when the month closed."""
    payload = store.get_month(month)
    if payload is None:
        return JSONResponse({"error": "no archive for that month"}, status_code=404)
    payload["repNames"] = {
        owner: display_name(owner) for owner in payload.get("owners", {})
    }
    return payload


def _admin_only(request: Request) -> JSONResponse | None:
    """Refuse anyone who is not a signed-in admin.

    These return field names, sample projections and upstream error text.
    They sat behind the shared site password, which everyone on the team has
    - and one of them contradicted its own docstring by returning a whole
    projected lead, seller name and address included.
    """
    user = _current_user(request)
    if not user or user["role"] != accounts.ROLE_ADMIN:
        return JSONResponse(
            {"error": "This diagnostic is for admins. Sign in on the Revenue "
                      "tab first."}, status_code=403)
    return None


@app.get("/api/debug/opportunities")
async def api_debug_opportunities(request: Request):
    """Non-personal diagnostics for the lead feed. Admin only.

    Field names and the vocabulary of the classification fields only - no
    seller names, phones, emails or addresses. Use this to confirm the funnel
    is reading what we think it is, or to spot a field Carrot has renamed.
    """
    blocked = _admin_only(request)
    if blocked:
        return blocked
    try:
        payload = client.opportunities(page=1, limit=25)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=502)

    items, meta = client.unwrap(payload)
    interesting = ("stage", "substage", "contacted_type", "action_dead_reason",
                   "action_dead_reason_type", "created_type", "already_dead")
    vocab: dict[str, list] = {}
    for item in items:
        for key in interesting:
            value = item.get(key)
            if value is None or isinstance(value, (dict, list)):
                continue
            vocab.setdefault(key, [])
            if str(value) not in vocab[key] and len(vocab[key]) < 12:
                vocab[key].append(str(value))

    sample = funnel_mod.project(items[0], settings.tz) if items else None
    return {
        "envelopeKeys": sorted(payload.keys()) if isinstance(payload, dict) else "array",
        "meta": meta,
        "itemsReturned": len(items),
        "fieldCount": len(items[0]) if items else 0,
        # Every field name the feed actually carries, names only - no values,
        # same contract as the rest of this endpoint. `fieldCount` on its own
        # cannot answer "is Carrot sending this, and what do they call it?",
        # which is the question that matters whenever a panel reads a field
        # straight off the item and then renders nothing.
        "rawFieldNames": sorted({k for item in items for k in item}),
        "vocabulary": vocab,
        "projectedSample": {k: v for k, v in (sample or {}).items()},
        "leadsStored": store.lead_count(),
    }


# Sub-resources an opportunity might hang its activity log off. InvestorFuse
# records a returning seller's new form fill as another entry in that log
# rather than as a new opportunity, and nothing on the opportunity itself
# changes - so the count of "webform submission" entries is the only place
# the fact exists. None of these paths are documented; the probe below finds
# out which one is real instead of the code guessing, because guessing a
# field name is what made the last two attempts at this silently wrong.
#
# The first sweep returned "IF v2: Invalid API Request" for every
# sub-resource and a different, well-formed "Opportunity not found" for
# `opportunities/{id}` - the router rejecting a path it does not have,
# versus the handler rejecting an id it cannot find. That is worth keeping
# apart in the output, so `_classify` below does.
#
# The second clue came from a URL: InvestorFuse links a seller as
# /app/teams/{team}/contacts/{contact_id}, so a CONTACT is a first-class
# record and not the same thing as an opportunity. The activity log in the
# screenshot was on a contact page, which is the likeliest reason every
# `opportunities/{id}/...` sub-path answered "no such route" - and probably
# also where the email and phone live that the reconcile could not find on
# the opportunity. Contacts are therefore probed too, and `?contact=` takes
# an id straight out of that URL.
ACTIVITY_PATHS = (
    # Contacts first, on the evidence above.
    ("contacts/{id}", None),
    ("contacts/{id}/activities", None),
    ("contacts/{id}/activity", None),
    ("contacts/{id}/timeline", None),
    ("contacts/{id}/events", None),
    ("contacts/{id}/communications", None),
    ("contacts/{id}/threads", None),
    ("contacts/{id}/submissions", None),
    ("contacts/{id}/opportunities", None),
    ("contacts", {"opportunity_id": "{id}"}),
    ("activities", {"contact_id": "{id}"}),
    # The single opportunity, first and most promising: list endpoints
    # routinely trim a record that the detail endpoint returns in full, log
    # and all.
    ("opportunities/{id}", None),
    ("opportunities/{id}", {"include": "activities"}),
    ("opportunities/{id}", {"expand": "activities,comments"}),
    ("opportunities/{id}", {"with": "activities"}),
    # Sub-resources.
    ("opportunities/{id}/activities", None),
    ("opportunities/{id}/activity", None),
    ("opportunities/{id}/activity-log", None),
    ("opportunities/{id}/events", None),
    ("opportunities/{id}/timeline", None),
    ("opportunities/{id}/communications", None),
    ("opportunities/{id}/messages", None),
    ("opportunities/{id}/notes", None),
    ("opportunities/{id}/comments", None),
    ("opportunities/{id}/threads", None),
    ("opportunities/{id}/history", None),
    ("opportunities/{id}/submissions", None),
    # Singular parent, which some of this API's routes use.
    ("opportunity/{id}/activities", None),
    ("opportunity/{id}", None),
    # Collections filtered by a query parameter rather than nested.
    ("activities", {"opportunity_id": "{id}"}),
    ("activities", {"opportunityId": "{id}"}),
    ("activity", {"opportunity_id": "{id}"}),
    ("events", {"opportunity_id": "{id}"}),
    ("comments", {"opportunity_id": "{id}"}),
    ("threads", {"opportunity_id": "{id}"}),
    ("communications", {"opportunity_id": "{id}"}),
    ("opportunity-activities", {"opportunity_id": "{id}"}),
    ("webform-submissions", {"opportunity_id": "{id}"}),
)


def _classify(error: str) -> str:
    """Tell a missing route apart from a route that simply disliked the id."""
    low = error.lower()
    if "invalid api request" in low:
        return "no such route"
    if "not found" in low:
        return "route exists, id not found"
    if "401" in low or "403" in low:
        return "route exists, not authorised"
    return "other"


@app.get("/api/debug/activity")
async def api_debug_activity(request: Request):
    """Find the endpoint that carries an opportunity's activity log.

    Behind the site password rather than the admin gate, unlike the other
    diagnostics here, because it deliberately returns no values at all - only
    which paths answer, how many entries they hold and what the fields are
    called. There is nothing in the output that belongs to a seller, and
    needing a second login to answer "does this endpoint exist" has kept this
    question unanswered for too long.

    Finding the id should not be the user's job, so there are three ways in
    and the fiddliest is last: ``?seller=ohrbeck`` looks the opportunity up
    by stored name, ``?id=`` takes one directly, and with neither it uses the
    most recent lead. An ``id`` that still contains the angle brackets from
    an example is treated as not supplied - the first run of this probed the
    literal string "<Chris Ohrbeck's opportunity id>", which answers nothing.
    """
    lead_id = (request.query_params.get("id") or "").strip()
    seller = (request.query_params.get("seller") or "").strip()
    # A contact id, copied out of an InvestorFuse URL. Takes precedence,
    # because a contact is the record the activity log actually hangs off.
    contact = (request.query_params.get("contact") or "").strip()
    how = "as given"
    if contact and "<" not in contact and " " not in contact:
        lead_id, how = contact, "contact id, as given"
    if lead_id.startswith("<") or "<" in lead_id or " " in lead_id:
        lead_id, how = "", "ignored a placeholder id"
    if not lead_id and seller:
        matches = store.lead_ids_by_seller(seller)
        if not matches:
            return JSONResponse(
                {"error": f"no stored lead whose seller name contains "
                          f"{seller!r}. Try a surname, or pass ?id=."},
                status_code=404)
        lead_id, how = matches[0], f"matched seller {seller!r}"
        if len(matches) > 1:
            how += f" ({len(matches)} matched, used the most recent)"
    if not lead_id:
        recent = store.recent_lead_ids(1)
        if not recent:
            return JSONResponse({"error": "no leads stored yet; pass ?id="},
                                status_code=404)
        lead_id, how = recent[0], how + "; used the most recent lead"

    def shape(payload: Any) -> dict[str, Any]:
        items, meta = client.unwrap(payload)
        if items:
            return {"kind": "list", "count": len(items),
                    "metaKeys": sorted(meta.keys()) if meta else [],
                    "fieldNames": sorted({k for it in items[:25]
                                          if isinstance(it, dict) for k in it})}
        if isinstance(payload, dict):
            data = payload.get("data", payload)
            if isinstance(data, dict):
                # Any list hanging off the record is a candidate for the log,
                # so report its length and what its entries are called. This
                # is where an inline activity log would be hiding.
                lists = {}
                for key, value in data.items():
                    if isinstance(value, list) and value:
                        lists[key] = {
                            "length": len(value),
                            "itemFields": sorted({
                                k for it in value[:25]
                                if isinstance(it, dict) for k in it}),
                        }
                return {"kind": "object", "fieldNames": sorted(data.keys()),
                        "listFields": lists}
        return {"kind": "empty"}

    results: dict[str, Any] = {}
    for path_template, param_template in ACTIVITY_PATHS:
        path = path_template.format(id=lead_id)
        params = ({k: v.format(id=lead_id) for k, v in param_template.items()}
                  if param_template else None)
        label = path_template + (f"?{'&'.join(f'{k}={v}' for k, v in param_template.items())}"
                                 if param_template else "")
        try:
            results[label] = {"ok": True, **shape(client.get(path, params))}
        except Exception as exc:  # noqa: BLE001 - a 404 is the normal answer
            message = str(exc)[:160]
            results[label] = {"ok": False, "verdict": _classify(message),
                              "error": message}

    live = {k: v for k, v in results.items() if v.get("ok")}
    return {"opportunityId": lead_id, "idResolvedBy": how,
            "pathsThatAnswered": sorted(live),
            "probed": results,
            "note": "Field names and counts only - no values are returned."}


# Field names that might carry attribution when `campaign` does not. Values of
# these ARE returned, because a campaign name, a form name or a UTM string is
# marketing metadata, not somebody's personal information - and seeing the
# value is the whole point. Everything else is reported by name only.
ATTRIBUTION_WORDS = ("campaign", "source", "medium", "utm", "form", "ad_",
                     "adset", "channel", "referr", "origin", "lead_type",
                     "created_type", "platform", "landing", "page")
ATTRIBUTION_NEVER = ("name", "email", "phone", "address", "seller", "owner",
                     "street", "city", "zip", "postal", "notes", "note",
                     "comment", "message")
# Keys that contain a blocked word but are marketing metadata, not a person.
# `form_name` is the one that matters - it is often the only thing left that
# says which ad a lead came from - and "name" would otherwise refuse it.
ATTRIBUTION_ALWAYS = frozenset({
    "form_name", "campaign_name", "adset_name", "ad_name", "adgroup_name",
    "source_name", "page_name", "landing_page_name", "channel_name",
})


@app.get("/api/debug/untagged")
async def api_debug_untagged(request: Request):
    """Leads with no campaign tag, and what they carry instead. Admin only.

    The Meta tab can only attribute a lead that has a campaign on it, so an
    untagged lead is invisible to every cost figure there - which is what made
    the Overview and the Meta tab disagree about how many contracts a month
    had. This answers the two questions that follow from that:

    1. Is the tag genuinely missing, or is it under a field this dashboard is
       not reading? If ``valuesPresent`` shows a populated `utm_campaign` or
       `form_name`, the fix is here rather than at Carrot.
    2. What do the untagged leads have in common - a day, a form, a created
       type? That is what turns "some leads are untagged" into a support
       ticket somebody can act on.

    Attribution fields come back WITH their values; everything else is names
    and types only. No seller names, phones, emails or addresses - the same
    rule the other probes follow, and the reason this one does not just dump
    the record.

    ``?from=&to=`` default to the last 30 days.
    """
    blocked = _admin_only(request)
    if blocked:
        return blocked
    day_from, day_to = _range(request, 30)
    stored = store.leads_between(day_from.isoformat(), day_to.isoformat())
    untagged = [lead for lead in stored
                if not _norm_campaign(lead.get("campaign"))]

    # Stored rows are the projection, which keeps only what the dashboard uses.
    # To answer "is it under another field" the raw record has to be re-read.
    ids = {str(lead["id"]) for lead in untagged}
    raw_by_id: dict[str, dict] = {}
    if ids:
        try:
            for page in range(1, 6):        # 500 leads is plenty to find these
                payload = client.opportunities(page=page, limit=100)
                items, _meta = client.unwrap(payload)
                if not items:
                    break
                for item in items:
                    key = str(item.get("id") or "")
                    if key in ids:
                        raw_by_id[key] = item
                if len(raw_by_id) >= len(ids):
                    break
        except Exception as exc:  # noqa: BLE001 - a probe reports, never raises
            raw_by_id = {}
            log.info("untagged probe could not re-read opportunities: %s", exc)

    def attribution_of(item: dict) -> dict[str, object]:
        """Every attribution-ish field on one record, with its value."""
        out: dict[str, object] = {}
        for key, value in (item or {}).items():
            name = key.lower()
            allowed = name in ATTRIBUTION_ALWAYS
            if not allowed and any(bad in name for bad in ATTRIBUTION_NEVER):
                continue
            if not allowed and not any(word in name for word in ATTRIBUTION_WORDS):
                continue
            if isinstance(value, (dict, list)):
                out[key] = _shape(value)
            elif value not in (None, ""):
                out[key] = str(value)[:120]
        return out

    rows = []
    populated: dict[str, int] = {}
    for lead in untagged:
        raw = raw_by_id.get(str(lead["id"]))
        fields = attribution_of(raw) if raw else {}
        for key in fields:
            populated[key] = populated.get(key, 0) + 1
        rows.append({
            "id": lead["id"],
            "createdDay": lead.get("created_day"),
            "stage": lead.get("stage"),
            "substage": lead.get("substage"),
            "owner": display_name(lead.get("owner_id") or "") or lead.get("owner_id"),
            "hasOffer": bool(lead.get("offer_ms")),
            "underContract": (lead.get("stage") or "").strip().lower() == "transaction",
            "resurfaced": bool(lead.get("resurfaced_count")),
            "attributionFields": fields,
            "rawFound": raw is not None,
        })
    rows.sort(key=lambda r: (r["createdDay"] or "", r["id"]))

    tagged = len(stored) - len(untagged)
    return {
        "range": {"from": day_from.isoformat(), "to": day_to.isoformat()},
        "leadsInRange": len(stored),
        "tagged": tagged,
        "untagged": len(untagged),
        "untaggedPct": round(100.0 * len(untagged) / len(stored), 1) if stored else None,
        "offersAmongUntagged": sum(1 for r in rows if r["hasOffer"]),
        "contractsAmongUntagged": sum(1 for r in rows if r["underContract"]),
        # How many of the untagged leads carry each attribution-ish field. A
        # field populated on all of them is a field this dashboard should be
        # falling back to; one populated on none of them is a dead end.
        "fieldsPopulated": dict(sorted(populated.items(), key=lambda kv: -kv[1])),
        "byDay": dict(sorted(Counter(
            r["createdDay"] for r in rows if r["createdDay"]).items())),
        # Whether the missing tag tracks with a resurfaced opportunity - a
        # seller who filled the form again against a record that already
        # existed - which would make this a known CRM behaviour rather than a
        # break.
        "resurfacedShare": sum(1 for r in rows if r["resurfaced"]),
        "notReadBack": sum(1 for r in rows if not r["rawFound"]),
        "leads": rows[:60],
    }


def normalize_user_of_summary(rows: list[dict]) -> dict[str, float]:
    """What the live resolver actually credits, per person, for these rows.

    Printed next to the per-field breakdown so the two can be compared: if
    `resolvesTo` matches the field you believe is right, the metric is being
    read correctly; if it matches a different one, it is not.
    """
    out: dict[str, float] = {}
    for row in rows:
        who = normalize_user_of(row)
        if not who:
            continue
        try:
            out[who] = out.get(who, 0.0) + float(row.get("count") or 0)
        except (TypeError, ValueError):
            pass
    return out


def _shape(value, depth: int = 0):
    """What a payload looks like, with none of what is in it.

    Field NAMES and value TYPES only, recursively. Enough to answer "does this
    endpoint carry a per-lead call history with a user on each entry" without
    pulling a single seller's name or number through the response.
    """
    if depth > 3:
        return "…"
    if isinstance(value, dict):
        return {k: _shape(v, depth + 1) for k, v in list(value.items())[:60]}
    if isinstance(value, list):
        return [f"list[{len(value)}]"] + ([_shape(value[0], depth + 1)] if value else [])
    return type(value).__name__




@app.get("/api/debug/offer-fields")
async def api_debug_offer_fields(request: Request):
    """Which field actually carries the offer amount, for one opportunity.

    Five of six offers picked up an amount from the guessed field names and
    one did not, so the amount clearly lives under more than one name. Rather
    than guess an eighth, this reads the live payload for a matching
    opportunity and reports every field that could plausibly be a money
    figure, with its value.

    Values are shown for money-shaped fields only - deal figures, which are
    already all over this dashboard. No seller names, phones or emails.

    ``?q=abbey`` matches on address.
    """
    blocked = _admin_only(request)
    if blocked:
        return blocked
    query = (request.query_params.get("q") or "").strip().lower()
    try:
        payload = client.opportunities(page=1, limit=100)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=502)
    items, _ = client.unwrap(payload)

    # A phone number parses as a large number, so "looks like money" alone
    # would print seller contact details into a debug response. The name is
    # checked first and a contact-shaped field is refused outright, whatever
    # its value.
    NEVER = ("phone", "mobile", "email", "zip", "postal", "address", "name",
             "lat", "lon", "lng", "ssn", "account", "_id", "id_", "uuid")
    MONEY_WORDS = ("offer", "amount", "price", "arv", "value", "mao", "fee",
                   "commission", "asking", "estimate")

    def money_like(key: str, value) -> bool:
        name = key.lower()
        # Scalars only. A money-ish NAME short-circuits the numeric test
        # below, so without this an "offers" array gets printed whole - and
        # whatever a nested object happens to contain is not ours to dump.
        # Nested values are reported as shapes instead, names only.
        if isinstance(value, (dict, list)):
            return False
        if isinstance(value, bool) or value in (None, "", 0, "0"):
            return False
        if any(word in name for word in NEVER) or name == "id":
            return False
        # Timestamps are the other thing that reads as a big number.
        if any(word in name for word in ("date", "_ms", "time", "stamp")):
            return False
        text = str(value).strip()
        if text.startswith("+"):                 # a phone, whatever it is named
            return False
        cleaned = text.lstrip("$").replace(",", "")
        if cleaned.isdigit() and len(cleaned) >= 10:
            return False                          # 10+ bare digits is not a price
        named = any(word in name for word in MONEY_WORDS)
        if isinstance(value, (int, float)):
            return named or 1000 <= float(value) <= 100_000_000
        try:
            return named or 1000 <= float(cleaned) <= 100_000_000
        except ValueError:
            return False

    # The offer-related fields, by name, as the live payload actually spells
    # them. Every record carries all of these keys, so their presence proves
    # nothing - what matters is which ones hold anything.
    OFFER_FIELDS = (
        "offer_amount", "last_offer_amount", "offer_profit", "last_offer_profit",
        "offer_count", "offer_id", "offer_status",
        "make_offer_date", "last_make_offer_date",
    )

    out = []
    for item in items:
        address = " ".join(str(item.get(k) or "") for k in
                           ("full_address", "street_address", "city")).lower()
        if query and query not in address:
            continue
        # With no search term, only opportunities that have an offer are
        # interesting - those are the ones the comparison is about.
        if not query and not (item.get("make_offer_date")
                              or item.get("last_make_offer_date")):
            continue
        fields = {k: v for k, v in sorted(item.items()) if money_like(k, v)}

        # An empty money list does not mean the amount is missing - it may be
        # nested, and the scalar filter above only reads the top level. So
        # report the SHAPE of every nested value, and every field name.
        # Names and shapes only: no values, so nothing here can be a seller
        # detail whatever the field turns out to hold.
        nested = {}
        for key, value in sorted(item.items()):
            if isinstance(value, dict):
                nested[key] = {"type": "object", "keys": sorted(value)[:25]}
            elif isinstance(value, list):
                inner = value[0] if value else None
                nested[key] = {
                    "type": "array", "length": len(value),
                    "itemKeys": sorted(inner)[:25] if isinstance(inner, dict)
                                else type(inner).__name__ if inner is not None
                                else None,
                }

        offerish = sorted(k for k in item
                          if any(w in k.lower() for w in
                                 ("offer", "amount", "price", "contract")))

        out.append({
            # Enough to identify the row without printing the seller.
            "address": item.get("full_address") or item.get("street_address"),
            "hasOfferDate": bool(funnel_mod._ms(item.get("make_offer_date"))
                                 or funnel_mod._ms(item.get("last_make_offer_date"))),
            "amountFoundByDashboard": funnel_mod._money(
                item, funnel_mod.OFFER_AMOUNT_FIELDS),
            "moneyShapedFields": fields,
            # Every field whose NAME mentions an offer, amount, price or
            # contract - regardless of whether it holds anything readable.
            "offerRelatedFieldNames": offerish,
            # The heart of it: which offer fields actually hold something, and
            # what. Money and ids only - no seller data lives under these
            # names. Comparing a record that produced an amount against one
            # that did not is what tells us why.
            "offerFieldValues": {
                key: item.get(key) for key in OFFER_FIELDS
                if item.get(key) not in (None, "", 0, "0", False)
            },
            **({"nestedShapes": nested, "allFieldNames": sorted(item)}
               if query else {}),
        })
        if len(out) >= 8:
            break

    return {
        "matched": len(out),
        "searchedFieldNames": list(funnel_mod.OFFER_AMOUNT_FIELDS),
        "opportunities": out,
    }


@app.get("/api/debug/offers")
async def api_debug_offers(request: Request):
    """Do the two ways of counting an offer agree? Addresses only, no seller data.

    The funnel counts an offer when a lead sits in the ``offer`` sub-stage,
    because ``make_offer_date`` was set on only 23 of the 43 August leads at
    that stage and that read as reps not filling it in. A lead has since turned
    up in the offer sub-stage with no offer recorded in InvestorFuse at all,
    which suggests the opposite reading: that the sub-stage is a pipeline
    position - where a rep is *working toward* an offer - and the date is set
    when one is actually presented.

    This does not pick a side. It counts both ways over any range and lists
    examples of each disagreement, so the definition can be settled by opening
    a few of them in InvestorFuse rather than by argument.
    """
    blocked = _admin_only(request)
    if blocked:
        return blocked
    day_from, day_to = _range(request, 30)
    leads = store.leads_between(day_from.isoformat(), day_to.isoformat())

    by_substage, by_date, both, neither = [], [], [], []
    for lead in leads:
        in_stage = (lead.get("substage") or "").strip().lower() == "offer"
        has_date = bool(lead.get("offer_ms"))
        if in_stage and has_date:
            both.append(lead)
        elif in_stage:
            by_substage.append(lead)
        elif has_date:
            by_date.append(lead)
        else:
            neither.append(lead)

    def sample(rows, limit=15):
        return [{"address": r.get("address") or "(no address)",
                 "stage": r.get("stage"), "substage": r.get("substage"),
                 "createdOn": r.get("created_day"),
                 "offerDate": r.get("offer_ms"),
                 "touches": r.get("touches"),
                 "url": lead_url(r)}
                for r in rows[:limit]]

    return {
        "range": {"from": day_from.isoformat(), "to": day_to.isoformat()},
        "leadsInRange": len(leads),
        "counts": {
            "substageOnly": len(by_substage),
            "dateOnly": len(by_date),
            "both": len(both),
            "offersIfSubstage": len(by_substage) + len(both),
            "offersIfDate": len(by_date) + len(both),
        },
        # The leads the dashboard calls an offer that have no offer date.
        # 2738 Mary Ave should be in here; open a few and see whether any of
        # them actually has an offer in InvestorFuse.
        "substageOnly": sample(by_substage),
        # The reverse: an offer date but not sitting in the offer sub-stage.
        # These are the ones a date-based count would ADD.
        "dateOnly": sample(by_date),
    }


@app.get("/api/debug/attribution")
async def api_debug_attribution(request: Request):
    """Can a call be tied to a lead AND to the rep who made it?

    The scorecard gives per-rep counts with no lead ids; the opportunity list
    gives leads with one current action. Neither can answer "which leads did
    Joy actually call", which is the denominator a real calls-per-lead needs.
    This probes the two places that might: the actions-touches analytics
    endpoint, and an individual opportunity, which may carry a fuller action
    history than the list rows do.

    Shapes only - names and types, never values. Nothing here identifies a
    seller, and the route sits behind the same login as everything else.
    """
    blocked = _admin_only(request)
    if blocked:
        return blocked
    out: dict[str, object] = {}

    try:
        touches = client.actions_touches()
        out["actionsTouches"] = _shape(touches)
    except Exception as exc:  # noqa: BLE001 - a probe reports, it doesn't raise
        out["actionsTouches"] = {"error": str(exc)[:300]}

    try:
        items, _meta = client.unwrap(client.opportunities(page=1, limit=1))
        if not items:
            out["opportunityDetail"] = {"error": "no opportunities returned"}
        else:
            out["listRowKeys"] = sorted(items[0].keys())
            lead_id = str(items[0].get("id") or "")
            detail = client.get(f"opportunities/{lead_id}") if lead_id else None
            out["opportunityDetail"] = _shape(detail)
            # The question in one line: does the detail hold anything that
            # looks like a list of actions or touches?
            body = detail.get("data", detail) if isinstance(detail, dict) else {}
            out["arrayFields"] = sorted(
                k for k, v in (body or {}).items() if isinstance(v, list)
            ) if isinstance(body, dict) else []
    except Exception as exc:  # noqa: BLE001
        out["opportunityDetail"] = {"error": str(exc)[:300]}

    # Which user field each scorecard metric is keyed on, and who shows up in
    # it. The field name is not the same on every metric - touches go by
    # touch_user_id, assignments by opportunity_owner_id, calls by plain
    # user_id - and that matters: a metric keyed on the OPPORTUNITY OWNER can
    # only ever credit somebody who owns opportunities. A lead manager never
    # does, so they would read zero on it no matter how much of the work was
    # theirs. This says whether that is what is happening.
    #
    # Team logins, not seller data - the same addresses every other endpoint
    # already returns.
    try:
        today = datetime.now(settings.tz).date()
        payload = client.employee_scorecard(
            (today - timedelta(days=30)).isoformat(), today.isoformat()
        )
        fields: dict[str, object] = {}
        for metric, entries in (payload or {}).items():
            if not isinstance(entries, list) or not entries:
                fields[metric] = "empty"
                continue
            rows = [e for e in entries if isinstance(e, dict)]
            # Per-ROW key sets, not the union across rows. The union cannot
            # tell "every row carries four user fields" from "each row carries
            # one, and different rows use different ones" - and only the first
            # of those is a problem, because then we are choosing between
            # several people for one count.
            shapes: dict[str, int] = {}
            for e in rows:
                shape = "+".join(sorted(k for k in e if k != "count"))
                shapes[shape] = shapes.get(shape, 0) + 1
            # What each candidate field would credit, so a metric that looks
            # wrong can be checked one field at a time.
            per_key: dict[str, dict[str, float]] = {}
            for e in rows:
                for k, v in e.items():
                    if k == "count" or not isinstance(v, str) or "@" not in v:
                        continue
                    try:
                        n = float(e.get("count") or 0)
                    except (TypeError, ValueError):
                        n = 0.0
                    bucket = per_key.setdefault(k, {})
                    who = v.strip().lower()
                    bucket[who] = bucket.get(who, 0.0) + n
            fields[metric] = {
                "rowShapes": shapes,
                "resolvesTo": normalize_user_of_summary(rows),
                "byField": per_key,
            }
        out["scorecardFields"] = fields
    except Exception as exc:  # noqa: BLE001
        out["scorecardFields"] = {"error": str(exc)[:300]}

    return out


# ----------------------------------------------------------------------
# Pages
# ----------------------------------------------------------------------
def _page(name: str) -> HTMLResponse:
    """Serve a page, telling it which timezone the data is bucketed in.

    Every ``day`` and ``created_day`` in the database is written in
    REPORT_TIMEZONE. The date presets are computed in the browser, so a viewer
    in another timezone - the lead managers are overseas - would ask for
    "today" on their own clock and get a date the server has no rows for. This
    tag lets the presets be built in the reporting timezone instead.
    """
    html = (BASE_DIR / name).read_text(encoding="utf-8")
    tag = f'<meta name="report-tz" content="{settings.tz}">\n'
    html = html.replace("<head>\n", "<head>\n" + tag, 1)
    # Cache-busting: the shared CSS/JS URLs carry a hash of their contents, so
    # a deploy can never pair a new page with yesterday's stylesheet from the
    # browser cache - which renders as a half-styled or empty page.
    html = (html.replace('href="/app.css"', f'href="/app.css?v={ASSET_VERSION}"')
                .replace('src="/app.js"', f'src="/app.js?v={ASSET_VERSION}"'))
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


def _asset_version() -> str:
    h = hashlib.sha256()
    for name in ("app.css", "app.js"):
        try:
            h.update((BASE_DIR / name).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:10]


ASSET_VERSION = _asset_version()


@app.get("/app.css")
async def app_css():
    return Response((BASE_DIR / "app.css").read_text(encoding="utf-8"),
                    media_type="text/css")


@app.get("/app.js")
async def app_js():
    return Response((BASE_DIR / "app.js").read_text(encoding="utf-8"),
                    media_type="application/javascript")


@app.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard_page():
    return _page("leaderboard.html")


@app.get("/rep", response_class=HTMLResponse)
async def rep_page():
    return _page("rep.html")


@app.get("/trends", response_class=HTMLResponse)
async def trends_page():
    return _page("trends.html")



# ----------------------------------------------------------------------
# Revenue
#
# The only part of this dashboard that knows who is looking. Everything else
# is behind one shared site password; this is a second, narrower gate, because
# the page shows what individual people are paid and a lead manager is meant
# to see their own bonuses and nobody else's.
#
# The role check happens on the SERVER, in the route, and the two views are
# built by different functions - so a lead manager's response never contains
# the company revenue figure at all. Withholding a computed field in the
# template would leave it sitting in the JSON for anyone who opened devtools.
# ----------------------------------------------------------------------
USER_COOKIE = "fhb_user"
USER_MAX_AGE = 60 * 60 * 24 * 14          # a fortnight, then sign in again
user_signer = URLSafeTimedSerializer(settings.session_secret, salt="fhb-user")
_user_attempts: dict[str, list[float]] = defaultdict(list)
# Failures per ACCOUNT, not just per address. With a four-digit PIN the IP
# limit alone is not enough: 10,000 combinations at 8 tries per 15 minutes is
# days of patient guessing, and an attacker who changes address resets it.
# Tracking the account means guessing one person's PIN is throttled no matter
# where the guesses come from - which matters, because the realistic threat
# here is a colleague who already has the site password, not a stranger.
_pin_failures: dict[str, list[tuple[float, str]]] = defaultdict(list)
PIN_ACCOUNT_MAX = 5
PIN_ACCOUNT_WINDOW = 900          # 15 minutes


# A global per-account lock is a weapon: five requests from anyone holding the
# site password could keep a colleague - or the only admin - locked out for as
# long as they cared to keep going. So the fast lock is per (account, address)
# and only a genuinely distributed attempt trips the account-wide one.
PIN_ACCOUNT_GLOBAL_MAX = 20


def _account_locked(username: str, ip: str) -> bool:
    now = time.time()
    fresh = [(t, addr) for t, addr in _pin_failures[username]
             if now - t < PIN_ACCOUNT_WINDOW]
    _pin_failures[username] = fresh
    if not fresh:
        _pin_failures.pop(username, None)      # keep the dict from growing
        return False
    from_here = sum(1 for _, addr in fresh if addr == ip)
    return from_here >= PIN_ACCOUNT_MAX or len(fresh) >= PIN_ACCOUNT_GLOBAL_MAX


def _note_pin_failure(username: str, ip: str) -> None:
    _pin_failures[username].append((time.time(), ip))


def _revenue_enabled() -> bool:
    return bool(settings.revenue_users.strip())


def _current_user(request: Request) -> dict | None:
    """The signed-in person, re-read from the database on every request.

    The cookie carries only a username. Roles are looked up fresh rather than
    baked into the cookie, so a role change in the environment takes effect on
    the next request instead of whenever somebody happens to sign in again.
    """
    token = request.cookies.get(USER_COOKIE)
    if not token:
        return None
    try:
        username = user_signer.loads(token, max_age=USER_MAX_AGE)
    except Exception:  # noqa: BLE001 - bad signature, expired, malformed
        return None
    user = store.get_user(str(username))
    if not user or not user.get("passwordHash"):
        return None
    # Membership is re-checked on every request, against the environment
    # rather than the database. Accounts are deliberately never deleted (a
    # payout still points at them), so without this, taking somebody out of
    # REVENUE_USERS did nothing at all: their cookie kept working, with their
    # old role, for a fortnight - and an admin who left stayed an admin.
    if user["username"] not in _roster_usernames():
        return None
    return user


def _revenue_display_names() -> dict[str, str]:
    """``{email: display name}`` for everyone on the Revenue roster."""
    return {
        person["person_email"]: person["display_name"]
        for person in accounts.parse_roster(settings.revenue_users, display_name)
        if person["person_email"]
    }


def _roster_usernames() -> frozenset[str]:
    """Who REVENUE_USERS currently names. Parsed per request; it is a
    semicolon-split of a short string, and caching it would reintroduce the
    very staleness this exists to close."""
    return frozenset(
        person["username"]
        for person in accounts.parse_roster(settings.revenue_users, display_name)
    )


def _sync_revenue_roster() -> None:
    """Reconcile the declared roster, and log claim codes for anyone unclaimed.

    The log is the bootstrap: an account system whose first account needs an
    account to create it never starts. Whoever can read the deploy logs claims
    the first admin account, and can then read everyone else's codes from the
    roster view.
    """
    if not _revenue_enabled():
        return
    # Forgotten-PIN recovery, before the reconcile so the cleared account is
    # unclaimed by the time sync_roster looks and gets a code like any other.
    for raw in (settings.revenue_reset or "").replace(",", ";").split(";"):
        username = raw.strip().lower()
        if not username:
            continue
        try:
            done = store.clear_password(username)
        except Exception as exc:  # noqa: BLE001 - never take the boot down
            log.warning("REVENUE_RESET: could not reset %s: %s", username, exc)
            continue
        if done:
            log.warning("REVENUE_RESET: cleared the password for %s. A claim "
                        "code follows below. REMOVE REVENUE_RESET from the "
                        "environment now - while it is set, this account's "
                        "password is wiped on every deploy.", username)
        else:
            log.warning("REVENUE_RESET: no account named %s. Check the "
                        "spelling against REVENUE_USERS.", username)
    try:
        unclaimed = accounts.sync_roster(
            store, settings.revenue_users, display_name)
    except Exception as exc:  # noqa: BLE001 - never take the boot down for this
        log.warning("revenue roster sync failed: %s", exc)
        return
    # The whole roster, every boot. Claim codes were already logged here, so
    # this adds no exposure - and without it there is no way to answer "what
    # is my username?" for an account that has already been claimed, which
    # is exactly the state somebody is in when they cannot sign in. The
    # username is whatever stands left of the "=" in REVENUE_USERS and is
    # frequently not an email, so showing it beats guessing.
    try:
        roster = store.list_users()
        log.info("REVENUE ROSTER (%d account%s) - sign in with the username "
                 "on the left:", len(roster), "" if len(roster) == 1 else "s")
        for row in roster:
            log.info("REVENUE ACCOUNT  %-38s  %-13s  %s",
                     row["username"], row.get("role", "?"),
                     "PIN set" if row.get("hasPassword") else "not claimed yet")
    except Exception as exc:  # noqa: BLE001
        log.warning("could not list the revenue roster: %s", exc)

    for person in unclaimed:
        log.info("REVENUE ACCOUNT UNCLAIMED  %s  (%s)  claim code: %s",
                 person["username"], person["role"], person["claimCode"])


@app.get("/revenue", response_class=HTMLResponse)
async def revenue_page():
    return _page("revenue.html")


@app.get("/api/revenue/me")
async def api_revenue_me(request: Request):
    """Who am I, and what may I see? The page draws itself from this."""
    if not _revenue_enabled():
        return {"enabled": False}
    user = _current_user(request)
    if not user:
        return {"enabled": True, "signedIn": False}
    return {
        "enabled": True, "signedIn": True,
        "name": user["displayName"], "role": user["role"],
        "roleLabel": accounts.ROLE_LABELS.get(user["role"], user["role"]),
        "company": accounts.can_see_company(user["role"]),
        "admin": user["role"] == accounts.ROLE_ADMIN,
    }


@app.post("/api/revenue/signin")
async def api_revenue_signin(request: Request, username: str = Form(""),
                             password: str = Form("")):
    if not _revenue_enabled():
        return JSONResponse({"error": "not configured"}, status_code=404)
    ip = _client_ip(request)
    now = time.time()
    _user_attempts[ip] = [t for t in _user_attempts[ip] if now - t < PIN_WINDOW]
    if len(_user_attempts[ip]) >= PIN_MAX_ATTEMPTS:
        return JSONResponse({"error": "too many attempts, try again later"},
                            status_code=429)
    name = username.strip().lower()
    if name and _account_locked(name, ip):
        return JSONResponse(
            {"error": "Too many wrong PINs for this account. "
                      "Wait 15 minutes and try again."}, status_code=429)
    user = store.get_user(name)
    # Somebody taken out of REVENUE_USERS is off the roster from that moment.
    # _current_user already refuses their cookie, but signing in still answered
    # 200 and set one, so the page said "signed in" and then showed nothing -
    # a dead end that reads as a bug rather than as revoked access. Treated as
    # no account at all, which is also what every other answer here says.
    if user and user["username"] not in _roster_usernames():
        user = None
    # PBKDF2 runs even when there is no account to check against. Without it
    # a missing account answered in 3ms and a real one in 150ms - a 40x gap
    # that reads out the staff list to anyone with a stopwatch.
    stored = (user or {}).get("passwordHash") or accounts.DUMMY_HASH
    ok = accounts.verify_password(password, stored)
    if not user or not ok:
        _user_attempts[ip].append(now)
        if name:
            _note_pin_failure(name, ip)
        # One answer for "no such account", "not set up yet" and "wrong PIN".
        # Naming the unclaimed ones told an attacker exactly which accounts a
        # leaked claim code would still open. The sign-in form already offers
        # "First time? Use a claim code" to everyone, so nobody is stuck.
        return JSONResponse({"error": "Wrong email or PIN"}, status_code=401)
    _user_attempts.pop(ip, None)
    _pin_failures.pop(name, None)
    store.touch_login(user["username"])
    response = JSONResponse({"ok": True})
    response.set_cookie(USER_COOKIE, user_signer.dumps(user["username"]),
                        max_age=USER_MAX_AGE, httponly=True, samesite="lax",
                        secure=settings.cookie_secure)
    return response


@app.post("/api/revenue/claim")
async def api_revenue_claim(request: Request, username: str = Form(""),
                            code: str = Form(""), password: str = Form("")):
    """Set a password for the first time, using a one-time claim code."""
    if not _revenue_enabled():
        return JSONResponse({"error": "not configured"}, status_code=404)
    ip = _client_ip(request)
    now = time.time()
    _user_attempts[ip] = [t for t in _user_attempts[ip] if now - t < PIN_WINDOW]
    if len(_user_attempts[ip]) >= PIN_MAX_ATTEMPTS:
        return JSONResponse({"error": "too many attempts, try again later"},
                            status_code=429)
    name = username.strip().lower()
    if name and _account_locked(name, ip):
        return JSONResponse(
            {"error": "Too many wrong codes for this account. "
                      "Wait 15 minutes and try again."}, status_code=429)
    user = store.get_user(name)
    # Same rule as signing in: off the roster, no account. Otherwise a stale
    # claim code for somebody who has left still opens a session.
    if user and user["username"] not in _roster_usernames():
        user = None
    stored_code = (user or {}).get("claimCode")
    if (not user or user.get("passwordHash") or not stored_code
            or not _same_secret(code.strip().upper(), stored_code)):
        _user_attempts[ip].append(now)
        if name:
            _note_pin_failure(name, ip)
        return JSONResponse({"error": "That claim code is not valid for this "
                                      "account."}, status_code=401)
    problem = accounts.pin_problem(password)
    if problem:
        return JSONResponse({"error": problem}, status_code=400)
    store.set_password(user["username"], accounts.hash_password(password))
    _user_attempts.pop(ip, None)
    _pin_failures.pop(name, None)
    response = JSONResponse({"ok": True})
    response.set_cookie(USER_COOKIE, user_signer.dumps(user["username"]),
                        max_age=USER_MAX_AGE, httponly=True, samesite="lax",
                        secure=settings.cookie_secure)
    return response


@app.post("/api/revenue/signout")
async def api_revenue_signout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(USER_COOKIE)
    return response


@app.get("/api/revenue/roster")
async def api_revenue_roster(request: Request):
    """Who has an account, and the claim codes still outstanding. Admin only."""
    user = _current_user(request)
    if not user or user["role"] != accounts.ROLE_ADMIN:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    rows = store.list_users()
    for row in rows:
        if not row["hasPassword"]:
            full = store.get_user(row["username"])
            row["claimCode"] = (full or {}).get("claimCode")
    return {"users": rows}


def _revenue_range(request: Request) -> tuple[str | None, str | None]:
    """``from``/``to``, both optional. Neither means all time.

    All time is the default the page opens on, so an absent range has to mean
    "everything" rather than falling back to the last 30 days the way the
    other tabs do.
    """
    day_from = (request.query_params.get("from") or "").strip() or None
    day_to = (request.query_params.get("to") or "").strip() or None
    if day_from and day_to and day_from > day_to:
        day_from, day_to = day_to, day_from
    return day_from, day_to


@app.get("/api/revenue")
async def api_revenue(request: Request):
    if not _revenue_enabled():
        return JSONResponse({"error": "not configured"}, status_code=404)
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "sign in required"}, status_code=403)

    # "View as" - an admin can render the page as somebody else sees it, for
    # checking that a lead manager really is walled off from the company
    # figures. The impersonation is resolved HERE, on the server, and only for
    # an admin: `as` from anyone else is ignored rather than refused, so the
    # parameter is not a probe for who else has an account.
    subject, viewing_as = user, None
    wanted = (request.query_params.get("as") or "").strip().lower()
    if wanted and user["role"] == accounts.ROLE_ADMIN and wanted != user["username"]:
        other = store.get_user(wanted)
        if other:
            subject, viewing_as = other, other

    day_from, day_to = _revenue_range(request)
    rows = store.deals_between(day_from, day_to)
    first, last = store.deal_days()
    # The Revenue roster as well as the InvestorFuse team. Dispositions people
    # have no InvestorFuse login at all, and neither does Alec, so a map built
    # from TEAM_ROLES alone printed a raw email where their name belongs.
    names = {owner: display_name(owner) for owner in (*TEAM_ROLES, *REVENUE_EXTRA_PEOPLE)}
    names.update(_revenue_display_names())

    if accounts.can_see_company(subject["role"]):
        payload = revenue_mod.company_view(rows, names)
    else:
        # Built by a different function, not filtered afterwards: there is no
        # company figure in this response to leak. That holds while viewing as
        # somebody too - an admin checking a lead manager's view gets exactly
        # the payload that lead manager would get, which is the point.
        payload = revenue_mod.personal_view(
            rows, subject.get("personEmail"),
            revenue_mod.ROLE_LEAD_MANAGER, subject["displayName"])

    payload["range"] = {"from": day_from, "to": day_to}
    payload["allTime"] = {"from": first, "to": last}
    payload["viewer"] = {"name": subject["displayName"], "role": subject["role"],
                         "roleLabel": accounts.ROLE_LABELS.get(subject["role"],
                                                               subject["role"])}
    # Only ever set for a real admin, so the page cannot be tricked into
    # showing the "viewing as" banner - and its own edit controls - by a
    # crafted query string.
    payload["viewingAs"] = ({"username": viewing_as["username"],
                             "name": viewing_as["displayName"]}
                            if viewing_as else None)
    payload["canEdit"] = user["role"] == accounts.ROLE_ADMIN
    return payload


@app.get("/api/revenue/deals")
async def api_revenue_deals(request: Request):
    """Every deal in range, in the shape the edit form needs. Admin only."""
    user = _current_user(request)
    if not user or user["role"] != accounts.ROLE_ADMIN:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    day_from, day_to = _revenue_range(request)
    rows = store.deals_between(day_from, day_to)

    # The person's NAME, not the raw text the payout was stored with. The bot
    # sends emails now - which is what makes a payout impossible to
    # misattribute - and an editor full of "alec@favoritehomebuyer.com: 14500"
    # is a worse thing to read and correct than one full of names. An
    # unresolved payout still shows its raw text, which is the case where the
    # raw text is the useful thing.
    known = _revenue_display_names()

    def who(payout):
        return known.get(payout["email"] or "", None) \
            or (display_name(payout["email"]) if payout["email"] else None) \
            or payout["person"]

    def lines(deal, role, key):
        return "\n".join(
            f"{who(p)}: {p['amount']:.0f}" if float(p["amount"]).is_integer()
            else f"{who(p)}: {p['amount']}"
            for p in deal["payouts"] if p["role"] == role)

    return {"deals": [{
        "id": d["id"], "address": d["address"], "closedOn": d["closedOn"],
        "fee": d["fee"], "channel": d["channel"], "notes": d["notes"],
        "acquisitions": lines(d, revenue_mod.ROLE_ACQUISITION, "commission"),
        "disposition": lines(d, revenue_mod.ROLE_DISPOSITION, "commission"),
        "leadManager": lines(d, revenue_mod.ROLE_LEAD_MANAGER, "bonus"),
        "owner": lines(d, revenue_mod.ROLE_OWNER, "share"),
        "unattributed": [p["person"] for p in d["payouts"] if not p["email"]],
    } for d in rows]}


@app.post("/api/revenue/deal")
async def api_revenue_save_deal(request: Request):
    """Correct a deal recorded wrong in Discord. Admin only.

    Takes exactly the payload the webhook takes, and runs it through exactly
    the same parser - so a hand correction and a bot post cannot drift into
    meaning different things. ``void: true`` retracts a deal; deals are never
    hard-deleted, because compensation history that can vanish is history
    nobody can audit.
    """
    user = _current_user(request)
    if not user or user["role"] != accounts.ROLE_ADMIN:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "body must be JSON"}, status_code=400)

    resolve = _deal_person_resolver()
    # The webhook's default for a missing closed_on is today, which is right
    # for a deal being logged as it happens. It is wrong for an EDIT: clearing
    # the date box, or a form that just did not send the field, silently
    # re-dated a deal closed in June to today and moved it between periods
    # with nothing on screen to say so. For a deal already on record the
    # default is the date it already has.
    fallback = datetime.now(settings.tz).date()
    if isinstance(payload, dict):
        stored = store.deal_closed_day(str(payload.get("deal_id") or "").strip())
        if stored:
            try:
                fallback = date.fromisoformat(stored)
            except ValueError:
                pass
    try:
        deal, payouts, unresolved = deals_mod.parse_deal(payload, resolve, fallback)
    except deals_mod.DealError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    if deal.get("void"):
        found = store.void_deal(deal["id"])
        log.info("deal %s voided by %s", deal["id"], user["username"])
        return {"ok": True, "deal_id": deal["id"], "voided": found}

    created = store.upsert_deal(deal, payouts)
    log.info("deal %s %s by %s", deal["id"],
             "created" if created else "edited", user["username"])
    return {"ok": True, "deal_id": deal["id"], "created": created,
            "unresolved": unresolved}


@app.api_route("/hooks/deal/{token}", methods=["POST"])
async def deal_webhook(token: str, request: Request):
    """Closed deals, pushed in by the Discord bot. See DEAL_WEBHOOK.md."""
    if not settings.deal_webhook_token or not _same_secret(token, settings.deal_webhook_token
    ):
        # A 404 rather than a 403, so a wrong token cannot be told apart from
        # the feature being switched off.
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - anything unparseable is a bad request
        return JSONResponse({"error": "body must be JSON"}, status_code=400)

    resolve = _deal_person_resolver()
    today = datetime.now(settings.tz).date()
    try:
        deal, payouts, unresolved = deals_mod.parse_deal(payload, resolve, today)
    except deals_mod.DealError as exc:
        # 400 with the offending field named, so the bot can echo it into the
        # channel while somebody still remembers the deal.
        return JSONResponse({"error": str(exc)}, status_code=400)

    if deal.get("void"):
        found = store.void_deal(deal["id"])
        return {"ok": True, "deal_id": deal["id"], "voided": found}

    created = store.upsert_deal(deal, payouts)
    if unresolved:
        log.info("deal %s has unattributed payouts: %s",
                 deal["id"], ", ".join(unresolved))
    return {"ok": True, "deal_id": deal["id"], "created": created,
            "unresolved": unresolved}


@app.get("/hooks/deal/{token}/people")
async def deal_people(token: str):
    """Who the bot may offer in its dropdowns, and under which role.

    Same secret as the webhook itself, and a 404 on a wrong one for the same
    reason. It exists so the bot's pickers are built from REVENUE_USERS rather
    than a second list kept in the bot: two lists drift, and the way that
    showed up was payouts recorded against a name the dashboard could not
    place. Picking from here means the bot sends the exact email, so nothing
    reaches `unresolved` at all.

    Nothing sensitive: names, emails and roles of people who are already on
    each other's leaderboards. No PINs, no claim codes, no figures.
    """
    if not settings.deal_webhook_token or not _same_secret(
            token, settings.deal_webhook_token):
        return JSONResponse({"error": "not found"}, status_code=404)

    # From the declared roster, not the database, so somebody removed from
    # REVENUE_USERS stops being offered immediately rather than at the next
    # restart - the same rule the sign-in gate follows.
    people = accounts.parse_roster(settings.revenue_users, display_name)
    owner = settings.revenue_owner
    return {
        # person_email, not username: a login named `ryan` is an operator, not
        # somebody who gets paid, and only an email resolves to a payout.
        "people": [{"username": p["username"],
                    "name": p["display_name"],
                    "role": p["role"],
                    "payable": bool(p["person_email"])} for p in people],
        # The bot asks "how much did Alec make?" only when there is an Alec to
        # ask about, and only when he is somebody the dashboard can resolve.
        "owner": next(({"username": p["username"], "name": p["display_name"]}
                       for p in people
                       if p["username"] == owner and p["person_email"]), None),
    }


def _deal_person_resolver():
    """Name-or-email to a known team email, or None.

    Reuses the same roster the texts feature matches smrtPhone senders
    against, so a person is spelled the same way everywhere and there is one
    place to fix a mismatch rather than two.
    """
    by_name = {}
    for email in (*TEAM_ROLES, *REVENUE_EXTRA_PEOPLE):
        by_name[display_name(email).strip().lower()] = email
        by_name[email.strip().lower()] = email
    # The Revenue roster too, not just the InvestorFuse team. Dispositions
    # people have no InvestorFuse login at all, so matching only against that
    # team would leave every disposition commission unattributed - which is
    # exactly the payout somebody notices missing.
    try:
        for person in store.list_users():
            email = (person.get("personEmail") or "").strip().lower()
            if not email:
                continue
            by_name.setdefault(email, email)
            name = (person.get("displayName") or "").strip().lower()
            if name:
                by_name.setdefault(name, email)
    except Exception:  # noqa: BLE001 - a webhook must not fail over this
        log.warning("could not read the revenue roster for name matching")

    def resolve(raw: str) -> str | None:
        text = (raw or "").strip().lower()
        if not text:
            return None
        if text in by_name:
            return by_name[text]
        return sms_user_email(text, parse_sms_user_map(settings.sms_user_map))

    return resolve


# ----------------------------------------------------------------------
# Meta ads
#
# Behind a second gate. Everyone with the site password can see how the team
# is performing; what the ads cost is a narrower audience, so the PIN adds a
# layer. It is checked on the SERVER for every data route - a PIN enforced in
# JavaScript is decoration, since the endpoint is one curl away.
#
# Four digits is ten thousand guesses, so the attempt limiter matters more
# here than on the main password. Same bucket mechanism, its own counter.
# ----------------------------------------------------------------------
META_COOKIE = "fhb_meta"
META_MAX_AGE = 60 * 60 * 12          # a working day, then PIN again
meta_signer = URLSafeTimedSerializer(settings.session_secret, salt="fhb-meta-pin")
meta_client = MetaClient(
    settings.meta_access_token, settings.meta_ad_account_id,
    settings.meta_api_version, settings.request_timeout,
)
_pin_attempts: dict[str, list[float]] = defaultdict(list)
PIN_MAX_ATTEMPTS = 8
PIN_WINDOW = 900                     # 15 minutes


def _meta_configured() -> bool:
    return bool(settings.meta_pin and meta_client.configured)


def _meta_unlocked(request: Request) -> bool:
    token = request.cookies.get(META_COOKIE)
    if not token:
        return False
    try:
        meta_signer.loads(token, max_age=META_MAX_AGE)
        return True
    except Exception:  # noqa: BLE001 - bad signature, expired, malformed
        return False


def _meta_guard(request: Request) -> JSONResponse | None:
    if not _meta_configured():
        return JSONResponse({"error": "not configured"}, status_code=404)
    if not _meta_unlocked(request):
        return JSONResponse({"error": "pin required"}, status_code=403)
    return None


google_client = GoogleAdsClient(
    settings.google_developer_token, settings.google_client_id,
    settings.google_client_secret, settings.google_refresh_token,
    settings.google_customer_id, settings.google_login_customer_id,
    settings.request_timeout,
    service_account_json=settings.google_service_account_json,
)


def _google_configured() -> bool:
    return bool(settings.meta_pin and google_client.configured())


def _google_guard(request: Request) -> JSONResponse | None:
    """Same PIN as Meta, because it is the same secret: ad spend.

    Checked against the Google client's own configuration rather than
    Meta's, so that turning Meta off - which is where this is heading -
    does not take the Google tab down with it.
    """
    if not _google_configured():
        return JSONResponse({"error": "not configured"}, status_code=404)
    if not _meta_unlocked(request):
        return JSONResponse({"error": "pin required"}, status_code=403)
    return None


@app.get("/google")
async def google_page():
    # Google and Meta share the Ads tab now.
    return RedirectResponse("/ads", status_code=302)


def _ads_source(lead: dict[str, Any]) -> str | None:
    """"google" | "meta" | None - which ad network a lead came from.

    GHL leads carry the channel worked out at intake (funnel.ghl_channel:
    web attribution first, the intake workflow's source as a fallback).
    InvestorFuse-era leads only have a campaign string: the Google tag, or a
    Meta campaign name - every other tagged lead then was a Facebook campaign.
    """
    channel = (lead.get("channel") or "").strip().lower()
    if channel == "google ads":
        return "google"
    if channel == "meta ads":
        return "meta"
    campaign = (lead.get("campaign") or "").strip()
    tag = (settings.google_campaign_tag or "").strip().lower()
    low = campaign.lower()
    if tag and tag in low:
        return "google"
    if funnel_mod.is_ghl(lead):
        if low.startswith("google ads"):
            return "google"
        if low.startswith(("fb", "meta")) or "facebook" in low:
            return "meta"
        return None
    # InvestorFuse era: Meta was the only new-lead intake, and its campaign
    # tagging was unreliable - so an untagged InvestorFuse lead is Meta too.
    # Anything that names Google / PPC / Ignite stays Google.
    if any(word in low for word in ("google", "ppc", "ignite")):
        return "google"
    return "meta"


def _ads_panel(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """One network's leads and where they stand - the Ads tab's top block.

    Same rules as the Overview tiles and the Rep drill-down: Qualified +
    Contacted, offers, contracts sent and signed are "ever reached"; the
    stage bar is where each lead sits right now.
    """
    built = funnel_mod.build(rows)
    board = funnel_mod.stage_breakdown(rows, _stage_order())
    total = built["total"]
    dq = sum(1 for l in rows if funnel_mod.is_ghl(l)
             and funnel_mod.ghl_stage_family(l.get("substage")) == "dq"
             and not funnel_mod.is_marked_lost(l))
    lost = sum(1 for l in rows if funnel_mod.is_ghl(l) and (
        funnel_mod.ghl_stage_family(l.get("substage")) == "lost"
        or funnel_mod.is_marked_lost(l)))
    stages = []
    for st in board["stages"]:
        if not st["count"]:
            continue
        kind = st["family"] or "new"
        if funnel_mod.is_contract_sent_stage(st["name"]):
            kind = "sent"
        stages.append({"name": st["name"], "count": st["count"], "kind": kind})
    # Grouped by what the stage means, board order within a group: every
    # not-yet-reached stage (new, no answer, never contacted) sits together at
    # the start, so the grey run of the bar reads as one number.
    kind_order = {"new": 0, "qualified": 1, "offer": 2, "sent": 3, "contract": 4,
                  "closed": 5, "dq": 6, "lost": 7}
    stages.sort(key=lambda st: kind_order.get(st["kind"], 8))
    if board["lost"]:
        stages.append({"name": "Marked lost", "count": board["lost"], "kind": "lost"})
    if board["otherLeads"]:
        stages.append({"name": "InvestorFuse (before GHL)", "count": board["otherLeads"],
                       "kind": "other"})
    return {
        "leads": total,
        "qualified": built["reachedQualified"],
        "qualifiedPct": funnel_mod._pct(built["reachedQualified"], total),
        "offers": built["offers"],
        "contractsSent": _contracts_sent_count(rows),
        "contracts": built["contracts"],
        "closed": built["closed"],
        "disqualified": dq,
        "disqualifiedPct": funnel_mod._pct(dq, total),
        "lost": lost,
        "stages": stages,
    }


def _ads_rows(day_from: date, day_to: date, source: str) -> list[dict[str, Any]]:
    return [r for r in store.leads_between(day_from.isoformat(), day_to.isoformat())
            if _ads_source(r) == source]


@app.get("/ads", response_class=HTMLResponse)
async def ads_page():
    return _page("ads.html")


@app.get("/api/ads/status")
async def api_ads_status(request: Request):
    unlocked = bool(settings.meta_pin) and _meta_unlocked(request)
    return {"pinConfigured": bool(settings.meta_pin), "unlocked": unlocked,
            "google": _google_configured(), "meta": _meta_configured()}


@app.get("/api/ads/leads")
def api_ads_leads(request: Request):  # sync: blocking HTTP/DB runs in a worker thread
    """Leads from each network and where they stand. Behind the spend PIN."""
    if not settings.meta_pin:
        return JSONResponse({"error": "not configured"}, status_code=404)
    if not _meta_unlocked(request):
        return JSONResponse({"error": "pin required"}, status_code=403)
    day_from, day_to = _range(request, 30)
    return {
        "range": {"from": day_from.isoformat(), "to": day_to.isoformat()},
        "google": _ads_panel(_ads_rows(day_from, day_to, "google")),
        "meta": _ads_panel(_ads_rows(day_from, day_to, "meta")),
        "freshness": _freshness(),
    }


def _google_leads(day_from: date, day_to: date) -> dict[str, Any]:
    """The InvestorFuse side of the Google tab.

    Google records a conversion for a website form fill and nothing for an
    inbound call, and the phone is a large part of what search produces
    here. So the lead count comes from the CRM, matched on the campaign tag
    Google intake writes, and Google is asked only what it alone knows:
    what we spent.

    The match is a case-insensitive substring, so the tag keeps working if
    somebody later appends a campaign name to it.
    """
    # Same population as the Ads tab's lead panel, so the two always agree.
    leads = _ads_rows(day_from, day_to, "google")
    built = funnel_mod.build(leads)
    return {
        "leads": built["total"],
        "contacted": built["contacted"],
        "contactedPct": built["contactedPct"],
        "qualified": built["reachedQualified"],
        "qualifiedPct": funnel_mod._pct(built["reachedQualified"],
                                        built["total"]),
        "offers": built["offers"],
        "contracts": built["contracts"],
        "contractsSent": _contracts_sent_count(leads),
        "buckets": built["buckets"],
        "bucketLabels": built["bucketLabels"],
        "tag": settings.google_campaign_tag,
        # Every distinct campaign string behind those leads. If the tag is
        # ever split per campaign this is where it shows up first; until
        # then it is the evidence that one tag really does carry them all.
        "tagVariants": dict(sorted(
            Counter((r.get("campaign") or "(untagged)") for r in leads).items(),
            key=lambda kv: -kv[1])),
    }


@app.get("/api/google")
def api_google(request: Request):  # sync: blocking HTTP/DB runs in a worker thread
    """Google spend joined to InvestorFuse leads.

    Behind the same PIN as Meta, because it is the same secret: ad spend.
    """
    blocked = _google_guard(request)
    if blocked:
        return blocked

    today = datetime.now(settings.tz).date()
    day_to = _parse_day(request.query_params.get("to"), today)
    day_from = _parse_day(request.query_params.get("from"),
                          day_to - timedelta(days=29))
    if day_from > day_to:
        day_from, day_to = day_to, day_from

    crm = _google_leads(day_from, day_to)

    campaigns: list[dict[str, Any]] = []
    totals: dict[str, Any] = {}
    terms: list[dict[str, Any]] = []
    devices: list[dict[str, Any]] = []
    error = None
    try:
        campaigns = google_client.by_campaign(day_from.isoformat(),
                                              day_to.isoformat())
        totals = google_client.totals_from(campaigns)
        terms = google_client.search_terms(day_from.isoformat(),
                                           day_to.isoformat())
        devices = google_client.by_device(day_from.isoformat(),
                                          day_to.isoformat())
    except GoogleAdsError as exc:
        # The CRM half still renders. A spend outage should cost the cost
        # figures, not the whole page.
        error = str(exc)
        log.warning("google ads read failed: %s", exc)

    spend = totals.get("spend")
    leads = crm["leads"]
    cpl = (spend / leads) if (spend is not None and leads) else None
    return {
        "range": {"from": day_from.isoformat(), "to": day_to.isoformat()},
        "spend": spend,
        "costPerLead": cpl,
        "cplBand": {"low": settings.google_cpl_low,
                    "high": settings.google_cpl_high},
        "crm": crm,
        "totals": totals,
        "campaigns": campaigns,
        "devices": devices,
        "searchTerms": terms,
        # What Google thinks it produced, beside what the CRM received.
        # These SHOULD differ - the gap is roughly the phone - and naming
        # the gap is what stops somebody trying to "fix" it.
        "formFillGap": (None if not totals else
                        round(crm["leads"] - totals.get("formFills", 0), 1)),
        "findings": _google_findings(campaigns, terms, totals, cpl, crm),
        "error": error,
        "freshness": _freshness(),
    }


def _google_findings(campaigns: list[dict[str, Any]],
                     terms: list[dict[str, Any]],
                     totals: dict[str, Any], cpl: float | None,
                     crm: dict[str, Any]) -> list[dict[str, Any]]:
    """Where the money is going wrong, in plain sentences.

    Only rules with an action behind them. A dashboard that flags
    everything gets read as flagging nothing.
    """
    out: list[dict[str, Any]] = []
    spend = (totals or {}).get("spend") or 0

    if cpl is not None and cpl > settings.google_cpl_high:
        out.append({"level": "bad", "title": "Cost per lead is above target",
                    "detail": f"${cpl:,.0f} a lead, against a ceiling of "
                              f"${settings.google_cpl_high:,.0f}."})
    elif cpl is not None and cpl <= settings.google_cpl_low:
        out.append({"level": "good", "title": "Cost per lead is strong",
                    "detail": f"${cpl:,.0f} a lead, inside the "
                              f"${settings.google_cpl_low:,.0f} target."})

    for c in campaigns:
        if c["spend"] >= max(spend * 0.1, 50) and not c["clicks"]:
            out.append({"level": "bad",
                        "title": f"{c['name']} spent with no clicks",
                        "detail": f"${c['spend']:,.0f} and not one click — "
                                  f"check targeting and that the ads are "
                                  f"approved."})

    # Why you are not showing more. Two different constraints with
    # opposite fixes, which is the whole reason they are reported apart:
    # losing impressions to BUDGET means raise the budget or narrow the
    # targeting; losing them to RANK means bid more or earn a better
    # Quality Score. Reading one as the other wastes a month.
    for c in campaigns:
        if c["spend"] < max(spend * 0.1, 50):
            continue
        budget_lost = c.get("lostToBudget")
        rank_lost = c.get("lostToRank")
        share = c.get("searchImpressionShare")
        if budget_lost is not None and budget_lost >= 0.10:
            out.append({
                "level": "warn",
                "title": f"{c['name']} is budget-capped",
                "detail": f"Losing {budget_lost:.0%} of available impressions "
                          f"to budget. People are searching and the ad is not "
                          f"showing because the money ran out — raise the "
                          f"budget, or narrow the targeting so the same money "
                          f"covers fewer places."})
        # Rank loss is normally large for a small advertiser, so this only
        # fires when rank is clearly the binding constraint and budget is not.
        elif (share is not None and share < 0.20
                and rank_lost is not None and rank_lost >= 0.50
                and (budget_lost or 0) < 0.10):
            out.append({
                "level": "warn",
                "title": f"{c['name']} is being outranked, not outspent",
                "detail": f"Showing on {share:.0%} of eligible searches, "
                          f"losing {rank_lost:.0%} to Ad Rank with the budget "
                          f"barely binding. That is a bid or Quality Score "
                          f"problem — more budget will not buy more "
                          f"impressions here."})

    # The classic search leak: impressions nobody clicks, and one term
    # quietly eating the budget.
    costly = [t for t in terms if t["spend"] > 0 and not t["clicks"]]
    wasted = sum(t["spend"] for t in costly)
    if wasted >= max(spend * 0.05, 25):
        out.append({"level": "warn",
                    "title": "Spend on terms nobody clicked",
                    "detail": f"${wasted:,.0f} across {len(costly)} search "
                              f"terms with impressions and no clicks — "
                              f"candidates for negative keywords."})
    if terms and spend:
        top = terms[0]
        if top["spend"] >= spend * 0.3:
            out.append({"level": "warn",
                        "title": f"One term is {top['spend'] / spend:.0%} "
                                 f"of all spend",
                        "detail": f"“{top['term']}” took "
                                  f"${top['spend']:,.0f}. Worth checking it "
                                  f"is the intent you want to buy."})

    if crm["leads"] and crm["contactedPct"] is not None \
            and crm["contactedPct"] < 60:
        out.append({"level": "warn",
                    "title": "Most Google leads go uncontacted",
                    "detail": f"{crm['contacted']} of {crm['leads']} reached. "
                              f"A paid lead going cold is the most expensive "
                              f"kind there is."})
    if spend and not crm["leads"]:
        out.append({"level": "bad", "title": "Spend with no leads in the CRM",
                    "detail": f"${spend:,.0f} spent and nothing tagged "
                              f"“{settings.google_campaign_tag}” "
                              f"arrived. Either the intake tag changed or "
                              f"tracking is broken."})
    return out


def _google_action_roles() -> dict[str, str]:
    """The conversion actions FHB actually uses, and what each one is for.

    Everything else in the account is left over from the agency, and the
    Ads tab tucks it away. Set by ID (config), so a rename in Google Ads
    doesn't drop one off the list.
    """
    roles = {
        settings.google_conv_lead: "Website lead — drives bidding",
        settings.google_conv_qualified: "From the CRM: Qualified + Contacted",
        settings.google_conv_contract: "From the CRM: Under contract",
        settings.google_conv_closed: "From the CRM: Closed",
    }
    return {k: v for k, v in roles.items() if k}


def _tracking_findings(health: dict[str, Any],
                       roles: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Whether Google can still see a conversion, in plain sentences.

    The ordering matters. A smart-bidding campaign with no conversion
    signal is not an unmeasured campaign, it is a campaign being optimised
    against nothing, and it goes first because it is the one that costs
    money every hour it stays true.
    """
    out: list[dict[str, Any]] = []
    actions = health.get("actions") or []
    campaigns = health.get("campaigns") or []
    if (health.get("errors") or {}).get("all"):
        return out
    # Judge only the actions in use. The leftovers are dormant on purpose, so
    # "excluded" or "not recording" about them is noise. The CRM uploads are
    # secondary by design (they report outcomes, the form drives bidding), and
    # calls are counted in the CRM rather than by Google.
    uploads: set[str] = set()
    if roles:
        by_id = {a["id"]: a for a in actions}
        for aid, label in roles.items():
            a = by_id.get(aid)
            if a is None:
                out.append({"level": "bad", "title": f"{label}: action not found",
                            "detail": f"Conversion action {aid} isn't in the account. "
                                      "If it was replaced, update the ID in Railway."})
            elif a["status"] != "ENABLED":
                out.append({"level": "bad",
                            "title": f"\u201c{a['name']}\u201d is {a['status'].lower() or 'off'}",
                            "detail": f"It's the one used for: {label}. Turn it back on "
                                      "in Google Ads, or nothing is counted there."})
        uploads = {settings.google_conv_qualified, settings.google_conv_contract,
                   settings.google_conv_closed}
        actions = [a for a in actions if a["id"] in roles]

    enabled = [a for a in actions if a["status"] == "ENABLED"]
    recording = [a for a in enabled if a["recorded"] > 0]
    live_spend = sum(c["spend"] for c in campaigns)
    smart = [c for c in campaigns
             if c.get("needsConversions") and c["spend"] > 0]

    if smart and not recording:
        burn = sum(c["spend"] for c in smart)
        names = ", ".join(c["name"] for c in smart[:3])
        plural = "s" if len(smart) != 1 else ""
        out.append({
            "level": "bad",
            "title": f"{len(smart)} campaign{plural} bidding on a conversion "
                     f"signal that is not arriving",
            "detail": f"${burn:,.0f} went through {names}"
                      f"{' and others' if len(smart) > 3 else ''}, all on "
                      f"automated bidding, while no conversion action "
                      f"recorded anything in this range. Automated bidding "
                      f"with no conversions does not pause itself - it keeps "
                      f"spending on whatever it guessed last. Fix this one "
                      f"first."})

    if not actions:
        out.append({"level": "bad", "title": "No conversion actions exist",
                    "detail": "The account has nothing defined to count as a "
                              "conversion, so nothing on the website or the "
                              "phone can be measured or bid on."})
    elif not enabled:
        out.append({"level": "bad",
                    "title": "Every conversion action is paused or removed",
                    "detail": f"{len(actions)} "
                              f"exist{'s' if len(actions) == 1 else ''}, "
                              f"none enabled. This is "
                              f"what being de-linked from an agency's manager "
                              f"account tends to look like."})
    elif not recording:
        plural = "s" if len(enabled) != 1 else ""
        out.append({"level": "bad",
                    "title": f"{len(enabled)} conversion action{plural} "
                             f"enabled, none recording",
                    "detail": "Enabled but silent across this range. An "
                              "enabled action whose tag is gone from the page "
                              "looks perfectly healthy in the account and "
                              "counts nothing."})

    for a in enabled:
        if not a["counted"] and a["id"] not in uploads:
            out.append({"level": "bad",
                        "title": f"\u201c{a['name']}\u201d is excluded from "
                                 f"Conversions",
                        "detail": "It records, but it is left out of the "
                                  "Conversions column, so bidding ignores it. "
                                  "Mark it primary if it is meant to drive "
                                  "bidding."})
        elif a["counting"] == "EVERY" and a["recorded"] > 0:
            out.append({"level": "warn",
                        "title": f"\u201c{a['name']}\u201d counts every "
                                 f"conversion",
                        "detail": "For a seller lead you want one per click, "
                                  "not one per submission - otherwise a double "
                                  "submit reads as two leads and bidding "
                                  "chases the duplicate."})

    calls = [a for a in enabled if a["type"] in google_client.CALL_TYPES]
    if not enabled or roles:
        # "No call action" on an account with nothing enabled at all is a
        # second way of saying the finding above. One sentence per problem.
        pass
    elif not calls:
        out.append({"level": "warn", "title": "No call conversion action",
                    "detail": "Nothing in the account counts a phone call. "
                              "Calls are a large share of what search "
                              "produces here, so Google is bidding as if they "
                              "do not happen."})
    elif not any(a["recorded"] > 0 for a in calls):
        plural = "s" if len(calls) != 1 else ""
        out.append({"level": "bad", "title": "Call tracking is not recording",
                    "detail": f"{len(calls)} call action{plural} enabled and "
                              f"none counted a call. Expected if the tracking "
                              f"number left with the agency."})

    if enabled and not any(a["type"] in google_client.UPLOAD_TYPES
                           for a in actions):
        out.append({"level": "warn",
                    "title": "Google is optimising for form fills, not "
                             "qualified leads",
                    "detail": "No offline conversion import is set up, so "
                              "every submission counts the same whether it was "
                              "a motivated seller or a wrong number. Importing "
                              "the CRM's qualified leads back is what moves "
                              "the qualified rate."})
    elif enabled and not any(a["primary"] for a in enabled):
        out.append({"level": "warn", "title": "No primary conversion action",
                    "detail": "Nothing is marked primary, so nothing drives "
                              "automated bidding even where it records."})

    if recording and live_spend and not smart:
        plural = "s" if len(enabled) != 1 else ""
        out.append({"level": "good", "title": "Conversions are recording",
                    "detail": f"{len(recording)} of {len(enabled)} enabled "
                              f"action{plural} counted something in this "
                              f"range."})
    return out


@app.get("/api/google/tracking")
def api_google_tracking(request: Request):  # sync: blocking HTTP/DB runs in a worker thread
    """Conversion-action health: what exists, what is recording, and what
    the live campaigns are bidding on.

    Separate from /api/google because it answers a different question.
    /api/google says what the money bought; this says whether Google can
    still see it - which is the question when an agency has just walked off
    with the landing page and the tracking number.
    """
    blocked = _google_guard(request)
    if blocked:
        return blocked

    today = datetime.now(settings.tz).date()
    day_to = _parse_day(request.query_params.get("to"), today)
    day_from = _parse_day(request.query_params.get("from"),
                          day_to - timedelta(days=29))
    if day_from > day_to:
        day_from, day_to = day_to, day_from

    health = google_client.tracking_health(day_from.isoformat(),
                                           day_to.isoformat())
    health["range"] = {"from": day_from.isoformat(), "to": day_to.isoformat()}
    roles = _google_action_roles()
    for a in health.get("actions") or []:
        a["inUse"] = a.get("id") in roles
        a["usedFor"] = roles.get(a.get("id"))
    # In-use actions first, in the order a lead moves through them.
    order = {aid: i for i, aid in enumerate(roles)}
    health["actions"] = sorted(health.get("actions") or [],
                               key=lambda a: (order.get(a.get("id"), 99),
                                              a.get("status") != "ENABLED", a.get("name") or ""))
    health["findings"] = _tracking_findings(health, roles)
    health["campaignTag"] = settings.google_campaign_tag
    return health


@app.get("/api/google/check")
def api_google_check(request: Request):  # sync: blocking HTTP/DB runs in a worker thread
    """Connectivity probe, for setting this up.

    Behind the spend PIN rather than the admin login. It was admin-only,
    which was circular: the one thing this endpoint exists to diagnose is a
    half-finished setup, and admin needs the Revenue sign-in, which is a
    separate piece of setup entirely. Anyone who can reach this can already
    open the Google tab, which shows actual spend - and this returns strictly
    less: an account name, a currency, and an error string.

    When Google is not configured at all there is no PIN gate to pass, so
    the list of missing variable NAMES is readable by anyone already past
    the site password. No value is ever echoed back.
    """
    if not _google_configured():
        return google_client.check()
    if not _meta_unlocked(request):
        return JSONResponse(
            {"error": "Enter the spend PIN on the Google or Meta tab first, "
                      "then reload this page.",
             "configured": True}, status_code=403)
    return google_client.check()


def _ghl_activity_status() -> dict:
    """Step 3 health, plus who's who - so a merged or missing user is visible."""
    out = {"enabled": settings.activity_enabled,
           "cutover": settings.ghl_activity_cutover or None,
           "messages": store.message_summary(),
           # Last 14 days of inbound calls by status/length - to check what
           # GHL calls an answered vs a missed call. Counts only.
           "inboundCallProfile": store.inbound_call_profile(
               int(time.time() * 1000) - 14 * 86_400_000),
           "lastComputed": store.get_meta("ghl_activity_last")}
    if ghl_client is not None:
        try:
            import activity as activity_mod
            users = ghl_client.users()
            ids, _numbers = activity_mod.canonical_users(
                users, normalize_team_names, settings.ghl_location_id)
            people: dict[str, dict] = {}
            for u in users:
                email = ids.get(str(u.get("id")))
                if not email:
                    continue
                person = people.setdefault(email, {"name": display_name(email),
                                                   "email": email, "accounts": 0})
                person["accounts"] += 1
            # Accounts outside the user list that the collector resolved by id
            # (agency logins) belong on the same person's row.
            try:
                cache = json.loads(store.get_meta("ghl_user_cache") or "{}")
            except ValueError:
                cache = {}
            extra, _n = activity_mod.canonical_users(
                [{"id": k, **v} for k, v in cache.items() if v.get("name")],
                normalize_team_names, settings.ghl_location_id)
            for uid, email in extra.items():
                if uid in ids:
                    continue
                person = people.setdefault(email, {"name": display_name(email),
                                                   "email": email, "accounts": 0})
                person["accounts"] += 1
            out["people"] = sorted(people.values(), key=lambda p: p["name"])
            out["unresolvedUserIds"] = sorted(k for k, v in cache.items() if not v.get("name"))

            # Calls and texts nobody gets credit for, since the cutover, grouped
            # by OUR side of the line - the number a rep dialled out on or a
            # seller rang. Never the seller's number. An answered inbound call
            # or a person's outbound message landing here means someone's
            # work is going uncounted.
            ids_all = {**extra, **ids}
            numbers = {**_n, **_numbers}
            since = int(datetime.fromisoformat(settings.ghl_activity_cutover)
                        .replace(tzinfo=settings.tz).timestamp() * 1000) \
                if settings.ghl_activity_cutover else 0
            missing: dict[str, int] = {}
            for msg in store.messages_between(since, 10**15):
                if msg["kind"] == "sms" and msg["source"] == "workflow":
                    continue
                if msg["direction"] == "inbound" and msg["kind"] == "sms":
                    continue
                if activity_mod.who(msg, ids_all, numbers):
                    continue
                ours = msg["from_number"] if msg["direction"] == "outbound" else msg["to_number"]
                key = f"{msg['kind']} {msg['direction']} via ...{(ours or '')[-4:]}"
                missing[key] = missing.get(key, 0) + 1
            out["uncredited"] = dict(sorted(missing.items(), key=lambda kv: -kv[1]))
        except Exception as exc:  # noqa: BLE001
            out["peopleError"] = str(exc)[:200]
    return out


@app.get("/api/ghl/verify")
async def api_ghl_verify():
    """Is the GoHighLevel connection working? One cheap call per scope.

    Behind the site password like every /api route. Returns counts and
    error strings only - never the token, never any contact data.
    """
    if ghl_client is None:
        return {"configured": False,
                "missing": [n for n, v in (("GHL_TOKEN", settings.ghl_token),
                                           ("GHL_LOCATION_ID", settings.ghl_location_id))
                            if not v]}
    import asyncio
    checks = await asyncio.to_thread(ghl_client.check)
    stored = store._query("SELECT COUNT(*) FROM lead WHERE crm = 'ghl'")
    return {"configured": True,
            "locationId": settings.ghl_location_id,
            "investorfuseStillOn": settings.if_configured,
            "checks": checks,
            "allOk": all(c.get("ok") for c in checks.values()),
            "ghlLeadsStored": int(stored[0][0]) if stored else 0,
            "stageHistory": store.stage_event_summary(),
            # How many GHL leads carry a hashed email / phone - what Google
            # matching and the Meta lead check depend on. Counts only.
            "contactHashes": dict(zip(("leads", "withEmail", "withPhone"), store._query(
                "SELECT COUNT(*), COUNT(email_sha), COUNT(phone_sha) FROM lead "
                "WHERE crm = 'ghl'")[0])),
            "pipelineFilter": {
                "pipelineId": settings.ghl_pipeline_id or "all",
                "skippedOtherPipelines": int(store.get_meta("ghl_pipeline_skipped") or 0),
                "oldLeadsPipelines": sorted(collector._old_pipelines()),
                "oldLeadsStored": store.old_lead_summary()["stored"],
                "warning": store.get_meta("ghl_pipeline_warning") or None,
            },
            "googleUploads": {
                "mode": settings.google_upload_mode,
                "since": settings.google_upload_since or None,
                "actions": conversion_sync.actions(),
                "serviceAccount": bool(settings.google_service_account_json),
                "waiting": len(conversion_sync.pending()) if settings.google_upload_mode != "off" else None,
                "lastRun": store.get_meta("google_upload_last"),
                "recheck": json.loads(store.get_meta("google_upload_recheck") or "{}"),
                **store.upload_summary(),
            },
            "activity": _ghl_activity_status(),
            "callAssign": ({**collector.call_assigner.status(),
                            "lastWebhook": store.get_meta("ghl_last_call_hook")}
                           if getattr(collector, "call_assigner", None) else None),
            "webhook": {
                "configured": bool(settings.ghl_webhook_token),
                "lastReceived": store.get_meta("ghl_last_hook"),
                "lastError": store.get_meta("ghl_last_hook_error") or None,
                "lastPayloadKeys": json.loads(store.get_meta("ghl_last_hook_keys") or "[]"),
                "stageMismatches": int(store.get_meta("ghl_hook_stage_mismatch") or 0),
            },
            "lastSuccess": store.get_meta("last_success"),
            "lastError": store.get_meta("last_error") or None,
            "investorfuseSnapshot": _if_snapshot_status()}


def _if_snapshot_status() -> dict | None:
    try:
        snap = json.loads(store.get_meta("if_snapshot") or "null")
    except ValueError:
        return None
    if not snap:
        return {"status": "not run yet" if settings.if_configured else "InvestorFuse not configured"}
    snap.pop("startedTs", None)
    return snap


@app.get("/meta")
async def meta_page():
    return RedirectResponse("/ads", status_code=302)


@app.get("/api/meta/status")
async def api_meta_status(request: Request):
    """What the page needs before it can decide what to draw."""
    return {"configured": _meta_configured(),
            "unlocked": _meta_configured() and _meta_unlocked(request)}


@app.get("/api/google/status")
async def api_google_status(request: Request):
    """What the Google page needs before it can decide what to draw."""
    return {"configured": _google_configured(),
            "unlocked": _google_configured() and _meta_unlocked(request)}


@app.post("/api/meta/unlock")
async def api_meta_unlock(request: Request, pin: str = Form("")):
    # One PIN unlocks spend, whichever network it belongs to. Gating this
    # on Meta alone would have taken the Google tab down the day Meta gets
    # switched off, which is where this is heading.
    # The Staff tab uses the same PIN for edits, so it works with only the
    # PIN set, whether or not an ad network is connected.
    if not settings.meta_pin:
        return JSONResponse({"error": "not configured"}, status_code=404)
    ip = _client_ip(request)
    now = time.time()
    _pin_attempts[ip] = [t for t in _pin_attempts[ip] if now - t < PIN_WINDOW]
    if len(_pin_attempts[ip]) >= PIN_MAX_ATTEMPTS:
        return JSONResponse({"error": "too many attempts, try again later"},
                            status_code=429)
    if not _same_secret(pin.strip(), settings.meta_pin):
        _pin_attempts[ip].append(now)
        left = PIN_MAX_ATTEMPTS - len(_pin_attempts[ip])
        return JSONResponse({"error": "wrong pin", "attemptsLeft": max(left, 0)},
                            status_code=401)
    _pin_attempts.pop(ip, None)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        META_COOKIE, meta_signer.dumps("ok"), max_age=META_MAX_AGE,
        httponly=True, samesite="lax", secure=settings.cookie_secure,
    )
    return response


# ----------------------------------------------------------------------
# Staff
#
# Who the KPI pages track, their role and their photo. Anyone signed in can
# see the list; changing it needs the PIN, so a rep cannot take themselves off
# the board. Every change rebuilds TEAM_ROLES in place (staff.apply), so the
# Overview, Leaderboard, Rep drill-down and Trends follow on the next load.
# ----------------------------------------------------------------------
_ghl_people_cache: dict[str, Any] = {"at": 0.0, "people": []}


def _ghl_people() -> list[dict[str, str]]:
    """GHL users as people: one row per person, duplicate logins merged."""
    if ghl_client is None:
        return []
    now = time.time()
    if now - _ghl_people_cache["at"] < 300:
        return _ghl_people_cache["people"]
    import activity as activity_mod
    users = ghl_client.users()
    ids, _numbers = activity_mod.canonical_users(
        users, normalize_team_names, settings.ghl_location_id)
    people: dict[str, dict[str, str]] = {}
    for u in users:
        email = ids.get(str(u.get("id") or ""))
        if not email or email in people:
            continue
        name = (u.get("name") or f"{u.get('firstName') or ''} "
                f"{u.get('lastName') or ''}").strip()
        people[email] = {"email": email, "ghlName": name, "ghlEmail":
                         str(u.get("email") or "").strip().lower()}
    _ghl_people_cache.update(at=now, people=list(people.values()))
    return _ghl_people_cache["people"]


def _staff_payload(request: Request) -> dict[str, Any]:
    rows = {r["email"]: r for r in store.staff_all()}
    error = None
    try:
        ghl = {p["email"]: p for p in _ghl_people()}
    except Exception as exc:  # noqa: BLE001 - the stored list still shows
        ghl, error = {}, f"Could not read GHL users: {exc}"
    people = []
    for email in sorted(set(rows) | set(ghl)):
        row, g = rows.get(email), ghl.get(email, {})
        role = (row or {}).get("role")
        people.append({
            "email": email,
            "name": display_name(email) if (email in normalize_team_names)
                    else ((row or {}).get("name") or g.get("ghlName") or display_name(email)),
            "role": role,
            "roleLabel": staff_mod.ROLES.get(role or "", "Not set"),
            "tracked": bool(row and row["tracked"] and role in staff_mod.TRACKABLE),
            "booksFor": (row or {}).get("booksFor"),
            "photo": staff_mod.photo_url(email, rows),
            "inGhl": email in ghl,
            "saved": row is not None,
        })
    order = {"closer": 0, "va": 1, "manager": 2, None: 3}
    people.sort(key=lambda p: (not p["tracked"], order.get(p["role"], 3), p["name"]))
    return {
        "people": people,
        "roles": staff_mod.ROLES,
        "pinConfigured": bool(settings.meta_pin),
        "canEdit": bool(settings.meta_pin) and _meta_unlocked(request),
        "error": error,
    }


def _staff_guard(request: Request) -> JSONResponse | None:
    if not settings.meta_pin:
        return JSONResponse({"error": "Set META_PIN in Railway to allow staff edits."},
                            status_code=403)
    if not _meta_unlocked(request):
        return JSONResponse({"error": "pin required"}, status_code=403)
    return None


def _known_staff_email(email: str) -> bool:
    if any(r["email"] == email for r in store.staff_all()):
        return True
    try:
        return any(p["email"] == email for p in _ghl_people())
    except Exception:  # noqa: BLE001
        return False


@app.get("/staff", response_class=HTMLResponse)
async def staff_page():
    return _page("staff.html")


@app.get("/api/staff")
def api_staff(request: Request):
    return _staff_payload(request)


@app.post("/api/staff")
async def api_staff_save(request: Request):
    """Save one person: role, tracked, books-for. JSON body."""
    blocked = _staff_guard(request)
    if blocked:
        return blocked
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "bad request"}, status_code=400)
    email = str(body.get("email") or "").strip().lower()
    role = str(body.get("role") or "").strip().lower()
    if role not in staff_mod.ROLES:
        return JSONResponse({"error": "Pick a role."}, status_code=400)
    if not email or not _known_staff_email(email):
        return JSONResponse({"error": "Unknown person."}, status_code=404)
    existing = next((r for r in store.staff_all() if r["email"] == email), None)
    name = (existing or {}).get("name")
    if not name:
        name = next((p["ghlName"] for p in _ghl_people_safe() if p["email"] == email), None)
    tracked = bool(body.get("tracked")) and role in staff_mod.TRACKABLE
    books_for = (str(body.get("booksFor") or "").strip().lower() or None) if role == "va" else None
    store.staff_upsert(email, name, role, tracked, books_for)
    staff_mod.apply(store)
    log.info("staff: %s -> role=%s tracked=%s books_for=%s", email, role, tracked, books_for)
    return _staff_payload(request)


def _ghl_people_safe() -> list[dict[str, str]]:
    try:
        return _ghl_people()
    except Exception:  # noqa: BLE001
        return []


@app.post("/api/staff/photo")
async def api_staff_photo(request: Request):
    """Set or clear a photo. JSON {email, photo: data URL | null}."""
    blocked = _staff_guard(request)
    if blocked:
        return blocked
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "bad request"}, status_code=400)
    email = str(body.get("email") or "").strip().lower()
    if not email or not _known_staff_email(email):
        return JSONResponse({"error": "Unknown person."}, status_code=404)
    if not any(r["email"] == email for r in store.staff_all()):
        # A photo for someone never saved: give them a row first, untracked.
        name = next((p["ghlName"] for p in _ghl_people_safe() if p["email"] == email), None)
        store.staff_upsert(email, name, "closer", False, None)
    photo = body.get("photo")
    if photo:
        try:
            stored = staff_mod.decode_photo(str(photo))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        store.staff_set_photo(email, stored)
    else:
        store.staff_set_photo(email, None)
    return _staff_payload(request)


@app.get("/staff-photo/{key}")
def staff_photo(key: str):
    for row in store.staff_all():
        if row["hasPhoto"] and staff_mod.photo_key(row["email"]) == key:
            stored = store.staff_photo(row["email"])
            if stored:
                data, mime = staff_mod.encode_photo(stored)
                # The URL carries ?v=<version>, so it can be cached hard.
                return Response(data, media_type=mime, headers={
                    "Cache-Control": "private, max-age=31536000, immutable"})
    return Response(status_code=404)


@app.get("/api/meta/verify")
async def api_meta_verify(request: Request):
    """Is the Meta setup working? Names the missing step where it can."""
    blocked = _meta_guard(request)
    if blocked:
        return blocked
    return {**meta_client.check(), "apiVersion": settings.meta_api_version}


def _norm_campaign(name: str | None) -> str:
    """A campaign string reduced to something two systems can be matched on.

    The same campaign is written "FB #15  - (1-2) -  $25 …" in one place and
    "FB #15 - (1-2) - $25 …" in another - the real exports have doubled
    spaces - so case and whitespace are flattened before comparing.
    """
    return " ".join((name or "").strip().lower().split())


# Where leads with no campaign tag at all are kept. They used to be dropped on
# the floor, which is how the Meta tab came to disagree with the Overview about
# how many contracts the month had: Overview counts every lead created in the
# range, the Meta tab counted only leads it could attribute, and nothing on
# either page said so. They are still kept OUT of the cost-per tiles - dividing
# Meta's spend by a contract Meta may not have produced flatters the number -
# but they are now counted, reported, and visible.
UNTAGGED = "\x00untagged"


def _outcomes_by_campaign(leads: list[dict]) -> dict[str, dict]:
    """What each campaign's leads actually turned into, keyed for matching.

    This is the half Meta cannot know. Meta counts a form submission and stops;
    whether that lead was real, got qualified, got an offer, is only in
    InvestorFuse. Joining the two is the entire point of the page.

    Leads with no campaign tag land under ``UNTAGGED`` rather than being
    skipped. The key is the empty string, which no real campaign normalises to,
    so it cannot be matched to a Meta campaign by accident.
    """
    out: dict[str, dict] = {}
    for lead in leads:
        raw = lead.get("campaign")
        # Not the empty string: "" is what a missing key defaults to all over
        # this file, and using it here meant any Meta campaign the join could
        # not match silently adopted every untagged lead in the account - its
        # `outcomes.get(if_key)` found the untagged bucket instead of nothing.
        key = _norm_campaign(raw) or UNTAGGED
        row = out.setdefault(key, {
            "campaign": raw, "leads": 0, "qualified": 0, "offers": 0,
            "contracts": 0, "invalid": 0, "contacted": 0,
        })
        row["leads"] += 1
        bucket = funnel_mod.classify(lead)
        # The funnel's own per-lead rules, so the Meta tab, the Google tab
        # and the Overview count a step the same way: ever qualified, not
        # "sitting in a qualified stage today" - a lead that qualified and
        # was later disqualified still cost what a qualified lead costs.
        if funnel_mod.reached_qualified(lead, bucket):
            row["qualified"] += 1
        if bucket == "invalid":
            row["invalid"] += 1
        if funnel_mod.is_contacted(lead, bucket):
            row["contacted"] += 1
        # Same definition as the funnel: an offer is one that was recorded,
        # not a lead parked in the offer sub-stage.
        if lead.get("offer_ms"):
            row["offers"] += 1
        if funnel_mod.is_contract(lead):
            row["contracts"] += 1
    return out


def _per(cost: float, count: float) -> float | None:
    """Cost per something, or None when there is no something to divide by."""
    return round(cost / count, 2) if count else None


# Meta has to report at least this many leads before "the CRM has none" means
# anything. Below it, one seller filling the form twice explains the whole gap.
TRACKING_MIN_LEADS = 3
# Below this share, the tag looks like it is set on some of a campaign's forms
# and not others. Generous, because resurfaced opportunities legitimately eat
# into the CRM's count and nobody should be sent chasing a working campaign.
TRACKING_PARTIAL = 0.4


def _tracking_verdict(meta_leads: int, if_leads: int | None) -> str:
    """``off`` / ``partial`` / ``ok`` / ``unknown`` for one campaign's tagging.

    ``off`` is the one to act on: Meta delivered leads and not one of them
    reached InvestorFuse carrying this campaign's name, which is what an
    unconfigured tag looks like from the outside.
    """
    if meta_leads < TRACKING_MIN_LEADS:
        return "unknown"                     # too few leads to conclude either way
    if not if_leads:
        return "off"
    if if_leads < meta_leads * TRACKING_PARTIAL:
        return "partial"
    return "ok"


# --- Matching Meta campaigns to InvestorFuse campaign tags -------------
#
# The two systems hold the same campaign under names that are only nearly
# identical: Meta has the name as typed in Ads Manager, InvestorFuse has
# whatever the lead form passed through, and somewhere between them a rename,
# a doubled space or a trailing suffix creeps in. So matching runs in three
# passes, strictest first, and every row records WHICH pass caught it - a
# number-matched row is a guess worth flagging, not a fact.
CAMPAIGN_NUMBER = re.compile(r"#\s*(\d+)")
CAMPAIGN_VARIANT = re.compile(r"\(\s*(\d+)\s*-\s*(\d+)\s*\)")


def _campaign_number(name: str) -> str:
    """The identifying part of a campaign name: ``FB #15 - (1-4) - …`` -> ``15/1-4``.

    Not just the ``#15``. These campaigns come in variants that share a number
    and differ only in the ``(1-2)`` / ``(1-4)`` group, so matching on the
    number alone would happily join two different ad sets - and the two are
    the whole reason anyone compares campaigns in the first place.
    """
    found = CAMPAIGN_NUMBER.search(name or "")
    if not found:
        return ""
    variant = CAMPAIGN_VARIANT.search(name or "")
    return found.group(1) + (f"/{variant.group(1)}-{variant.group(2)}"
                             if variant else "")


def _match_campaigns(meta_keys: list[str],
                     if_keys: list[str]) -> dict[str, tuple[str, str]]:
    """``{meta_key: (if_key, how)}`` for the campaigns the two systems share."""
    matched: dict[str, tuple[str, str]] = {}
    # The untagged bucket is not a campaign and must never be matched to one -
    # a Meta campaign whose name happened to normalise to nothing would
    # otherwise adopt every untagged lead in the account.
    unclaimed = {k for k in if_keys if k != UNTAGGED}

    for key in meta_keys:                                  # 1. same name
        if key in unclaimed:
            matched[key] = (key, "exact")
            unclaimed.discard(key)

    by_number: dict[str, list[str]] = {}
    for key in unclaimed:
        number = _campaign_number(key)
        if number:
            by_number.setdefault(number, []).append(key)
    for key in meta_keys:                                  # 2. same "#15"
        if key in matched:
            continue
        candidates = by_number.get(_campaign_number(key) or "\x00", [])
        if len(candidates) == 1:                           # ambiguous = no match
            matched[key] = (candidates[0], "number")
            unclaimed.discard(candidates[0])

    for key in meta_keys:                                  # 3. one inside other
        if key in matched:
            continue
        hits = [c for c in unclaimed if c and (c in key or key in c)]
        if len(hits) == 1:
            matched[key] = (hits[0], "contains")
            unclaimed.discard(hits[0])
    return matched


def _rate(part: float, whole: float) -> float | None:
    return round(part / whole * 100, 1) if whole else None


def _meta_row(row: dict) -> dict:
    """The numbers every level of the report shares, cleaned of Meta's strings."""
    spend = _num(row.get("spend"))
    leads = leads_from(row)
    return {
        "spend": round(spend, 2),
        "leads": int(leads),
        "cpl": _per(spend, leads),
        "impressions": int(_num(row.get("impressions"))),
        "clicks": int(_num(row.get("clicks"))),
        "ctr": round(_num(row.get("ctr")), 2),
        "cpm": round(_num(row.get("cpm")), 2),
        "cpc": round(_num(row.get("cpc")), 2),
    }


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# Meta's insights endpoint is slow - a few seconds per call, more with
# pagination - and a report needs three of them. Two things fix the lag: fire
# the three at once instead of in sequence, and keep the answer for a while.
#
# Caching costs nothing real here. Meta's own reporting lags by minutes to
# hours, so a number that is ten minutes old is not meaningfully staler than
# a number fetched this second - it just looks it. The page shows when the
# data was actually pulled rather than implying it is live, and the refresh
# button forces a new fetch for people who want one.
META_CACHE_TTL = 600                 # ten minutes
_meta_cache: dict[tuple[str, str], tuple[float, tuple, str]] = {}
_meta_fetch_lock = threading.Lock()


def _previous_range(day_from: date, day_to: date) -> tuple[str, str]:
    """The equally long stretch of days immediately before a range.

    The same rule the rest of the dashboard compares against, so "vs last
    period" means one thing everywhere: last week compares with the week
    before, a hand-typed nine days with the nine before those.
    """
    span = (day_to - day_from).days + 1
    prev_to = day_from - timedelta(days=1)
    return (prev_to - timedelta(days=span - 1)).isoformat(), prev_to.isoformat()


def _meta_insights(since: str, until: str, prev_since: str, prev_until: str,
                   fresh: bool = False):
    """``((cur_totals, cur_campaigns, ads, prev_totals, prev_campaigns), when)``.

    All five calls go out at once. Sequentially this is the slowest thing on
    the dashboard by an order of magnitude; in parallel it costs about as long
    as the slowest single call, and the cache means only the first look at a
    given range pays even that.

    Ad-level rows are fetched for the current period only. Nothing on the page
    compares ads across periods, and it is the heaviest of the calls.
    """
    key = (since, until)
    now = time.time()
    if not fresh:
        hit = _meta_cache.get(key)
        if hit and now - hit[0] < META_CACHE_TTL:
            return hit[1], hit[2]

    # One fetch at a time. Without this, four people opening the tab at once
    # (or one person clicking through presets) each start their own round of
    # API calls, which is how an account meets Meta's rate limit.
    with _meta_fetch_lock:
        hit = _meta_cache.get(key)
        if not fresh and hit and time.time() - hit[0] < META_CACHE_TTL:
            return hit[1], hit[2]        # someone else fetched it while waiting
        with ThreadPoolExecutor(max_workers=5) as pool:
            jobs = (
                pool.submit(meta_client.account_totals, since, until),
                pool.submit(meta_client.by_campaign, since, until),
                pool.submit(meta_client.by_ad, since, until),
                pool.submit(meta_client.account_totals, prev_since, prev_until),
                pool.submit(meta_client.by_campaign, prev_since, prev_until),
            )
            rows = tuple(job.result() for job in jobs)
        stamp = datetime.now(settings.tz).isoformat(timespec="seconds")
        _meta_cache[key] = (time.time(), rows, stamp)
        # Ranges are cheap to hold but there is no reason to hold every one
        # somebody ever typed into the date box.
        if len(_meta_cache) > 40:
            oldest = min(_meta_cache, key=lambda k: _meta_cache[k][0])
            _meta_cache.pop(oldest, None)
        return rows, stamp


# Copy performance is its own endpoint and its own cache, not part of the five
# calls above. It costs five more API calls and most visits to the Meta tab do
# not open it, so making the main tab wait for it would slow the common case to
# pay for the rare one.
_copy_cache: dict[tuple[str, str], tuple[float, tuple, str]] = {}
_copy_fetch_lock = threading.Lock()


def _meta_copy_rows(since: str, until: str, fresh: bool = False):
    """``((asset_rows_by_kind, ad_rows, creatives), fetched_at)``.

    The raw rows are cached, not the rankings, so switching the campaign
    filter re-aggregates from memory instead of going back to Meta.
    """
    key = (since, until)
    if not fresh:
        hit = _copy_cache.get(key)
        if hit and time.time() - hit[0] < META_CACHE_TTL:
            return hit[1], hit[2]

    with _copy_fetch_lock:
        hit = _copy_cache.get(key)
        if not fresh and hit and time.time() - hit[0] < META_CACHE_TTL:
            return hit[1], hit[2]
        with ThreadPoolExecutor(max_workers=5) as pool:
            asset_jobs = {
                kind: pool.submit(meta_client.asset_rows, since, until, kind)
                for kind in metacopy.KINDS
            }
            ads_job = pool.submit(meta_client.by_ad, since, until)
            creatives_job = pool.submit(meta_client.ad_creatives)

            assets: dict[str, list] = {}
            for kind, job in asset_jobs.items():
                try:
                    assets[kind] = job.result()
                except MetaError as exc:
                    # One breakdown refused should not lose the other two.
                    # An account with no Dynamic Creative ads is the ordinary
                    # case here, and the page says so rather than erroring.
                    log.info("meta asset breakdown %s unavailable: %s", kind, exc)
                    assets[kind] = []
            ad_rows = ads_job.result()
            try:
                creatives = creatives_job.result()
            except MetaError as exc:
                # Without creatives only the Dynamic Creative half works. Still
                # worth showing, so this degrades rather than fails.
                log.info("meta ad creatives unavailable: %s", exc)
                creatives = {}

        rows = (assets, ad_rows, creatives)
        stamp = datetime.now(settings.tz).isoformat(timespec="seconds")
        _copy_cache[key] = (time.time(), rows, stamp)
        if len(_copy_cache) > 20:
            oldest = min(_copy_cache, key=lambda k: _copy_cache[k][0])
            _copy_cache.pop(oldest, None)
        return rows, stamp


@app.get("/api/meta/copy")
def api_meta_copy(request: Request):
    """Headlines, primary text and descriptions ranked by what they earned.

    No previous-period comparison here, unlike the campaign view. A single
    headline variant in a week is a handful of leads, and a week-on-week
    arrow on a handful of leads is noise wearing a trend's clothes. The lead
    floor does the honest version of the same job: it says which rows are
    worth believing at all.
    """
    blocked = _meta_guard(request)
    if blocked:
        return blocked

    day_from, day_to = _range(request, 30)
    since, until = day_from.isoformat(), day_to.isoformat()
    fresh = request.query_params.get("fresh") == "1"
    campaign = (request.query_params.get("campaign") or "").strip() or None

    try:
        (assets, ad_rows, creatives), fetched_at = _meta_copy_rows(
            since, until, fresh)
    except MetaError as exc:
        return {"error": str(exc), "hint": metaclient.MetaClient._diagnose(exc),
                "range": {"from": since, "to": until}}

    payload = metacopy.build(assets, ad_rows, creatives,
                             metaclient.LEAD_ACTIONS, campaign=campaign)
    payload["range"] = {"from": since, "to": until}
    payload["fetchedAt"] = fetched_at
    payload["cacheMinutes"] = META_CACHE_TTL // 60
    payload["goals"] = {"cplLow": settings.meta_cpl_low,
                        "cplHigh": settings.meta_cpl_high}
    payload["labels"] = metacopy.KIND_LABELS
    return payload


def _roll_up_campaigns(rows: list[dict]) -> dict[str, dict]:
    """Insights rows grouped by campaign, keyed for matching.

    Meta can return the same campaign name more than once - a campaign renamed
    mid-range, or two campaigns genuinely sharing a name - and the page treats
    those as one line, so the rows are kept together to be added up rather than
    the last one silently winning.
    """
    out: dict[str, dict] = {}
    for row in rows:
        key = _norm_campaign(row.get("campaign_name"))
        if not key:
            continue
        existing = out.get(key)
        if existing is None:
            out[key] = {"name": row.get("campaign_name"),
                        "id": row.get("campaign_id"), "rows": [row]}
        else:
            existing["rows"].append(row)
    return out


def _meta_pool(lead: dict[str, Any]) -> bool:
    return _ads_source(lead) == "meta"


@app.get("/api/meta")
def api_meta(request: Request):
    """Spend from Meta, outcomes from InvestorFuse, joined on campaign name.

    Deliberately a sync ``def``: the Meta calls are blocking HTTP and three of
    them run per request, so FastAPI should hand this to a worker thread rather
    than stall the event loop for everything else on the dashboard.
    """
    blocked = _meta_guard(request)
    if blocked:
        return blocked

    day_from, day_to = _range(request, 30)
    since, until = day_from.isoformat(), day_to.isoformat()
    fresh = request.query_params.get("fresh") == "1"
    prev_since, prev_until = _previous_range(day_from, day_to)

    try:
        rows, fetched_at = _meta_insights(since, until, prev_since, prev_until,
                                          fresh)
        (totals_row, campaign_rows, ad_rows,
         prev_totals_row, prev_campaign_rows) = rows
    except MetaError as exc:
        # A 200 with an error inside, on purpose: the page can then show the
        # message next to a "check setup" link instead of a blank failure.
        return JSONResponse({
            "range": {"from": since, "to": until},
            "error": str(exc),
            "hint": "Open /api/meta/verify for which setup step is missing.",
        }, status_code=200)

    totals = _meta_row(totals_row)
    totals["reach"] = int(_num(totals_row.get("reach")))
    totals["frequency"] = round(_num(totals_row.get("frequency")), 2)

    # The InvestorFuse half. Cohort, not activity: leads CREATED in the range,
    # which is the only population comparable to what Meta spent in the range.
    # Meta's own leads only. This used to take every tagged lead, so Google
    # leads were counted as "landed in the CRM" from Meta. Untagged
    # InvestorFuse-era leads stay in for the untagged note.
    cohort_rows = [r for r in store.leads_between(since, until) if _meta_pool(r)]
    outcomes = _outcomes_by_campaign(cohort_rows)
    # Repeat form fills on opportunities that already existed. Meta counted
    # each of these as a lead and billed for it; the CRM created no new record.
    returning_count = collector.resurfaced_in(day_from, day_to)

    meta_by_key = _roll_up_campaigns(campaign_rows)
    prev_by_key = _roll_up_campaigns(prev_campaign_rows)
    prev_outcomes = _outcomes_by_campaign(
        [r for r in store.leads_between(prev_since, prev_until) if _meta_pool(r)]
    )
    prev_matches = _match_campaigns(list(prev_by_key), list(prev_outcomes))

    matches = _match_campaigns(list(meta_by_key), list(outcomes))

    campaigns = []
    for key, meta in meta_by_key.items():
        merged: dict = {"actions": [], "spend": 0.0, "impressions": 0.0,
                        "clicks": 0.0}
        lead_total = 0.0
        for row in meta["rows"]:
            merged["spend"] += _num(row.get("spend"))
            merged["impressions"] += _num(row.get("impressions"))
            merged["clicks"] += _num(row.get("clicks"))
            lead_total += leads_from(row)
        entry = {
            "campaign": meta["name"],
            "campaignId": meta["id"],
            "spend": round(merged["spend"], 2),
            "leads": int(lead_total),
            "cpl": _per(merged["spend"], lead_total),
            "impressions": int(merged["impressions"]),
            "clicks": int(merged["clicks"]),
            "ctr": _rate(merged["clicks"], merged["impressions"]),
            "cpm": _per(merged["spend"], merged["impressions"] / 1000
                        if merged["impressions"] else 0),
        }
        if_key, how = matches.get(key, (None, None))
        found = outcomes.get(if_key) if if_key else None
        if found:
            entry["matchedBy"] = how
            entry["ifCampaign"] = found["campaign"]
            entry["ifLeads"] = found["leads"]
            entry["contacted"] = found["contacted"]
            entry["qualified"] = found["qualified"]
            entry["offers"] = found["offers"]
            entry["contracts"] = found["contracts"]
            entry["invalid"] = found["invalid"]
            entry["qualifyRate"] = _rate(found["qualified"], found["leads"])
            entry["junkRate"] = _rate(found["invalid"], found["leads"])
            # The numbers worth the whole page: not what a lead cost, but what
            # a lead that turned into something cost.
            entry["costPerQualified"] = _per(merged["spend"], found["qualified"])
            entry["costPerOffer"] = _per(merged["spend"], found["offers"])
            entry["costPerContract"] = _per(merged["spend"], found["contracts"])
        else:
            entry["matchedBy"] = None

        # Is this campaign's tagging working?
        #
        # InvestorFuse sets the campaign tag per campaign, so when it is not
        # set up the symptom is stark: Meta reports leads and the CRM holds
        # none carrying that campaign's name. This is the only way to find out
        # WHICH campaign is untagged - an untagged lead, by definition, does
        # not say where it came from, so it has to be inferred from the
        # campaign that is missing its leads rather than from the leads.
        #
        # Meta's count always runs a little ahead of the CRM's: a seller who
        # fills the form twice is two leads to Meta and one resurfaced
        # opportunity to InvestorFuse, and the last day of a range may not have
        # synced. So a shortfall is normal and only ZERO is damning, and even
        # then not on one or two leads.
        entry["tracking"] = _tracking_verdict(int(lead_total),
                                              entry.get("ifLeads"))

        # What the same campaign did last period, so the page can say which
        # way it is moving rather than only where it stands. Absent when the
        # campaign did not run then - "new" is a different fact from "flat",
        # and the client draws it differently.
        was = prev_by_key.get(key)
        if was:
            prev_spend = sum(_num(r.get("spend")) for r in was["rows"])
            prev_leads = sum(leads_from(r) for r in was["rows"])
            prev_key, _ = prev_matches.get(key, (None, None))
            prev_found = prev_outcomes.get(prev_key) if prev_key else None
            entry["prev"] = {
                "spend": round(prev_spend, 2),
                "leads": int(prev_leads),
                "cpl": _per(prev_spend, prev_leads),
                "qualified": (prev_found or {}).get("qualified"),
                "costPerQualified": _per(prev_spend,
                                         (prev_found or {}).get("qualified", 0)),
            }
        campaigns.append(entry)

    campaigns.sort(key=lambda c: (-(c.get("spend") or 0), c["campaign"] or ""))

    # Campaigns InvestorFuse has leads for that Meta never mentioned: either
    # the tag is a name Meta does not use, or those leads are not from Meta at
    # all. Shown rather than dropped, because a silent drop is how a channel
    # goes unnoticed for a quarter.
    claimed = {if_key for if_key, _ in matches.values()}
    unmatched = [
        {"campaign": row["campaign"], "leads": row["leads"],
         "qualified": row["qualified"], "offers": row["offers"],
         "contracts": row["contracts"]}
        for key, row in outcomes.items()
        if key not in claimed and key != UNTAGGED
    ]
    unmatched.sort(key=lambda r: -r["leads"])

    # The campaign table's footer adds up only what is in the table. The tiles
    # above are account-wide, so the two will differ whenever something did not
    # join - which is exactly when the difference is worth seeing, and why the
    # unmatched block sits right underneath.
    matched_totals = {"spend": 0.0, "leads": 0, "ifLeads": 0, "qualified": 0,
                      "offers": 0, "contracts": 0, "invalid": 0,
                      "impressions": 0, "clicks": 0}
    for entry in campaigns:
        matched_totals["spend"] += entry["spend"]
        matched_totals["leads"] += entry["leads"]
        matched_totals["impressions"] += entry["impressions"]
        matched_totals["clicks"] += entry["clicks"]
        for field in ("ifLeads", "qualified", "offers", "contracts", "invalid"):
            matched_totals[field] += entry.get(field) or 0
    matched_totals["spend"] = round(matched_totals["spend"], 2)
    matched_totals["cpl"] = _per(matched_totals["spend"], matched_totals["leads"])
    matched_totals["costPerQualified"] = _per(matched_totals["spend"],
                                              matched_totals["qualified"])
    matched_totals["qualifyRate"] = _rate(matched_totals["qualified"],
                                          matched_totals["ifLeads"])
    matched_totals["junkRate"] = _rate(matched_totals["invalid"],
                                       matched_totals["ifLeads"])

    ads = []
    for row in ad_rows:
        entry = _meta_row(row)
        entry.update({
            "ad": row.get("ad_name"),
            "adId": row.get("ad_id"),
            "adset": row.get("adset_name"),
            "campaign": row.get("campaign_name"),
        })
        if entry["spend"] or entry["impressions"]:
            ads.append(entry)
    # Best = most leads, then cheapest per lead. An ad with no leads yet sorts
    # last however cheap its clicks are, which is the honest order.
    ads.sort(key=lambda a: (-a["leads"], a["cpl"] if a["cpl"] is not None
                            else float("inf")))

    # Tagged leads only. These divide Meta's spend, and a lead with no campaign
    # on it is a lead nobody can say Meta paid for.
    if_totals = {"leads": 0, "qualified": 0, "offers": 0, "contracts": 0,
                 "invalid": 0, "contacted": 0}
    # Untagged rows count: every lead in this pool is Meta's (see _ads_source -
    # in the InvestorFuse era Meta was the only intake; its tagging broke).
    for key, row in outcomes.items():
        for field in if_totals:
            if_totals[field] += row[field]

    # Reported separately, so the gap against the Overview tiles has a number
    # and a name instead of being something you notice and cannot explain.
    untagged_row = outcomes.get(UNTAGGED)
    untagged = None   # counted as Meta above, so no "left out" note
    _ = untagged_row
    totals["ifLeads"] = if_totals["leads"]
    totals["qualified"] = if_totals["qualified"]
    totals["offers"] = if_totals["offers"]
    totals["contracts"] = if_totals["contracts"]
    totals["invalid"] = if_totals["invalid"]
    totals["costPerQualified"] = _per(totals["spend"], if_totals["qualified"])
    totals["costPerOffer"] = _per(totals["spend"], if_totals["offers"])
    totals["costPerContract"] = _per(totals["spend"], if_totals["contracts"])
    totals["qualifyRate"] = _rate(if_totals["qualified"], if_totals["leads"])

    prev_totals = _meta_row(prev_totals_row)
    prev_if = {"qualified": 0, "contracts": 0, "leads": 0}
    for key, row in prev_outcomes.items():
        for field in prev_if:
            prev_if[field] += row[field]
    prev_totals["ifLeads"] = prev_if["leads"]
    prev_totals["qualified"] = prev_if["qualified"]
    prev_totals["contracts"] = prev_if["contracts"]
    prev_totals["costPerQualified"] = _per(prev_totals["spend"],
                                           prev_if["qualified"])
    prev_totals["costPerContract"] = _per(prev_totals["spend"],
                                          prev_if["contracts"])
    prev_totals["frequency"] = round(_num(prev_totals_row.get("frequency")), 2)

    return {
        "range": {"from": since, "to": until},
        "previousRange": {"from": prev_since, "to": prev_until},
        "totals": totals,
        "prevTotals": prev_totals,
        "campaigns": campaigns,
        "campaignTotals": matched_totals,
        "unmatched": unmatched,
        "untagged": untagged,
        # Where Meta's lead count actually went. Meta charges for a form
        # submission; InvestorFuse creates an opportunity only for a seller it
        # has not seen before, and logs a repeat against the existing record.
        # So Meta's number is always the larger one, and until this line
        # existed the difference was a mystery you could only notice.
        #
        # The last term is the one that matters: a submission Meta billed for
        # that produced neither a new opportunity nor a repeat on an old one
        # did not reach the CRM at all. That is a paid lead nobody is calling,
        # and it is worth more attention than everything else on this page.
        "reconcile": {
            "metaLeads": int(totals.get("leads") or 0),
            "created": len(cohort_rows),
            "returning": returning_count,
            "unaccounted": max(0, int(totals.get("leads") or 0)
                               - len(cohort_rows) - returning_count),
        },
        # The campaigns whose tagging looks unconfigured, named. This is what
        # goes in the support ticket: InvestorFuse sets the tag per campaign,
        # so the fix is a list of campaign names, not a list of lead ids.
        "trackingOff": [
            {"campaign": c["campaign"], "leads": c["leads"],
             "ifLeads": c.get("ifLeads") or 0, "spend": c["spend"]}
            for c in campaigns if c["tracking"] == "off"
        ],
        "trackingPartial": [
            {"campaign": c["campaign"], "leads": c["leads"],
             "ifLeads": c.get("ifLeads") or 0, "spend": c["spend"]}
            for c in campaigns if c["tracking"] == "partial"
        ],
        "ads": ads[:40],
        "adCount": len(ads),
        "fetchedAt": fetched_at,
        "cacheMinutes": round(META_CACHE_TTL / 60),
        "goals": {
            "cplLow": settings.meta_cpl_low, "cplHigh": settings.meta_cpl_high,
            "ctrMin": settings.meta_ctr_min, "cpmMax": settings.meta_cpm_max,
            "freqLow": settings.meta_freq_low, "freqHigh": settings.meta_freq_high,
        },
    }



# ----------------------------------------------------------------------
# Data for the new views
# ----------------------------------------------------------------------
def _scorecard_rows(day_from: date, day_to: date, with_old: bool = False) -> list[tuple]:
    """Stored scorecard rows for a range, minus the ones logged against nobody.

    Filtered on read, not just on write: rows with a literal "null" user were
    collected for months before that was caught, and they are still in the
    database. Dropping them here fixes the history too.
    """
    # "old.<metric>" rows are the old-lead share of a metric, already inside
    # the plain total. Only the Overview split asks for them.
    return [r for r in store.range_rows(day_from.isoformat(), day_to.isoformat())
            if is_real_user(r[1]) and (with_old or not r[2].startswith("old."))]


def _call_attempted(leads: list[dict]) -> dict[str, float] | None:
    """``{"count": 7, "avgTouches": 13.7}`` for a set of leads, or None if none.

    The dialled-but-never-answered pile, which is the one funnel bucket a lead
    manager's work lands in - they are the ones climbing the call ladder. Given
    the CLOSER's leads, not the manager's, because a lead manager owns nothing:
    the leads they work are the ones their closer ends up owning, which is the
    same pool the "Leads assigned to …" box above counts. Two boxes drawn from
    two different populations would be worse than no second box.
    """
    if not leads:
        return None
    built = funnel_mod.build(leads)
    count = built["buckets"].get("call_attempted", 0)
    if not count:
        return None
    return {"count": count, "avgTouches": built["avgTouches"].get("call_attempted")}


def _rep_rows(day_from: date, day_to: date) -> dict[str, dict[str, float]]:
    by_user: dict[str, dict[str, float]] = {}
    for _day, user, metric, value in _scorecard_rows(day_from, day_to):
        by_user.setdefault(user, {})
        by_user[user][metric] = by_user[user].get(metric, 0.0) + value
    return {u: with_claimed(m) for u, m in by_user.items()}


@app.post("/api/refresh")
async def api_refresh():
    """Pull from InvestorFuse right now instead of waiting for the timer."""
    started = collector.refresh_now()
    return {
        "started": started,
        "alreadyRunning": not started,
        "lastSuccess": store.get_meta("last_success"),
    }


@app.get("/api/refresh")
async def api_refresh_status():
    return {
        "refreshing": collector.refreshing,
        "lastSuccess": store.get_meta("last_success"),
        "lastError": store.get_meta("last_error") or None,
    }


@app.get("/api/leaderboard")
async def api_leaderboard(request: Request):
    """Standings for any date range.

    Closers and lead managers are ranked separately and on different measures.
    A closer is judged on contracts; a lead manager's job is getting sellers on
    the phone, so they are judged on conversations. Putting both in one list on
    one number would just park the lead managers at the bottom of a column
    their job doesn't produce.

    The tiebreak is the next thing down each role's own funnel: for a closer,
    the offers behind the contracts; for a lead manager, the appointments that
    came out of the conversations.

    This used to take a month and nothing else. It reads from/to like every
    other endpoint now, because a month is too long to coach on - by the time
    the standings mean anything the month is over.
    """
    day_from, day_to = _range(request, default_days=7)
    by_user = _rep_rows(day_from, day_to)
    # The whole roster, zeros included, like the Overview.
    for email in TEAM_ROLES:
        by_user.setdefault(email, with_claimed({}))

    staff_rows = {r["email"]: r for r in store.staff_all()}
    # Speed to lead per person: median business minutes from a lead arriving
    # to that person's first call or text, for leads created in the range.
    speed_by: dict[str, float | None] = {}
    try:
        sp = _speed(store.leads_between(day_from.isoformat(), day_to.isoformat()))
        speed_by = {r["email"]: r.get("median") for r in (sp or {}).get("byRep", [])}
    except Exception:  # noqa: BLE001 - the boards render without it
        log.exception("leaderboard speed to lead failed")

    def board(role: str, rank_metric: str, tiebreak: str):
        people = [
            {
                "email": email,
                "name": display_name(email),
                "photo": staff_mod.photo_url(email, staff_rows),
                "speed": speed_by.get(email),
                "title": role_of(email).get("title", ""),
                "booksFor": (display_name(role_of(email)["books_for"])
                             if role_of(email).get("books_for") else None),
                "score": metrics.get(rank_metric, 0.0),
                "metrics": metrics,
            }
            for email, metrics in by_user.items()
            if role_of(email).get("role") == role
        ]
        people.sort(key=lambda p: (-p["score"], -p["metrics"].get(tiebreak, 0)))
        return people

    return {
        "range": {"from": day_from.isoformat(), "to": day_to.isoformat()},
        "closers": board("closer", "contractsAccepted", "verbalOffersMade"),
        "closerMetrics": CLOSER_METRICS,
        "closerRank": "contractsAccepted",
        # Tie on outbound calls: appointments need GHL calendars, not set up yet.
        "vas": board("va", "contacts", "outboundCalls"),
        "vaMetrics": VA_METRICS,
        "vaRank": "contacts",
        "metricLabels": METRIC_LABELS,
        "freshness": _freshness(),
    }


def _goal_gaps(funnel: dict[str, Any]) -> list[dict[str, Any]]:
    """The three conversion rates against their targets.

    One stage each, in pipeline order, and every one is a ratio of two
    numbers the funnel already counted - nothing here re-derives a figure
    that exists above it, so a bar can never disagree with the pipeline it
    sits on top of.

    Each entry carries the counts as well as the rate, because "64 points
    short" lands differently once it is also "188 more leads would have had
    to qualify". `shortfall` is that number: how many more of the numerator
    the goal implies, holding the denominator where it is.
    """
    def stage(key, label, hint, got, of, goal_pct, noun):
        of = of or 0
        got = got or 0
        rate = (got / of * 100) if of else None
        need = math.ceil(of * goal_pct / 100) if of else 0
        return {
            "key": key, "label": label, "hint": hint,
            "actual": round(rate, 1) if rate is not None else None,
            "goal": round(goal_pct, 1),
            # Percentage POINTS, not percent - 16% against 80% is 64 points
            # apart, and calling that "64%" invites reading it as "64% of
            # the way there", which is the opposite of what it means.
            "gapPoints": (round(goal_pct - rate, 1)
                          if rate is not None else None),
            "met": rate is not None and rate >= goal_pct,
            "numerator": got, "denominator": of, "noun": noun,
            "needed": need,
            "shortfall": max(need - got, 0),
            # How full the bar is: the track runs 0 -> goal, so this is
            # progress toward the target rather than toward 100%. It keeps a
            # 6.7% goal and an 80% goal legible on the same screen.
            "progress": (round(min(rate / goal_pct, 1) * 100, 1)
                         if rate is not None and goal_pct else 0),
        }

    per_contract = settings.goal_qualified_per_contract or 15
    out = [
        stage("qualified", "Lead → Qualified + Contacted",
              "Leads created in this range that reached Qualified + Contacted",
              funnel.get("reachedQualified"), funnel.get("total"),
              settings.goal_qualified_rate, "leads"),
        stage("offer", "Qualified + Contacted → verbal offer",
              "Qualified + Contacted leads we put an offer in front of",
              funnel.get("offers"), funnel.get("reachedQualified"),
              settings.goal_offer_rate, "Qualified + Contacted leads"),
        stage("contract", "Qualified + Contacted → contract signed",
              f"Qualified + Contacted leads that signed · goal is 1 in {per_contract:g}",
              funnel.get("contracts"), funnel.get("reachedQualified"),
              100 / per_contract, "Qualified + Contacted leads"),
    ]
    # Drawn as N segments, one of which is the goal, rather than a 0-100% bar.
    out[2]["segments"] = int(round(per_contract))
    return out


# ----------------------------------------------------------------------
# Leaderboard: the leads behind each number, and the recent-wins feed.
# Both read the activity trail (collector.trace_activity) - the same replay
# that produces the scorecard - so a list always matches the number above it.
# ----------------------------------------------------------------------
LB_CATEGORIES = {
    "closer": [("contractsAccepted", "CB", "Contracts signed"),
               ("contractsSent", "CS", "Contracts sent"),
               ("verbalOffersMade", "offers", "Verbal offers"),
               ("leads", "leads", "Leads claimed")],
    "va": [("contacts", "Q+C", "Qualified + Contacted")],
}
LEAD_CLAIM_METRICS = ("newLeadsAssigned", "existingLeadsAssigned")


def _lead_brief(lead: dict, at_ms: int | None) -> dict[str, Any]:
    tz = settings.tz
    name = (lead.get("seller_name") or "").split(",")[0].strip()
    return {
        "name": name or (lead.get("address") or "Unnamed lead"),
        "address": (lead.get("address") or "") if name else "",
        "day": (datetime.fromtimestamp(at_ms / 1000, tz).date().isoformat() if at_ms else None),
        "atMs": at_ms,
        "stageNow": lead.get("substage") or lead.get("stage"),
        "url": lead_url(lead) if lead.get("id") else None,
        "oldLead": lead.get("origin") == "old",
    }


@app.get("/api/leaderboard/leads")
def api_leaderboard_leads(request: Request):
    """The leads behind one person's Leaderboard numbers for a range."""
    day_from, day_to = _range(request, default_days=7)
    email = (request.query_params.get("email") or "").strip().lower()
    if email not in TEAM_ROLES:
        return JSONResponse({"error": "Not on the board."}, status_code=404)
    role = role_of(email).get("role", "closer")
    cats = LB_CATEGORIES.get(role, LB_CATEGORIES["closer"])
    wanted = {key for key, _s, _l in cats}
    try:
        trail = collector.trace_activity(day_from, day_to)
    except Exception:  # noqa: BLE001
        log.exception("leaderboard trace failed")
        trail = []
    picked: dict[str, dict[str, int]] = {key: {} for key in wanted}
    for t in trail:
        if t["user"] != email:
            continue
        key = "leads" if t["metric"] in LEAD_CLAIM_METRICS else t["metric"]
        if key in picked and t["opp"] not in picked[key]:
            picked[key][t["opp"]] = t["at"]
    # Before the GHL cutover the numbers came from InvestorFuse's scorecard,
    # which names no leads. The lead records stand in for that stretch.
    cutover = date.fromisoformat(settings.ghl_activity_cutover)
    pre_end = min(day_to, cutover - timedelta(days=1))
    if day_from <= pre_end:
        tz = settings.tz
        lo = int(datetime.combine(day_from, dtime.min).replace(tzinfo=tz).timestamp() * 1000)
        hi = int(datetime.combine(pre_end, dtime.max).replace(tzinfo=tz).timestamp() * 1000)
        for lead in store.all_leads(include_old=True):
            if (lead.get("owner_id") or "").strip().lower() != email:
                continue
            for key, field in (("contractsAccepted", "contract_ms"),
                               ("verbalOffersMade", "offer_ms")):
                at = lead.get(field)
                if key in picked and at and lo <= int(at) <= hi:
                    picked[key].setdefault(lead["id"], int(at))
    ids = {opp for m in picked.values() for opp in m}
    leads = store.leads_by_id(list(ids)) if ids else {}
    out = []
    for key, short, label in cats:
        rows = [_lead_brief(leads.get(opp) or {"id": opp}, at)
                for opp, at in picked.get(key, {}).items()]
        rows.sort(key=lambda r: -(r["atMs"] or 0))
        out.append({"key": key, "short": short, "label": label,
                    "count": len(rows), "leads": rows})
    return {"email": email, "name": display_name(email), "role": role,
            "range": {"from": day_from.isoformat(), "to": day_to.isoformat()},
            "categories": out}


FEED_VERBS = {
    "closedDeals": ("closed", "closed the deal with"),
    "contractsAccepted": ("signed", "signed"),
    "contractsSent": ("sent", "sent a contract to"),
    "verbalOffersMade": ("offer", "made a verbal offer to"),
}


@app.get("/api/leaderboard/feed")
def api_leaderboard_feed(request: Request):
    """Recent wins: offers, contracts sent and signed, closes - last 14 days."""
    today = datetime.now(settings.tz).date()
    try:
        days = max(1, min(90, int(request.query_params.get("days") or 14)))
    except ValueError:
        days = 14
    first = today - timedelta(days=days - 1)
    try:
        trail = collector.trace_activity(first, today)
    except Exception:  # noqa: BLE001
        log.exception("leaderboard feed failed")
        return {"events": [], "error": "Could not build the feed."}
    seen: set[tuple[str, str]] = set()
    picked = []
    for t in trail:
        if t["metric"] not in FEED_VERBS or t["user"] not in TEAM_ROLES:
            continue
        key = (t["metric"], t["opp"])
        if key in seen:
            continue
        seen.add(key)
        picked.append(t)
    picked.sort(key=lambda t: -(t["at"] or 0))
    picked = picked[:30]
    leads = store.leads_by_id(list({t["opp"] for t in picked})) if picked else {}
    staff_rows = {r["email"]: r for r in store.staff_all()}
    events = []
    for t in picked:
        kind, verb = FEED_VERBS[t["metric"]]
        lead = _lead_brief(leads.get(t["opp"]) or {"id": t["opp"]}, t["at"])
        events.append({"kind": kind, "verb": verb, "who": display_name(t["user"]),
                       "photo": staff_mod.photo_url(t["user"], staff_rows),
                       "lead": lead["name"], "url": lead["url"], "atMs": t["at"]})
    return {"events": events, "since": first.isoformat()}


@app.get("/api/rep")
async def api_rep(request: Request):
    """Everything about one rep - or the whole team - for a date range."""
    today = datetime.now(settings.tz).date()
    day_to = _parse_day(request.query_params.get("to"), today)
    day_from = _parse_day(request.query_params.get("from"), day_to - timedelta(days=29))
    if day_from > day_to:
        day_from, day_to = day_to, day_from
    owner = (request.query_params.get("owner") or "").strip().lower()

    by_user = _rep_rows(day_from, day_to)

    # Every lead created in the range, split by who owns it. The selector cards
    # and the pipeline below them are both computed from this, so a card and
    # the funnel it opens can never disagree - they were the same numbers run
    # through the same classifier. Cards used to read the scorecard, which
    # counts activity on leads of any age and so never matched.
    all_leads = store.leads_between(day_from.isoformat(), day_to.isoformat())
    # Source filter: every lead-based number on the page (cards, funnel,
    # pipeline, goals, speed to lead) follows it. Calls, texts and talk time
    # come from the activity log, which has no source, so they don't.
    source = (request.query_params.get("source") or "").strip().lower()
    if source in ("google", "meta"):
        all_leads = [l for l in all_leads if _ads_source(l) == source]
    else:
        source = "all"
    leads_by_owner: dict[str, list[dict]] = {}
    for lead in all_leads:
        leads_by_owner.setdefault(
            (lead.get("owner_id") or "").strip().lower(), []).append(lead)

    # Two card shapes, because the two roles are measured on different things.
    #
    # A closer owns opportunities, so their card is the pipeline in miniature
    # and ties out to the funnel exactly. A lead manager never becomes the
    # opportunity owner - they work new leads and hand them on - so an
    # owner-based card for them is four zeros. Theirs counts what they actually
    # do instead, and is marked as activity so nobody reads it as pipeline.
    card_metrics = ["leads", "contacted", "offers", "contracts"]

    def card_stats(rows: list[dict]) -> dict[str, float]:
        f = funnel_mod.build(rows)
        return {"leads": f["total"], "contacted": f["contacted"],
                "offers": f["offers"], "contracts": f["contracts"]}

    people = [{
        "email": "", "name": "Whole team", "role": "team", "title": "Everyone",
        "stats": card_stats(all_leads),
        "statKeys": card_metrics,
    }]
    # Anyone with scorecard activity, anyone with a role, and anyone who owns a
    # lead - the last of those matters now that the cards are lead-based.
    known = set(TEAM_ROLES)     # the roster only - see normalize.TEAM_ROLES
    for email in sorted(known):
        info = role_of(email)
        is_va = info.get("role") == "va"
        people.append({
            "email": email,
            "name": display_name(email),
            "role": info.get("role", "other"),
            "title": info.get("title", ""),
            "booksFor": (display_name(info["books_for"])
                         if info.get("books_for") else None),
            "stats": ({k: by_user.get(email, {}).get(k, 0.0) for k in VA_METRICS}
                      if is_va else card_stats(leads_by_owner.get(email, []))),
            "statKeys": VA_METRICS if is_va else card_metrics,
        })
    people[1:] = sorted(people[1:],
                        key=lambda p: (ROLE_ORDER.get(p["role"], 2), p["name"]))

    metrics = by_user.get(owner, {}) if owner else {}
    if not owner:
        metrics = {}
        for m in by_user.values():
            for k, v in m.items():
                metrics[k] = metrics.get(k, 0.0) + v

    # Daily series for this rep (or the team).
    per_day: dict[str, dict[str, float]] = {}
    for day, user, metric, value in _scorecard_rows(day_from, day_to):
        if owner and user != owner:
            continue
        per_day.setdefault(day, {})
        per_day[day][metric] = per_day[day].get(metric, 0.0) + value
    daily = []
    cursor = day_from
    while cursor <= day_to:
        iso_day = cursor.isoformat()
        daily.append({"day": iso_day, "metrics": per_day.get(iso_day, {})})
        cursor += timedelta(days=1)

    # Their funnel: the leads they own, created in this range - the same split
    # the cards above were built from, so the two always agree.
    funnel_rows = leads_by_owner.get(owner, []) if owner else all_leads
    funnel_payload = funnel_mod.build(funnel_rows)
    # The New Leads pipeline as GHL shows it: real stage names, board order.
    funnel_payload["pipeline"] = funnel_mod.stage_breakdown(funnel_rows, _stage_order())
    # Alec's five-row funnel. History only for leads with an offer, to name
    # the offer stage each one last sat in (Hot / Cold / Contract Sent).
    offer_ids = [l["id"] for l in funnel_rows if funnel_mod.is_ghl(l) and l.get("offer_ms")]
    funnel_payload["flow"] = funnel_mod.ghl_flow(
        funnel_rows, store.stage_events_for(offer_ids) if offer_ids else {})
    # Contracts signed in the range on leads that came in BEFORE it. The
    # funnel only follows this range's leads, so these sit beside Stage 5 -
    # and the two together match the Leaderboard's contracts signed.
    funnel_payload["flow"]["oldContracts"] = _old_contracts(day_from, day_to, owner, source)

    info = role_of(owner) if owner else {"role": "team", "title": "Whole team"}

    # A lead manager's activity has no denominator of its own - they own no
    # opportunities, so "592 calls" floats free. The leads they were calling
    # are the ones their closer ends up owning, so that count is the scale the
    # activity should be read against. Same number as the closer's own card,
    # computed the same way, so the two can never disagree.
    books_for = info.get("books_for")
    books_for_leads = (
        card_stats(leads_by_owner.get(books_for, []))["leads"]
        if books_for else None
    )
    books_for_attempted = (
        _call_attempted(leads_by_owner.get(books_for, [])) if books_for else None
    )
    return {
        "range": {"from": day_from.isoformat(), "to": day_to.isoformat()},
        "source": source,
        "owner": owner or None,
        "name": display_name(owner) if owner else "Whole team",
        "role": info.get("role", "other"),
        "title": info.get("title", ""),
        "booksFor": (display_name(info["books_for"]) if info.get("books_for") else None),
        "booksForLeads": books_for_leads,
        "callAttempted": books_for_attempted,
        "people": people,
        # Hidden until somebody says otherwise: anyone with no role in
        # TEAM_ROLES. That is the owner, whoever is logged in to look at the
        # dashboard, and any stray login the API reports - real accounts with
        # a little activity on them, but not people whose numbers get managed.
        # Give someone a role and they appear without anyone touching this.
        "defaultHiddenPeople": [
            p["email"] for p in people if p["email"] and p["role"] == "other"
        ],
        "cardMetrics": card_metrics,
        "metrics": metrics,
        "daily": daily,
        "funnel": funnel_payload,
        # Credited to whoever made the first call or text, not the owner -
        # a lead manager's speed is theirs even though they own nothing.
        "speed": _rep_speed(all_leads, owner),
        "goals": _goal_gaps(funnel_payload),
        "metricLabels": METRIC_LABELS,
        "defaultHidden": DEFAULT_HIDDEN_METRICS,
        "durationMetrics": sorted(DURATION_METRICS),
        "roleMetrics": {"va": VA_METRICS, "closer": CLOSER_METRICS},
        "freshness": _freshness(),
    }


def _old_contracts(day_from: date, day_to: date, owner: str, source: str) -> dict[str, Any]:
    """Leads created before ``day_from`` that got signed inside the range."""
    tz = settings.tz
    start = int(datetime.combine(day_from, dtime.min).replace(tzinfo=tz).timestamp() * 1000)
    end = int(datetime.combine(day_to, dtime.max).replace(tzinfo=tz).timestamp() * 1000)
    out = []
    for lead in store.all_leads(include_old=True):
        signed = lead.get("contract_ms")
        if not signed or not (start <= int(signed) <= end):
            continue
        if (lead.get("created_day") or "") >= day_from.isoformat():
            continue
        if owner and (lead.get("owner_id") or "").strip().lower() != owner:
            continue
        if source in ("google", "meta") and _ads_source(lead) != source:
            continue
        out.append({
            "name": (lead.get("seller_name") or "").split(",")[0].strip() or "Unnamed lead",
            "signedDay": datetime.fromtimestamp(int(signed) / 1000, tz).date().isoformat(),
            "createdDay": lead.get("created_day"),
            "url": lead_url(lead),
        })
    out.sort(key=lambda r: r["signedDay"])
    return {"count": len(out), "leads": out}


def _stage_order() -> list[str]:
    try:
        order = json.loads(store.get_meta("ghl_stage_order") or "null")
    except ValueError:
        order = None
    return order if isinstance(order, list) and order else funnel_mod.DEFAULT_STAGE_ORDER


def _rep_speed(leads: list[dict], owner: str) -> dict | None:
    try:
        team = _speed(leads)
    except Exception as exc:  # noqa: BLE001 - never take the drill-down down
        log.exception("speed to lead failed")
        return {"error": str(exc)[:300]}
    if team is None or not owner:
        return team
    mine = next((r for r in team["byRep"] if r["email"] == owner), None)
    return {**(mine or {"n": 0, "median": None, "within5": None, "within60": None}),
            "hours": team["hours"], "rep": True}


# The five numbers an acquisitions rep is judged on, all counted off the lead
# table so they tie out to that rep's funnel in the drill-down.
REP_COMPARE_METRICS = [
    {"key": "leads", "label": "New leads", "hue": "leadsCreated"},
    {"key": "contacted", "label": "Qualified + Contacted", "hue": "contacts"},
    {"key": "offers", "label": "Verbal offers", "hue": "verbalOffersMade"},
    {"key": "contracts", "label": "Contracts signed", "hue": "contractsAccepted"},
    # Replaced "Qualified": under GHL that is the same step as contacted.
    {"key": "closed", "label": "Closed", "hue": "closedDeals"},
]


def _metric_series(day_from: date, day_to: date, metric: str
                   ) -> tuple[list[str], list[float]]:
    """``(days, values)`` for one metric, one value per day in the range."""
    days: list[str] = []
    cursor = day_from
    while cursor <= day_to:
        days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    totals = {d: 0.0 for d in days}

    if metric in LEAD_TREND_METRICS:
        # Counted off the lead table, by created date, so this ties out to the
        # funnel and the Overview tiles. The scorecard cannot answer it: its
        # assignment counts drop leads nobody was assigned to and double up
        # leads that changed hands.
        for day, _owner, count in store.lead_daily_counts(
                day_from.isoformat(), day_to.isoformat()):
            if day in totals:
                totals[day] += float(count)
    else:
        # Only people with a role, the same rule the Overview scorecard uses.
        # The API also reports the owner's own login and whoever is signed in
        # looking at the dashboard; counting their stray activity here would
        # put a total on the tile that the per-rep chart below it cannot add
        # up to, and the missing few would look like a bug in the chart.
        parts = CLAIMED_PARTS if metric == "claimedOpps" else (metric,)
        for day, user, m, value in _scorecard_rows(day_from, day_to):
            if m in parts and day in totals and role_of(user).get("role") != "other":
                totals[day] += value

    return days, [totals[d] for d in days]


# Who owns nothing. Leads arrive unassigned and about half of them stay that
# way, so a per-rep chart that showed only people would not add up to the total
# printed above it. This series makes the gap visible instead of unexplained.
UNASSIGNED = "(unassigned)"

# A rep's colour is fixed by their position in the team list, not by where they
# land in a given response - so filtering a range down to two people leaves the
# two keeping the colours they had. Only four colours are validated; see the
# --rep-N note in app.css before a fifth person is added.
def _rep_color_order() -> list[str]:
    """Fixed colour slot per person: alphabetical by login, from the live
    roster, so a Staff change re-slots people rather than leaving a stale list."""
    return sorted(TEAM_ROLES)


def _metric_series_by_user(day_from: date, day_to: date, metric: str
                           ) -> tuple[list[str], dict[str, list[float]]]:
    """``(days, {user: [value per day]})`` - the same series, split by person."""
    days: list[str] = []
    cursor = day_from
    while cursor <= day_to:
        days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    at = {day: i for i, day in enumerate(days)}
    out: dict[str, list[float]] = {}

    def row(user: str) -> list[float]:
        return out.setdefault(user, [0.0] * len(days))

    if metric in LEAD_TREND_METRICS:
        for day, owner, count in store.lead_daily_counts(
                day_from.isoformat(), day_to.isoformat()):
            if day in at:
                who = (owner or "").strip().lower() or UNASSIGNED
                row(who)[at[day]] += float(count)
    else:
        parts = CLAIMED_PARTS if metric == "claimedOpps" else (metric,)
        for day, user, name, value in _scorecard_rows(day_from, day_to):
            if name in parts and day in at:
                row(user)[at[day]] += value

    return days, out


def _best_window(values: list[float], size: int = 7) -> float:
    """The best any ``size`` consecutive days managed.

    A rolling window, not a Monday-to-Sunday one. The ranges here are "the last
    N days", so calendar weeks would slice the window into partial weeks and
    "best week" could come out below the total on a 7-day range. Rolling means
    the best week is the best week, whichever day it started on.
    """
    if not values:
        return 0.0
    if len(values) <= size:
        return float(sum(values))
    best = running = sum(values[:size])
    for i in range(size, len(values)):
        running += values[i] - values[i - size]
        best = max(best, running)
    return float(best)


def _rep_compare(day_from: date, day_to: date) -> dict[str, dict[str, float]]:
    """``{email: {leads, contacted, qualified, offers, contracts}}``.

    Same source and same classifier as the drill-down funnel, so a rep's row
    here and their pipeline there are the same numbers.
    """
    by_owner: dict[str, list[dict]] = {}
    for lead in store.leads_between(day_from.isoformat(), day_to.isoformat()):
        by_owner.setdefault((lead.get("owner_id") or "").strip().lower(), []).append(lead)

    out: dict[str, dict[str, float]] = {}
    for email, rows in by_owner.items():
        if not email:
            continue
        f = funnel_mod.build(rows)
        out[email] = {
            "leads": f["total"], "contacted": f["contacted"],
            "qualified": f["buckets"].get("qualified", 0),
            "offers": f["offers"], "contracts": f["contracts"],
            "closed": f["closed"],
        }
    return out


@app.get("/api/trends")
async def api_trends(request: Request):
    """One metric over time, against the same measure in the previous period.

    Every number on the Trends page is a comparison: the client picks both
    ranges (so "month to date" lands on the same span of last month rather
    than all of it) and this reports the pair.
    """
    today = datetime.now(settings.tz).date()
    day_to = _parse_day(request.query_params.get("to"), today)
    day_from = _parse_day(request.query_params.get("from"), day_to - timedelta(days=29))
    if day_from > day_to:
        day_from, day_to = day_to, day_from

    # The comparison window, chosen by the client alongside the preset. Falling
    # back to "the same number of days immediately before" keeps a hand-typed
    # range working.
    length = (day_to - day_from).days
    prev_to = _parse_day(request.query_params.get("prevTo"), day_from - timedelta(days=1))
    prev_from = _parse_day(request.query_params.get("prevFrom"),
                           prev_to - timedelta(days=length))
    if prev_from > prev_to:
        prev_from, prev_to = prev_to, prev_from

    metric = request.query_params.get("metric") or "leadsCreated"

    days, values = _metric_series(day_from, day_to, metric)
    _, prev_values = _metric_series(prev_from, prev_to, metric)

    # The chart draws one line per person rather than a team total, so it shows
    # the same thing the comparison table below it does - who is doing what -
    # only over time instead of as two numbers.
    _, by_user = _metric_series_by_user(day_from, day_to, metric)
    lines = []
    for email, points in by_user.items():
        if not any(points):
            continue        # somebody with a row but nothing in this range
        if email == UNASSIGNED:
            lines.append({"key": email, "name": "Unassigned", "role": "none",
                          "colorIndex": None, "points": points})
            continue
        if role_of(email).get("role", "other") == "other":
            continue        # untracked logins, same rule as everywhere else
        lines.append({
            "key": email, "name": display_name(email),
            "role": role_of(email).get("role", "other"),
            "colorIndex": (_rep_color_order().index(email)
                           if email in TEAM_ROLES else None),
            "points": points,
        })
    # Biggest first, so the legend order matches what the eye picks out; the
    # unassigned line always sits last because it is context, not a competitor.
    lines.sort(key=lambda s: (s["key"] == UNASSIGNED, -sum(s["points"])))

    def summary(vals: list[float]) -> dict[str, float]:
        return {
            "total": sum(vals),
            "bestDay": max(vals) if vals else 0.0,
            "bestWeek": _best_window(vals),
            "perDay": (sum(vals) / len(vals)) if vals else 0.0,
        }

    # Per-rep comparison. Acquisitions are measured on leads; lead managers own
    # none, so theirs stays on the scorecard metrics their job produces.
    cur_leads = _rep_compare(day_from, day_to)
    prev_leads = _rep_compare(prev_from, prev_to)
    cur_rows = _rep_rows(day_from, day_to)
    prev_rows = _rep_rows(prev_from, prev_to)

    reps = []
    for email in sorted(set(TEAM_ROLES) | set(cur_leads) | set(cur_rows)):
        info = role_of(email)
        role = info.get("role", "other")
        if role == "va":
            keys = VA_METRICS
            cur = {k: cur_rows.get(email, {}).get(k, 0.0) for k in keys}
            prev = {k: prev_rows.get(email, {}).get(k, 0.0) for k in keys}
            labels = [{"key": k, "label": METRIC_LABELS.get(k, k), "hue": k} for k in keys]
        elif role == "closer":
            cur = cur_leads.get(email, {k["key"]: 0 for k in REP_COMPARE_METRICS})
            prev = prev_leads.get(email, {k["key"]: 0 for k in REP_COMPARE_METRICS})
            labels = REP_COMPARE_METRICS
        else:
            continue        # stray logins and the owner aren't measured here
        reps.append({
            "email": email, "name": display_name(email), "role": role,
            "title": info.get("title", ""),
            "booksFor": (display_name(info["books_for"])
                         if info.get("books_for") else None),
            # The same colour their line wears on the chart below, so the two
            # cards read as one thing rather than two lists of the same names.
            "colorIndex": (_rep_color_order().index(email)
                           if email in TEAM_ROLES else None),
            "metrics": labels, "current": cur, "previous": prev,
        })
    reps.sort(key=lambda r: (ROLE_ORDER.get(r["role"], 2), r["name"]))

    return {
        "range": {"from": day_from.isoformat(), "to": day_to.isoformat()},
        "previousRange": {"from": prev_from.isoformat(), "to": prev_to.isoformat()},
        "metric": metric,
        "days": days,
        # The team total, still sent: the chart draws people, but the table
        # under it totals them and the daily total is the honest way to do
        # that when a lead can be owned by nobody.
        "values": values,
        "summary": summary(values),
        "previousSummary": summary(prev_values),
        "lines": lines,
        "reps": reps,
        # Lead-table series aren't scorecard metrics, so their labels aren't in
        # METRIC_LABELS - merged here rather than added there, which would put
        # them in the "Every metric" picker with nothing behind them.
        "metricLabels": {**METRIC_LABELS, **LEAD_TREND_METRICS},
        "options": TREND_METRICS,
        "freshness": _freshness(),
    }


# ----------------------------------------------------------------------
# Opportunities list
# ----------------------------------------------------------------------
# Each named view answers "show me the leads behind that number", so a tile on
# the Overview can link straight here without the two ever disagreeing: both
# read the same classifier.
OPP_VIEWS = {
    "all":         {"label": "All leads"},
    # Per-lead rules (funnel.is_contacted etc.), because InvestorFuse and GHL
    # record the same steps differently - see the note in funnel.py.
    "contacted":   {"label": "Qualified + Contacted", "pred": "contacted"},
    "not_reached": {"label": "Not Qualified + Contacted yet", "pred": "not_reached"},
    "dead_before": {"label": "Disqualified before qualifying", "pred": "dead_before"},
    "active":      {"label": "Qualified + Contacted, still in play", "pred": "active"},
    "dq_after":    {"label": "Disqualified after Qualified + Contacted", "pred": "dq_after"},
    "lost_after":  {"label": "Lost after qualifying", "pred": "lost_after"},
    "qualified_lost": {"label": "Opportunity lost (after qualifying)", "pred": "qualified_lost"},
    "lost_stage":  {"label": "Opportunity Lost", "pred": "lost_stage"},
    "contracts_sent": {"label": "Contracts sent", "pred": "contracts_sent"},
    "closed":      {"label": "Closed", "pred": "closed"},
    "qualified":   {"label": "Qualified + Contacted", "buckets": {"qualified"}},
    # Booked for a closer, then no-showed: still in qualification because
    # nobody has managed to get the seller back on the phone.
    "missed_appt": {"label": "Missed appointment", "buckets": {"pending"}},
    "offers":      {"label": "Verbal offers", "hasOffer": True},
    # The pile the old "Offers made" tile was really showing: parked in the
    # offer sub-stage with no offer recorded. Worth being able to open,
    # because it is the list of offers somebody has been meaning to make.
    "offer_stage": {"label": "Working toward an offer",
                    "substages": {"offer"}, "noOffer": True},
    "contracts":   {"label": "Contracts signed", "pred": "contract"},
    "uncontacted": {"label": "Not yet contacted", "pred": "not_reached"},
    "no_contact":  {"label": "No contact yet",  "buckets": {"no_contact"}},
    "call_attempted": {"label": "Call attempted", "buckets": {"call_attempted"}},
    "invalid":     {"label": "Invalid",       "buckets": {"invalid"}},
    "dead":        {"label": "Disqualified",  "buckets": {"disqualified"}},
    # Sometimes the seller sold elsewhere, sometimes the deal fell over. Kept
    # apart from Disqualified because it isn't a decision the team made.
    "lost":        {"label": "Opportunity lost", "buckets": {"lost"}},
    "unclassified_dead": {"label": "Dead, reason unrecognised",
                          "buckets": {"unclassified_dead"}},
    # Everything the CRM treats as dead, whatever the reason.
    "dead_all":    {"label": "Dead opportunities",
                    "buckets": {"disqualified", "lost", "invalid",
                                "unclassified_dead"}},
}


OPP_PREDICATES = {
    "contacted": lambda lead, b: funnel_mod.is_contacted(lead, b),
    "contract": lambda lead, b: funnel_mod.is_contract(lead),
    "closed": lambda lead, b: funnel_mod.is_closed(lead),
    "not_reached": lambda lead, b: funnel_mod.lead_step(lead, b) == "not_reached",
    "dead_before": lambda lead, b: funnel_mod.lead_step(lead, b) == "dead_before",
    "active": lambda lead, b: funnel_mod.contacted_outcome(lead, b) == "active",
    "dq_after": lambda lead, b: funnel_mod.contacted_outcome(lead, b) == "dq_after",
    "lost_after": lambda lead, b: funnel_mod.contacted_outcome(lead, b) == "lost_after",
    "qualified_lost": lambda lead, b: funnel_mod.contacted_outcome(lead, b) in ("dq_after", "lost_after"),
    "lost_stage": lambda lead, b: funnel_mod.is_lost_stage(lead),
    # Set per request from stage history (see api_opportunities).
    "contracts_sent": lambda lead, b: bool(lead.get("_contract_sent")),
}


def _contracts_sent_count(rows: list[dict]) -> int:
    """Leads that reached Contract Sent or got signed - the Overview tile,
    counted exactly like the Rep drill-down box and its lead list."""
    marked = [dict(l) for l in rows]
    _mark_contract_sent(marked)
    return sum(1 for l in marked if l["_contract_sent"])


def _mark_contract_sent(rows: list[dict]) -> None:
    """Flag leads that ever reached Contract Sent (or got signed) - the same
    rule as the Rep drill-down's Contracts sent box."""
    ids = [l["id"] for l in rows if funnel_mod.is_ghl(l) and l.get("offer_ms")]
    history = store.stage_events_for(ids) if ids else {}
    for lead in rows:
        events = history.get(lead.get("id")) or []
        lead["_contract_sent"] = bool(
            funnel_mod.is_ghl(lead) and lead.get("offer_ms") and (
                lead.get("contract_ms")
                or funnel_mod.is_contract_sent_stage(lead.get("substage"))
                or any(funnel_mod.is_contract_sent_stage(e[1]) for e in events)))


@app.get("/opportunities", response_class=HTMLResponse)
async def opportunities_page():
    return _page("opportunities.html")


# ----------------------------------------------------------------------
# Leads Meta billed for that InvestorFuse never received
#
# Meta's Leads Center knows every form submission; InvestorFuse only shows the
# ones its intake accepted. A lead swallowed between the two is invisible on
# both sides - Meta reports it delivered, the CRM simply has no record - so
# the only way to find one is to compare the two lists directly.
#
# Admin only, and the one place on this dashboard that stores a seller's phone
# and email. That is deliberate: a lead that never reached the CRM exists
# nowhere else, so if it is not here somebody has to find it in a screenshot.
# Matched leads are compared in memory and discarded.
# ----------------------------------------------------------------------
def _missing_key(row: dict) -> str:
    """Stable id for one submission, so re-uploading an export is harmless."""
    seed = (row.get("email") or row.get("phone")
            or (row.get("name") or "").lower() or "?")
    return hashlib.sha256(
        f"{seed}|{row.get('created') or row.get('createdRaw') or ''}"
        .encode("utf-8")).hexdigest()[:20]


def _submission_ms(row: dict) -> int | None:
    """When a returning seller's latest fill happened, in epoch ms.

    Prefers the exact timestamp the export carries and falls back to the end
    of that day, so a row with only a date still sorts after everything that
    happened before it rather than landing at midnight.
    """
    stamp = row.get("latest")
    if stamp:
        try:
            when = datetime.fromisoformat(stamp)
            if when.tzinfo is None:
                when = when.replace(tzinfo=settings.tz)
            return int(when.timestamp() * 1000)
        except (ValueError, TypeError):
            pass
    day = row.get("latestDay")
    if not day:
        return None
    try:
        return int(datetime.combine(date.fromisoformat(day), dtime.max)
                   .replace(tzinfo=settings.tz).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


@app.post("/api/leads/reconcile")
async def api_leads_reconcile(request: Request,
                              file: UploadFile = File(...),
                              save: str = Form("1")):
    """Diff a Leads Center export against InvestorFuse. Admin only.

    The CRM side is read live rather than from the lead table, because the
    match needs email and phone and the lead table deliberately stores
    neither. It is built into an index, compared, and dropped.
    """
    blocked = _admin_only(request)
    if blocked:
        return blocked

    raw = await file.read()
    if len(raw) > 8 * 1024 * 1024:
        return JSONResponse({"error": "that file is larger than 8MB — export a "
                                      "narrower date range"}, status_code=400)
    try:
        meta_rows = leadcsv.parse_export(raw)
    except leadcsv.LeadCsvError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    days = sorted(r["createdDay"] for r in meta_rows if r.get("createdDay"))
    if not days:
        return JSONResponse({"error": "no readable submission dates in that "
                                      "file"}, status_code=400)
    day_from, day_to = days[0], days[-1]

    # Only as far back as the CRM sweep actually reaches. Comparing a year of
    # export against a rolling window of CRM rows would report everything
    # before the window as missing - a false alarm big enough to bury the
    # real one.
    oldest, _newest = store.lead_span()
    if oldest and oldest > day_from:
        day_from = oldest

    if settings.ghl_configured:
        # Against GHL: the stored lead rows, matched on the hashed email and
        # phone each one carries - no live read, nothing new stored. Only
        # from the GHL cutover on; a Meta lead from before it went to
        # InvestorFuse and would read as missing here.
        if settings.ghl_cutover and settings.ghl_cutover > day_from:
            day_from = settings.ghl_cutover
        # Old leads included: a Meta lead whose seller is in Old Leads is
        # not missing.
        crm = [l for l in store.all_leads(include_old=True) if l.get("crm") == "ghl"]
        result = leadcsv.reconcile(meta_rows, crm, day_from, day_to, hashed=True)
    else:
        try:
            start_ms = int(datetime.combine(date.fromisoformat(day_from), dtime.min)
                           .replace(tzinfo=settings.tz).timestamp() * 1000)
            crm = list(client.iter_opportunities(date_from_ms=start_ms))
        except Exception as exc:  # noqa: BLE001 - report, never 500
            return JSONResponse(
                {"error": f"could not read InvestorFuse to compare against: {exc}"},
                status_code=502)
        result = leadcsv.reconcile(meta_rows, crm, day_from, day_to)

    # Refuse to call anything missing when we could not read the CRM side.
    # An unreadable contact field makes every lead in the export look absent,
    # which is a false alarm big enough to send somebody re-importing leads
    # that were there all along.
    problem = leadcsv.coverage_problem(result)
    if problem:
        log.warning("lead reconcile refused: %s", problem)
        return JSONResponse({"error": problem,
                             "coverage": result["coverage"],
                             "crmLeadsRead": len(crm)}, status_code=409)

    # Old lead, new form fill. Written onto the opportunities themselves so
    # the returning-sellers band on the Opportunities page - which everyone
    # can see, not just admins - has something real to show.
    returned = store.mark_resurfaced([
        {"crmId": r["crmId"], "submissions": r["submissions"],
         "resurfacedMs": _submission_ms(r)}
        for r in result["returning"]
        if r.get("inCrm") and r.get("crmId") and not r.get("sameDay")
    ])

    saved = 0
    if save not in ("0", "false", "no"):
        saved = store.record_missing([{
            "key": _missing_key(r),
            "seller_name": r.get("name"),
            "email": r.get("email"),
            "phone": r.get("rawPhone") or r.get("phone"),
            "form": r.get("form"),
            "channel": r.get("channel"),
            "submitted_at": r.get("created"),
            "submitted_day": r.get("createdDay"),
        } for r in result["missing"]])

    log.info("lead reconcile: %d checked, %d missing, %d newly saved, "
             "%d returning sellers marked",
             result["checked"], len(result["missing"]), saved, returned)
    return {**result,
            "crmLeadsRead": len(crm),
            "checkedFrom": day_from,
            "crmName": "GoHighLevel" if settings.ghl_configured else "InvestorFuse",
            "savedToWorkList": saved,
            "returningMarked": returned,
            "fileRows": len(meta_rows)}


@app.get("/api/leads/missing")
async def api_leads_missing(request: Request):
    """The work list. Admin only - it holds phone numbers and emails."""
    blocked = _admin_only(request)
    if blocked:
        return blocked
    status = (request.query_params.get("status") or "open").strip().lower()
    rows = store.missing_leads(None if status == "all" else status)
    return {"leads": rows, "status": status,
            "open": len(store.missing_leads("open"))}


@app.post("/api/leads/missing/{key}")
async def api_leads_missing_update(key: str, request: Request,
                                   action: str = Form("done"),
                                   note: str = Form("")):
    """Tick one off, or delete it outright. Admin only."""
    blocked = _admin_only(request)
    if blocked:
        return blocked
    if action == "forget":
        return {"ok": store.forget_missing(key), "action": "forget"}
    return {"ok": store.resolve_missing(key, note.strip() or None),
            "action": "done"}


@app.get("/api/opportunities")
async def api_opportunities(request: Request):
    today = datetime.now(settings.tz).date()

    # No date bound at all. This is what the page opens on: any dated default
    # can come up empty (month-to-date on the 1st showed a blank page), and an
    # empty list is indistinguishable from a broken one.
    unbounded = request.query_params.get("all") in ("1", "true", "yes")

    raw_view = request.query_params.get("view") or "all"
    view = raw_view.lower()
    spec = OPP_VIEWS.get(view, OPP_VIEWS["all"])
    # "stage:Hot Offer" - one GHL stage exactly as named on the board, open
    # cards only (Lost ones are their own view, as on GHL's board).
    if view.startswith("stage:"):
        stage_name = raw_view.split(":", 1)[1]
        spec = {"label": stage_name, "pred": "stage", "stageName": stage_name}
    elif view == "marked_lost":
        spec = {"label": "Marked lost", "pred": "marked_lost"}
    owner = (request.query_params.get("owner") or "").strip().lower()
    search = (request.query_params.get("q") or "").strip().lower()

    if unbounded:
        rows = store.all_leads()
        # Report the span actually held rather than implying we have every lead
        # the business has ever taken - the lead table is a rolling window.
        oldest, newest = store.lead_span()
        day_range = {"from": oldest, "to": newest, "all": True}
    else:
        day_to = _parse_day(request.query_params.get("to"), today)
        day_from = _parse_day(request.query_params.get("from"), day_to - timedelta(days=29))
        if day_from > day_to:
            day_from, day_to = day_to, day_from
        rows = store.leads_between(day_from.isoformat(), day_to.isoformat())
        day_range = {"from": day_from.isoformat(), "to": day_to.isoformat(), "all": False}
    src = (request.query_params.get("source") or "").strip().lower()
    if src in ("google", "meta"):
        rows = [r for r in rows if _ads_source(r) == src]

    if spec.get("pred") == "contracts_sent":
        _mark_contract_sent(rows)
    out = []
    for lead in rows:
        bucket = funnel_mod.classify(lead)
        if "buckets" in spec and bucket not in spec["buckets"]:
            continue
        if spec.get("pred") == "stage":
            if not (funnel_mod.is_ghl(lead)
                    and (funnel_mod.is_lost_stage(lead) or not funnel_mod.is_marked_lost(lead))
                    and (lead.get("substage") or "") == spec["stageName"]):
                continue
        elif spec.get("pred") == "marked_lost":
            if not funnel_mod.is_marked_lost(lead):
                continue
        elif "pred" in spec and not OPP_PREDICATES[spec["pred"]](lead, bucket):
            continue
        if "substages" in spec and (lead.get("substage") or "").lower() not in spec["substages"]:
            continue
        if spec.get("hasOffer") and not lead.get("offer_ms"):
            continue
        if spec.get("noOffer") and lead.get("offer_ms"):
            continue
        if "stages" in spec and (lead.get("stage") or "").lower() not in spec["stages"]:
            continue
        if owner and (lead.get("owner_id") or "").strip().lower() != owner:
            continue
        if search:
            hay = " ".join(str(lead.get(k) or "") for k in
                           ("address", "seller_name", "campaign")).lower()
            if search not in hay:
                continue
        item = dict(lead)
        item["bucket"] = bucket
        item["bucketLabel"] = funnel_mod.BUCKET_LABELS.get(bucket, bucket)
        # "Call attempted #4", not just "Call attempted". The rung is the whole
        # story on an unreached lead - a first attempt and a fifth are very
        # different rows to be looking at - and it is sitting right there in
        # the action. Only on this bucket: once a seller has been reached the
        # ladder stops and the action becomes a follow-up.
        if bucket == "call_attempted":
            rung = funnel_mod.call_attempt_number(lead.get("action_title"))
            if rung:
                item["bucketLabel"] += f" #{rung}"
        item["ownerName"] = display_name(lead["owner_id"]) if lead.get("owner_id") else None
        item["assigneeName"] = (display_name(lead["action_assignee"])
                                if lead.get("action_assignee") else None)
        item["url"] = lead_url(lead)
        out.append(item)

    # Freshest first: whoever was touched most recently is who you are thinking
    # about, and a never-touched lead sorts by when it arrived.
    out.sort(key=lambda l: (l.get("touched_ms") or l.get("created_ms") or 0), reverse=True)

    owners = sorted({(l["owner_id"], display_name(l["owner_id"]))
                     for l in rows if l.get("owner_id")}, key=lambda x: x[1])

    # Returning sellers: an opportunity created BEFORE this range that got a
    # new form fill inside it. InvestorFuse logs the re-submission against the
    # existing record instead of creating a second one, so these never appear
    # in a created-date list - which is precisely why nobody was checking them.
    #
    # Kept out of `total` and out of `items` deliberately. The lead count on
    # this page means opportunities created in the range, the funnel buckets
    # add up to it, and folding returners in would make the word "lead" mean
    # two things inside one number. They ride alongside instead - which is
    # also the honest bridge to Meta, who charges for the form fill and so
    # counts new + returning.
    returning = []
    if not unbounded:
        tz = settings.tz
        start_ms = int(datetime.combine(day_from, dtime.min)
                       .replace(tzinfo=tz).timestamp() * 1000)
        end_ms = int(datetime.combine(day_to, dtime.max)
                     .replace(tzinfo=tz).timestamp() * 1000)
        for lead in store.resurfaced_leads(start_ms, end_ms,
                                           created_before=day_from.isoformat()):
            bucket = funnel_mod.classify(lead)
            back, created = lead.get("resurfaced_ms"), lead.get("created_ms")
            returning.append({
                "id": lead["id"],
                "address": lead.get("address"),
                "sellerName": lead.get("seller_name"),
                "createdDay": lead.get("created_day"),
                "resurfacedMs": back,
                # How long they were gone - the most useful number here. Back
                # after three weeks is a warm lead; back after a year is a
                # different house and a different situation.
                "daysBetween": (round((back - created) / 86_400_000)
                                if back and created else None),
                "timesBack": lead.get("resurfaced_count") or 1,
                "bucket": bucket,
                "bucketLabel": funnel_mod.BUCKET_LABELS.get(bucket, bucket),
                # Why it was killed, if it was. This is what makes a returning
                # seller worth opening rather than skimming past.
                "deadReason": (lead.get("dead_reason") or "").strip() or None,
                "stage": lead.get("stage"),
                "campaign": lead.get("campaign"),
                "ownerName": (display_name(lead["owner_id"])
                              if lead.get("owner_id") else None),
                "hadOffer": bool(lead.get("offer_ms")),
                "url": lead_url(lead),
            })

    total = len(out)
    # Clamped, not trusted: ?limit=abc used to raise straight out of the handler
    # as a 500, and ?limit=-5 sliced from the end while reporting a nonsense count.
    try:
        limit = int(request.query_params.get("limit") or 200)
    except ValueError:
        limit = 200
    limit = max(1, min(limit, 500))
    return {
        "range": day_range,
        "view": view,
        "viewLabel": spec["label"],
        # Resolved here rather than looked up in the owners list, which only
        # holds reps who own something in this range - a rep with nothing to
        # show still needs their name on the page.
        "ownerName": display_name(owner) if owner else None,
        "views": {k: v["label"] for k, v in OPP_VIEWS.items()},
        "total": total,
        "shown": min(total, limit),
        "items": out[:limit],
        # Deliberately separate from `items` and `total`. See above.
        "returning": returning,
        "owners": [{"email": e, "name": n} for e, n in owners],
        "freshness": _freshness(),
    }
