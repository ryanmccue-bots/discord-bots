"""The background job that keeps the database current.

Runs in a daemon thread rather than an asyncio task because the HTTP client is
synchronous, and a thread keeps a slow InvestorFuse response from blocking the
web server's event loop.

Two jobs:

* **Poll** (every ``POLL_INTERVAL_MINUTES``) - refetch today and yesterday.
  Yesterday is included because activity logged late in the evening, or a call
  whose recording finishes after midnight, can still change yesterday's numbers.
* **Backfill** (once at startup, in the background) - walk back
  ``BACKFILL_DAYS`` and collect any day not already recorded. One API call per
  day, spaced out, so history exists from the first boot.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, time as time_of_day, timedelta

from config import Settings
from funnel import apply_history, build, ghl_stage_family, project, project_ghl
import activity
from ghlclient import GHLClient
from ifclient import InvestorFuseClient
from normalize import TEAM_NAMES, scorecard_rows
from store import Store

log = logging.getLogger(__name__)

# There used to be a third job here: five whole-CRM snapshots - pipeline,
# status, groups, campaigns, dead - fetched every poll. Every one of them lost
# its last reader when the "Pipeline right now" card went, because the campaign
# and funnel views are built from the lead table instead, which can be filtered
# by date and drilled into. Five calls per poll against an unofficial API for
# data nothing displays isn't a trade worth making, so the job is gone.
#
# The client methods remain in ifclient.py, and verify.py still calls them as a
# connectivity check, so reviving one means adding storage - not rediscovering
# the endpoints.


def _import_tagged(item: dict) -> bool:
    """Does the opportunity's contact carry the InvestorFuse import tag?"""
    contact = item.get("contact") or {}
    tags = contact.get("tags") or item.get("tags") or []
    if isinstance(tags, str):
        tags = tags.split(",")
    return any(str(t).strip().lower() == "investorfuse import" for t in tags)


class Collector:
    def __init__(self, client: InvestorFuseClient, store: Store, settings: Settings,
                 ghl: GHLClient | None = None, uploader=None):
        self.client = client
        self.ghl = ghl
        self.uploader = uploader
        self.store = store
        self.settings = settings
        self._stop = threading.Event()
        self._refreshing = threading.Event()
        # GHL reference data (stage names, user emails), shared by the sync
        # and the webhook so a burst of stage changes isn't two extra API
        # calls each. Refreshed every sync and whenever older than 10 minutes.
        self._ref: tuple[dict, dict, dict, float] | None = None
        self._ref_lock = threading.Lock()
        # Call-in auto-assignment (callassign.py). The sync hands it the raw
        # opportunities it just read, since the stored rows drop "source".
        self.call_assigner = None
        if ghl is not None:
            from callassign import CallAssigner
            self.call_assigner = CallAssigner(store, ghl, settings)
        self._recent_items: list[dict] = []

    # ------------------------------------------------------------------
    def today(self) -> date:
        return datetime.now(self.settings.tz).date()

    def collect_day(self, day: date) -> int:
        # The scorecard is InvestorFuse's. With GHL live and InvestorFuse
        # switched off there is nothing to call - activity metrics come from
        # GHL conversations instead (step 3 of the migration).
        if not self.settings.if_configured:
            return 0
        iso = day.isoformat()
        # From the activity cutover on, GHL computes this day. Collecting
        # InvestorFuse too would overwrite one source with the other.
        if self.settings.ghl_owns_day(iso):
            return 0
        payload = self.client.employee_scorecard(iso, iso)
        rows = scorecard_rows(payload)
        written = self.store.upsert_day(iso, rows)
        log.info("collected %s (%d rows)", iso, written)
        return written

    # ------------------------------------------------------------------
    # Leads (the funnel)
    # ------------------------------------------------------------------
    def sync_leads(self, days: int | None = None) -> int:
        """Refresh lead rows for a rolling window of created dates.

        A window rather than everything: leads keep changing stage for weeks
        after they arrive, but a lead created six months ago is settled. The
        window is re-pulled in full each time so stage changes are picked up.
        """
        days = days or self.settings.lead_window_days
        today = self.today()
        start = today - timedelta(days=days)
        date_from = int(
            datetime.combine(start, time_of_day.min)
            .replace(tzinfo=self.settings.tz).timestamp() * 1000
        )

        if self.ghl is not None:
            return self._sync_ghl(date_from, days)

        batch: list[dict] = []
        written = 0
        for item in self.client.iter_opportunities(date_from_ms=date_from):
            row = project(item, self.settings.tz)
            if row:
                batch.append(row)
            if len(batch) >= 200:
                written += self.store.upsert_leads(batch)
                batch = []
        written += self.store.upsert_leads(batch)
        log.info("lead sync complete: %d leads (window %d days)", written, days)
        return written

    def _sync_ghl(self, since_ms: int, days: int) -> int:
        """GHL opportunities in the window, translated into lead rows.

        Stage names and users are re-read every sync - two cheap calls - so
        a renamed stage or a new hire needs no deploy. Contacts are swept for
        the same window to pick up addresses, which the opportunity search
        does not return.
        """
        tz = self.settings.tz
        stage_names, users, _numbers = self._reference(force=True)
        self._record_stage_order()
        contacts = {str(c["id"]): c
                    for c in self.ghl.iter_contacts(since_ms=since_ms) if c.get("id")}

        batch: list[dict] = []
        written = 0
        seen: set[str] = set()
        items = list(self.ghl.iter_opportunities(since_ms=since_ms))
        old_ids = self._old_pipelines()
        self._recent_items = [i for i in items if self.call_assigner
                              and self.call_assigner.is_call_in(i)]
        for item in self._lead_pipeline_only(items):
            row = project_ghl(item, tz, stage_names, users, contacts)
            if row and item.get("pipelineId") in old_ids:
                row["_old_pipeline"] = True
                row["_import_tagged"] = _import_tagged(item)
            if row:
                batch.append(row)
                seen.add(row["id"])
            if len(batch) >= 200:
                written += self._write_ghl(batch, "poll")
                batch = []
        written += self._write_ghl(batch, "poll")

        # Deleted in GHL -> gone from here. Anything stored from this window
        # that GHL no longer returns has been deleted (a test lead, a
        # duplicate). Only after a COMPLETE walk - an exception above skips
        # this - and never on an empty one: GHL returning nothing for a busy
        # window is far likelier to be a hiccup than everything being deleted.
        removed = self.store.prune_ghl_leads(since_ms, seen) if seen else 0
        if removed:
            log.info("removed %d opportunities deleted in GHL", removed)
        log.info("GHL lead sync complete: %d opportunities (window %d days)",
                 written, days)
        return written

    # ------------------------------------------------------------------
    # GHL stage history
    # ------------------------------------------------------------------
    def _reference(self, force: bool = False) -> tuple[dict, dict, dict]:
        """``(stage names, {user_id: email}, {phone: email})``.

        Users are merged by name (see activity.canonical_users), so a person
        with two GHL accounts is one person everywhere on the dashboard -
        owner of a lead, sender of a call - not two.
        """
        with self._ref_lock:
            if force or self._ref is None or time.time() - self._ref[3] > 600:
                ids, numbers = activity.canonical_users(
                    self.ghl.users(), TEAM_NAMES, self.settings.ghl_location_id)
                extra_ids, extra_numbers = self._resolve_unknown_users(set(ids))
                ids = {**extra_ids, **ids}
                numbers = {**extra_numbers, **numbers}
                self._ref = (self.ghl.stage_names(), ids, numbers, time.time())
            return self._ref[0], self._ref[1], self._ref[2]

    def _resolve_unknown_users(self, known: set[str]) -> tuple[dict, dict]:
        """Name the user ids on calls and texts that users() didn't return.

        Agency-level logins aren't in a sub-account's user list, but someone
        signed in with one still works under its id - Alec's agency login,
        and whatever account Trevor's calls came from. Each unknown id is
        looked up once and the answer cached; a failed lookup is retried
        daily, in case the token gains the permission later.
        """
        import json as _json
        try:
            cache = _json.loads(self.store.get_meta("ghl_user_cache") or "{}")
        except ValueError:
            cache = {}
        now = time.time()
        changed = False
        for uid in sorted(self.store.message_user_ids() - known):
            entry = cache.get(uid)
            if entry and (entry.get("name") or now - entry.get("failed", 0) < 86400):
                continue
            try:
                user = self.ghl.get_user(uid)
            except Exception as exc:  # noqa: BLE001 - one bad id mustn't stop a sync
                log.warning("could not look up GHL user %s: %s", uid, exc)
                user = None
            cache[uid] = ({"name": user.get("name") or " ".join(filter(None, [
                               user.get("firstName"), user.get("lastName")])),
                           "email": user.get("email") or "",
                           "lcPhone": user.get("lcPhone") or {}}
                          if user else {"failed": now})
            changed = True
        if changed:
            self.store.set_meta("ghl_user_cache", _json.dumps(cache)[:20000])
        resolved = [{"id": uid, **e} for uid, e in cache.items() if e.get("name")]
        return activity.canonical_users(resolved, TEAM_NAMES, self.settings.ghl_location_id)

    def _write_ghl(self, rows: list[dict], source: str) -> int:
        """Record any stage changes, correct milestones from history, store.

        A stage change is noticed by comparison: the stage GHL reports now
        against the last one recorded. It is timed with GHL's own
        lastStageChangeAt, never the time we happened to look, so a move made
        at 9:02 and noticed by the 9:15 sync is recorded at 9:02 - and the
        webhook, which reads the same field, records the identical row.
        """
        if not rows:
            return 0
        ids = [r["id"] for r in rows]
        history = self.store.stage_events_for(ids)
        known = self.store.lead_stages(ids)
        owners = self.store.lead_owners(ids)
        origins = self.store.lead_origins(ids)
        now_ms = int(time.time() * 1000)
        touches = {}
        if self.settings.activity_enabled:
            _names, uids, numbers = self._reference()
            touches = activity.touches_for(
                self.store.messages_for_contacts(r.get("contact_id") for r in rows),
                uids, numbers)
        for row in rows:
            # Leads claimed: an owner that differs from the one stored. A lead
            # seen for the first time already owned was claimed when it was
            # created (call-ins are assigned at intake); otherwise it's timed
            # when noticed - GHL keeps no record of when an owner changed.
            owner = row.get("owner_id")
            # Old = the InvestorFuse import: first seen in the Old Leads
            # pipeline, or carrying the import tag. A lead that started in New
            # Leads and was later moved to Old Leads stays a new lead - its
            # history already counted and must not change after the fact.
            stored = row["id"] in known
            is_old = origins.get(row["id"]) == "old" or (
                row.get("_old_pipeline") and (not stored or row.get("_import_tagged")))
            if is_old:
                row["origin"] = "old"
                if stored and origins.get(row["id"]) != "old":
                    # Stored before it was known to be old (an earlier build,
                    # or the webhook beating the poll): turn its import-time
                    # stage into a baseline and drop the import-time "claim".
                    self.store.make_old_baseline(row["id"], row.get("created_ms"))
                    history[row["id"]] = self.store.stage_events_for([row["id"]]).get(row["id"], [])
                    owners[row["id"]] = owner
            if owner and row["id"] not in owners and is_old:
                # Imported already owned: that owner came with the import,
                # nobody claimed anything today. Later changes still count.
                pass
            elif owner and row["id"] not in owners:
                self.store.record_assignment(row["id"], owner,
                                             row.get("created_ms") or now_ms)
            elif owner and owners.get(row["id"]) != owner:
                self.store.record_assignment(row["id"], owner, now_ms)
            # Touches: the lead's own calls and texts, so a New Lead reads
            # "call attempted #3" instead of "no contact yet".
            tally = touches.get(row.get("contact_id") or "")
            if tally:
                row["touches"] = float(tally["touches"])
                row["touched_ms"] = tally["last_ms"] or None
                if tally["calls"] and row.get("family") == "new":
                    row["action_title"] = f"Call attempt {tally['calls']}"
            stage = row.get("substage")
            events = history.get(row["id"], [])
            last = events[-1][1] if events else None
            at = row.get("stage_changed_ms") or row.get("created_ms")
            if stage and at and last != stage:
                previous = last or known.get(row["id"])
                # First sighting of a lead already sitting where it was last
                # time: that's history being seeded, not a move.
                kind = "seed" if not events and previous in (None, stage) else source
                # An imported old lead's starting stage is a baseline: it
                # sets how far the lead had got and credits nobody.
                if kind == "seed" and is_old:
                    kind = "baseline"
                if self.store.record_stage_event(
                        row["id"], stage, at, None if previous == stage else previous,
                        row.get("family"), row.get("owner_id"), kind):
                    events = sorted(events + [(int(at), stage, row.get("family"))])
            apply_history(row, events)
        return self.store.upsert_leads(rows)

    def _record_stage_order(self) -> None:
        """The New Leads pipeline's stages in board order, for the drill-down.
        Read each sync, so a renamed or reordered stage shows up by itself."""
        import json as _json
        try:
            pid = self._wanted_pipeline()
            for pipe in self.ghl.pipelines():
                if pid and str(pipe.get("id")) != pid:
                    continue
                stages = sorted(pipe.get("stages") or [],
                                key=lambda s: (s.get("position") is None, s.get("position") or 0))
                names = [str(s.get("name") or "") for s in stages if s.get("name")]
                if names:
                    self.store.set_meta("ghl_stage_order", _json.dumps(names))
                return
        except Exception as exc:  # noqa: BLE001 - the default order still works
            log.warning("could not read GHL stage order: %s", exc)

    def _old_pipelines(self) -> set[str]:
        """Ids of the Old Leads pipeline(s): GHL_OLD_PIPELINE_ID, or any
        pipeline whose name says "old lead". Cached for ten minutes; empty if
        GHL can't be asked (then old leads are simply left out, as before)."""
        configured = (getattr(self.settings, "ghl_old_pipeline_id", "") or "").strip()
        if configured:
            return {configured}
        cached = getattr(self, "_old_cache", None)
        if cached and time.time() - cached[1] < 600:
            return cached[0]
        try:
            names = self.ghl.pipeline_names()
            found = {pid for pid, name in names.items() if "old lead" in name.lower()}
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read GHL pipelines for Old Leads: %s", exc)
            found = set()
        self._old_cache = (found, time.time())
        return found

    def _wanted_pipeline(self) -> str | None:
        pid = (getattr(self.settings, "ghl_pipeline_id", "") or "").strip()
        return None if pid.lower() in ("", "all", "*") else pid

    def _lead_pipeline_only(self, items: list[dict]) -> list[dict]:
        """Only the seller-lead pipeline. A buyers or nurture pipeline added
        later would otherwise turn every card in it into a "new lead".

        If NOTHING matches while other pipelines do, the configured id is
        wrong (the pipeline was recreated, say). Filtering then would empty
        the dashboard and prune every lead, so it is skipped and flagged on
        the verify page instead.
        """
        pid = self._wanted_pipeline()
        if not pid:
            return items
        allowed = {pid} | self._old_pipelines()
        keep = [i for i in items if not i.get("pipelineId") or i.get("pipelineId") in allowed]
        others = sorted({str(i.get("pipelineId")) for i in items
                         if i.get("pipelineId") and i.get("pipelineId") not in allowed})
        matched_new = [i for i in items if not i.get("pipelineId") or i.get("pipelineId") == pid]
        if items and not matched_new:
            log.warning("GHL_PIPELINE_ID %s matched nothing (saw %s) - not filtering", pid, others)
            self.store.set_meta("ghl_pipeline_warning",
                                f"GHL_PIPELINE_ID {pid} matched no opportunities; "
                                f"saw pipelines {', '.join(others)}. Not filtering.")
            return items
        self.store.set_meta("ghl_pipeline_warning", "")
        self.store.set_meta("ghl_pipeline_skipped", str(len(items) - len(keep)))
        return keep

    def ingest_opportunity(self, opportunity_id: str, source: str = "webhook"
                           ) -> dict | None:
        """Fetch one opportunity from GHL and record where it is now.

        The webhook path. GHL's webhook says THAT a card moved but not from
        where, when, or by whom - so it's treated as a doorbell, and the
        opportunity is read back from the API for the authoritative stage
        and change time.
        """
        if self.ghl is None or not opportunity_id:
            return None
        item = self.ghl.get_opportunity(opportunity_id)
        if not item:
            return None
        pid = self._wanted_pipeline()
        old = item.get("pipelineId") in self._old_pipelines()
        if pid and item.get("pipelineId") and item["pipelineId"] != pid and not old:
            return None     # not a seller lead; the next poll prunes it if stored
        stage_names, users, _numbers = self._reference()
        if str(item.get("pipelineStageId") or "") not in stage_names:
            # A stage created since the last refresh.
            stage_names, users, _numbers = self._reference(force=True)
        row = project_ghl(item, self.settings.tz, stage_names, users, None)
        if not row:
            return None
        if old:
            row["_old_pipeline"] = True
            row["_import_tagged"] = _import_tagged(item)
        self._write_ghl([row], source)
        # A move is an offer or a contract on today's scorecard - show it now.
        if self.settings.ghl_owns_day(self.today().isoformat()):
            try:
                self.compute_activity(self.today(), self.today())
            except Exception:  # noqa: BLE001 - the next poll will catch up
                log.exception("activity recompute after webhook failed")
        return row

    # ------------------------------------------------------------------
    # GHL activity -> the scorecard
    # ------------------------------------------------------------------
    def _day_ms(self, day: date, end: bool = False) -> int:
        moment = time_of_day.max if end else time_of_day.min
        return int(datetime.combine(day, moment)
                   .replace(tzinfo=self.settings.tz).timestamp() * 1000)

    def fetch_messages(self, since: date) -> int:
        """Pull calls and texts since ``since`` into ghl_message."""
        tz = self.settings.tz
        since_ms = self._day_ms(since)
        written = 0
        for channel, kind in (("Call", "call"), ("SMS", "sms")):
            batch = []
            for item in self.ghl.iter_messages(channel, since_ms):
                row = activity.message_row(item, kind, tz)
                if row:
                    batch.append(row)
            written += self.store.upsert_messages(batch)
        return written

    def trace_activity(self, first: date, last: date) -> list[dict]:
        """Which lead earned each stage-move and claim credit, day by day.

        The same replay compute_activity does (same inputs, same compute_day),
        but it returns the per-lead trail instead of writing totals - so the
        Leaderboard's lead lists always match its numbers. Days before the
        GHL activity cutover are skipped: those totals came from InvestorFuse.
        """
        cutover = date.fromisoformat(self.settings.ghl_activity_cutover)
        first = max(first, cutover)
        if first > last:
            return []
        _names, uids, numbers = self._reference()
        lead_in = self._day_ms(first) - activity.CREDIT_WINDOW_MS
        end = self._day_ms(last, end=True)
        messages = self.store.messages_between(lead_in, end)
        history = self.store.stage_moves_between(self._day_ms(first), end)
        assignments = self.store.assignments_between(self._day_ms(first), end)
        leads = self.store.leads_by_id(list(history) + [a["opp_id"] for a in assignments])
        old_contacts = self.store.old_contact_ids()
        trail: list[dict] = []
        day = first
        while day <= last:
            activity.compute_day(day.isoformat(), self.settings.tz, messages, history,
                                 leads, assignments, uids, numbers, old_contacts,
                                 trace=trail)
            day += timedelta(days=1)
        return trail

    def compute_activity(self, first: date, last: date) -> int:
        """Recompute GHL-owned scorecard days from what's stored.

        Purely local - no API calls - so recomputing a fortnight is cheap,
        and a correction (a user merged, a definition changed) reaches every
        day it touches the next time this runs.
        """
        cutover = date.fromisoformat(self.settings.ghl_activity_cutover)
        first = max(first, cutover)
        if first > last:
            return 0
        _names, uids, numbers = self._reference()
        # Three days of lead-in, so a stage move can be credited to the call
        # that earned it.
        lead_in = self._day_ms(first) - activity.CREDIT_WINDOW_MS
        end = self._day_ms(last, end=True)
        messages = self.store.messages_between(lead_in, end)
        history = self.store.stage_moves_between(self._day_ms(first), end)
        assignments = self.store.assignments_between(self._day_ms(first), end)
        leads = self.store.leads_by_id(list(history) + [a["opp_id"] for a in assignments])
        old_contacts = self.store.old_contact_ids()

        written = 0
        day = first
        while day <= last:
            rows = activity.compute_day(day.isoformat(), self.settings.tz, messages,
                                        history, leads, assignments, uids, numbers,
                                        old_contacts)
            written += self.store.replace_day_metrics(day.isoformat(), rows,
                                                      activity.GHL_METRICS
                                                      + activity.OLD_METRICS)
            day += timedelta(days=1)
        return written

    def resurfaced_in(self, first: date, last: date) -> int:
        """Older opportunities that came back to life in this window.

        Older is the point: one that was created in the window too is already
        in the lead total, and counting it here as well would double it in
        ``formFills``.
        """
        tz = self.settings.tz
        start = int(datetime.combine(first, time_of_day.min)
                    .replace(tzinfo=tz).timestamp() * 1000)
        end = int(datetime.combine(last, time_of_day.max)
                  .replace(tzinfo=tz).timestamp() * 1000)
        return self.store.resurfaced_between(start, end,
                                             created_before=first.isoformat())

    def archive_month(self, month: str | None = None) -> str | None:
        """Freeze one month's funnel. Defaults to the month just finished."""
        today = self.today()
        if month is None:
            last_day_prev = today.replace(day=1) - timedelta(days=1)
            month = last_day_prev.strftime("%Y-%m")

        year, mon = (int(p) for p in month.split("-"))
        first = date(year, mon, 1)
        last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)

        leads = self.store.leads_between(first.isoformat(), last.isoformat())
        if not leads:
            log.info("no leads for %s, nothing archived", month)
            return None

        payload = build(leads, resurfaced=self.resurfaced_in(first, last))
        payload["month"] = month
        payload["range"] = {"from": first.isoformat(), "to": last.isoformat()}
        self.store.put_month(month, payload)
        log.info("archived %s (%d leads)", month, payload["total"])
        return month

    # ------------------------------------------------------------------
    def poll_once(self) -> None:
        today = self.today()
        scorecard_errors: list[str] = []
        lead_errors: list[str] = []

        if self.settings.if_configured:
            for day in (today, today - timedelta(days=1)):
                try:
                    self.collect_day(day)
                except Exception as exc:  # noqa: BLE001
                    scorecard_errors.append(f"scorecard {day.isoformat()}: {exc}")
                    log.exception("failed collecting %s", day)

        errors = list(scorecard_errors)

        # Recent leads every poll; a deeper sweep once a day. Leads keep moving
        # for weeks after they arrive, but a lead from last spring is settled -
        # except that it can still resurface, which is why the deep sweep runs
        # at all rather than never.
        deep = self.store.get_meta("deep_sync_day") != today.isoformat()

        # GHL calls and texts first: the lead sync below reads them for each
        # lead's touches. Today and yesterday every poll (a call's length
        # lands after it ends); everything since the cutover once a day.
        activity_from = None
        # Stage families re-derived from names before anything counts them,
        # so a rule fix (or a renamed stage) corrects history too.
        if self.settings.ghl_configured:
            try:
                fixed = self.store.refamily_stage_events(ghl_stage_family)
                if fixed:
                    log.info("re-derived the family of %d stage name(s) in history", fixed)
            except Exception:  # noqa: BLE001
                log.exception("stage family re-derivation failed")
        if self.settings.activity_enabled:
            cutover = date.fromisoformat(self.settings.ghl_activity_cutover)
            activity_from = max(cutover, today - timedelta(days=60 if deep else 1))
            try:
                self.fetch_messages(activity_from)
            except Exception as exc:  # noqa: BLE001
                lead_errors.append(f"messages: {exc}")
                errors.append(f"messages: {exc}")
                log.exception("GHL message fetch failed")
        try:
            self.sync_leads(self.settings.lead_deep_days if deep else None)
            if deep:
                self.store.set_meta("deep_sync_day", today.isoformat())
        except Exception as exc:  # noqa: BLE001
            lead_errors.append(f"lead sync: {exc}")
            errors.append(f"lead sync: {exc}")
            log.exception("lead sync failed")

        # The scorecard days GHL owns, rebuilt from what was just stored.
        if activity_from is not None:
            try:
                self.compute_activity(activity_from, today)
                self.store.set_meta("ghl_activity_last", datetime.now(self.settings.tz).isoformat())
            except Exception as exc:  # noqa: BLE001
                lead_errors.append(f"activity: {exc}")
                errors.append(f"activity: {exc}")
                log.exception("GHL activity compute failed")

        # Call-ins without an owner are invisible to agents - give them one.
        # After the activity rebuild, so the calls just fetched are in.
        if self.call_assigner is not None and self.call_assigner.mode in ("dry", "live"):
            try:
                self.call_assigner.run_items(self._recent_items)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"call assign: {exc}")
                log.exception("call-in auto-assignment failed")

        # Step 4: report new pipeline outcomes on Google leads back to Google.
        if self.uploader is not None and self.ghl is not None:
            try:
                self.uploader.run()
            except Exception as exc:  # noqa: BLE001 - never fail a poll for this
                errors.append(f"google upload: {exc}")
                log.exception("Google conversion upload failed")

        # Old lead, new form fill - found by fingerprinting the leads we just
        # pulled against the ones already stored, on every poll rather than
        # whenever somebody remembers to upload a Meta export. A seller who
        # came back is only worth knowing about while they are still
        # deciding, so the flag has to be on the lead before anyone opens the
        # page, not weeks later.
        try:
            linked = self.store.link_repeat_sellers(self.settings.lead_deep_days)
            if linked:
                log.info("repeat sellers: %d opportunities newly flagged", linked)
        except Exception as exc:  # noqa: BLE001 - never fail a poll for this
            log.warning("repeat-seller linking failed: %s", exc)

        # Archive last month once, shortly after it ends.
        prev_month = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        if self.store.get_meta(f"archived:{prev_month}") != "yes":
            try:
                if self.archive_month(prev_month):
                    self.store.set_meta(f"archived:{prev_month}", "yes")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"month archive {prev_month}: {exc}")
                log.exception("archiving %s failed", prev_month)

        now = datetime.now(self.settings.tz).isoformat()

        if errors:
            self.store.set_meta("last_error", " | ".join(errors)[:2000])
            self.store.set_meta("last_error_at", now)
        else:
            self.store.set_meta("last_error", "")

        # The freshness badge tracks whatever the dashboard is built on. Under
        # InvestorFuse that was the scorecard; under GHL it is the lead sync,
        # since GHL has no scorecard. A failed month archive is worth logging
        # but should not mark the whole page stale.
        primary = list(scorecard_errors)
        if self.ghl is not None:
            primary += lead_errors
        if not primary:
            self.store.set_meta("last_success", now)
        # While the InvestorFuse login still works, keep a full copy (daily).
        self.maybe_if_snapshot()

    # ------------------------------------------------------------------
    # InvestorFuse final snapshot
    # ------------------------------------------------------------------
    # InvestorFuse is being switched off. Everything the dashboard shows from
    # before the GHL cutover lives in this database, and nothing re-reads it
    # once GHL took over - so while the InvestorFuse login still works, take
    # one complete copy, once a day: every opportunity at its latest stage
    # (offers, contracts, repeat fills), and every scorecard day up to the
    # cutover re-pulled in full. When the login stops working this fails
    # quietly and the last good copy stays.
    IF_SNAPSHOT_EVERY = 20 * 3600

    def maybe_if_snapshot(self) -> None:
        if not self.settings.if_configured or getattr(self, "_if_snap_running", False):
            return
        import json as _json
        try:
            last = _json.loads(self.store.get_meta("if_snapshot") or "{}")
        except ValueError:
            last = {}
        if time.time() - float(last.get("startedTs") or 0) < self.IF_SNAPSHOT_EVERY:
            return
        self._if_snap_running = True
        threading.Thread(target=self._if_snapshot_safe, name="if-snapshot", daemon=True).start()

    def _if_snapshot_safe(self) -> None:
        import json as _json
        info: dict = {"startedTs": time.time(),
                      "started": datetime.now(self.settings.tz).isoformat()}
        try:
            info.update(self.if_snapshot())
            info["ok"] = not info.get("errors")
            if info["ok"]:
                info["lastGood"] = info["started"]
        except Exception as exc:  # noqa: BLE001
            log.exception("InvestorFuse snapshot failed")
            info["ok"] = False
            info["errors"] = [str(exc)[:300]]
        try:
            prev = _json.loads(self.store.get_meta("if_snapshot") or "{}")
        except ValueError:
            prev = {}
        if prev.get("deepDone"):
            info["deepDone"] = True
        if not info.get("ok") and prev.get("lastGood"):
            info["lastGood"] = prev["lastGood"]
            info["lastGoodLeads"] = prev.get("lastGoodLeads", prev.get("leads"))
        elif info.get("ok"):
            info["lastGoodLeads"] = info.get("leads")
        info["finished"] = datetime.now(self.settings.tz).isoformat()
        self.store.set_meta("if_snapshot", _json.dumps(info))
        self._if_snap_running = False
        log.info("InvestorFuse snapshot: %s", info)

    def if_snapshot(self) -> dict:
        tz = self.settings.tz
        errors: list[str] = []
        # 1. Every opportunity, whatever its created date, at its latest state.
        leads = 0
        batch: list[dict] = []
        for item in self.client.iter_opportunities(date_from_ms=None):
            row = project(item, tz)
            if row:
                batch.append(row)
            if len(batch) >= 200:
                leads += self.store.upsert_leads(batch)
                batch = []
        leads += self.store.upsert_leads(batch)
        # 2. Every scorecard day before GHL owns the day, re-pulled in full -
        #    a day collected while it was still happening holds partial totals.
        #    The first good run reaches back a year, so history the backfill
        #    window never covered is saved too; later runs re-pull only the
        #    last 45 days before the cutover, which is all that still moves.
        import json as _json
        today = self.today()
        cutover = (date.fromisoformat(self.settings.ghl_activity_cutover)
                   if self.settings.activity_enabled else today + timedelta(days=1))
        try:
            deep_done = bool(_json.loads(self.store.get_meta("if_snapshot") or "{}").get("deepDone"))
        except ValueError:
            deep_done = False
        collected = sorted(self.store.collected_days())
        if deep_done:
            start = cutover - timedelta(days=45)
        else:
            start = min([today - timedelta(days=365)]
                        + ([date.fromisoformat(collected[0])] if collected else []))
        days = 0
        day = start
        while day < min(cutover, today + timedelta(days=1)):
            if self._stop.is_set():
                break
            try:
                self.collect_day(day)
                days += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"scorecard {day.isoformat()}: {exc}"[:200])
                if len(errors) > 5:
                    break
            day += timedelta(days=1)
        return {"leads": leads, "scorecardDays": days,
                "scorecardFrom": start.isoformat(), "errors": errors,
                "deepDone": deep_done or not errors}

    def backfill(self) -> None:
        """Collect any day in the window not already recorded.

        Wrapped, because this runs in its own thread: anything escaping here
        would vanish silently and leave the dashboard permanently short of
        history. Record it where the dashboard can show it instead.
        """
        try:
            self._backfill()
        except Exception as exc:  # noqa: BLE001
            log.exception("backfill aborted")
            self.store.set_meta("last_error", f"backfill aborted: {exc}"[:2000])

    def _backfill(self) -> None:
        if not self.settings.if_configured:
            return  # scorecard history is InvestorFuse-only
        already = self.store.collected_days()
        today = self.today()
        pending = [
            today - timedelta(days=offset)
            for offset in range(1, self.settings.backfill_days + 1)
        ]
        pending = [d for d in pending if d.isoformat() not in already
                   and not self.settings.ghl_owns_day(d.isoformat())]
        if not pending:
            return

        log.info("backfilling %d days of scorecard history", len(pending))
        for day in pending:
            if self._stop.is_set():
                return
            try:
                self.collect_day(day)
            except Exception as exc:  # noqa: BLE001
                log.warning("backfill failed for %s: %s", day, exc)
            # Be a polite client - this is an unofficial API.
            time.sleep(1.5)
        log.info("backfill complete")

    # ------------------------------------------------------------------
    def run_forever(self) -> None:
        # First poll immediately so the dashboard has something to show.
        try:
            self.poll_once()
        except Exception:  # noqa: BLE001
            log.exception("initial poll failed")

        threading.Thread(target=self.backfill, name="backfill", daemon=True).start()

        interval = max(60, self.settings.poll_minutes * 60)
        while not self._stop.wait(interval):
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001
                log.exception("poll failed")

    # ------------------------------------------------------------------
    def refresh_now(self) -> bool:
        """Kick off a poll immediately, from the Refresh button.

        Returns False if one is already running: two concurrent polls would
        just double the load on someone else's API and race each other into
        the same rows.
        """
        if self._refreshing.is_set():
            return False
        self._refreshing.set()

        def run():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001
                log.exception("manual refresh failed")
            finally:
                self._refreshing.clear()

        threading.Thread(target=run, name="manual-refresh", daemon=True).start()
        return True

    @property
    def refreshing(self) -> bool:
        return self._refreshing.is_set()

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.run_forever, name="collector", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()
