import os
import time
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, List, Any

import requests
import psycopg
from psycopg.rows import dict_row

# Display timezone for logs. Dubai/GST is fixed UTC+4 (no DST), so a fixed
# offset is safe and needs no tzdata in the slim image.
# TZ=Asia/Dubai | GST | UTC+4 | +4 -> GST display. Anything else -> UTC.
_TZ_RAW = os.getenv("TZ", "Asia/Dubai").strip()
if _TZ_RAW.lower() in ("asia/dubai", "gst", "gulf", "uae", "utc+4", "+4", "utc04", "+04:00"):
    DISPLAY_TZ = timezone(timedelta(hours=4), name="GST")
    DISPLAY_TZ_LABEL = "GST"
else:
    DISPLAY_TZ = timezone.utc
    DISPLAY_TZ_LABEL = "Z"


class _TzFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=DISPLAY_TZ)
        if datefmt:
            s = dt.strftime(datefmt)
        else:
            s = dt.isoformat()
        return s + ("" if s.endswith(("Z", "GST")) else f" {DISPLAY_TZ_LABEL}")


_handler = logging.StreamHandler()
_handler.setFormatter(_TzFormatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), handlers=[_handler], force=True)
logging.info("Log timezone: %s (%s)", _TZ_RAW, "UTC+4 GST" if DISPLAY_TZ_LABEL == "GST" else "UTC")


def _fmt_next_run(deadline_epoch: float) -> str:
    dt = datetime.fromtimestamp(deadline_epoch, tz=DISPLAY_TZ)
    if DISPLAY_TZ_LABEL == "GST":
        return dt.strftime("%Y-%m-%d %H:%M:%S GST")
    return dt.strftime("%Y-%m-%d %H:%M:%SZ")

API_URL = os.getenv("CURSOR_API_URL", "https://api.cursor.com").rstrip("/")
API_KEY = os.environ["CURSOR_API_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]
INTERVAL = int(os.getenv("COLLECTION_INTERVAL_SECONDS", "3600"))
TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))

GROUP_NAMES = [
    x.strip()
    for x in os.getenv("GROUP_NAMES", "").split(",")
    if x.strip()
]
EXCLUDE_GROUP_NAMES = {
    x.strip()
    for x in os.getenv("EXCLUDE_GROUP_NAMES", "").split(",")
    if x.strip()
}
SHOW_UNASSIGNED = os.getenv("SHOW_UNASSIGNED_USERS", "true").lower() in ("1", "true", "yes")
UNASSIGNED_GROUP_NAME = os.getenv("UNASSIGNED_GROUP_NAME", "unassigned-users")
UNASSIGNED_GROUP_ID = "__unassigned__"

session = requests.Session()
session.auth = (API_KEY, "")
session.headers.update({"Accept": "application/json"})


def cursor_get(path: str, params=None) -> Dict[str, Any]:
    import requests as _rq
    url = f"{API_URL}{path}"
    last_error = None
    for attempt in range(4):
        try:
            response = session.get(url, params=params, timeout=TIMEOUT)
        except (_rq.exceptions.Timeout, _rq.exceptions.ConnectionError) as e:
            logging.warning("Cursor GET %s timeout/conn (attempt %d/4): %s", url, attempt + 1, e)
            time.sleep(3 * (attempt + 1))
            last_error = e
            continue
        if response.status_code == 429:
            wait = int(response.headers.get("Retry-After", "5"))
            logging.warning("Cursor GET %s 429, retry %d in %ss", url, attempt + 1, wait)
            time.sleep(wait)
            continue
        if response.status_code >= 500:
            logging.warning("Cursor GET %s %s, retry %d", url, response.status_code, attempt + 1)
            last_error = response
            time.sleep(2 * (attempt + 1))
            continue
        if response.status_code >= 400:
            raise RuntimeError(
                f"Cursor GET {url} failed: HTTP {response.status_code}: {response.text[:1000]}"
            )
        return response.json()
    if last_error is not None and hasattr(last_error, "status_code"):
        raise RuntimeError(
            f"Cursor GET {url} failed after retries: HTTP {last_error.status_code}: {last_error.text[:1000]}"
        )
    raise RuntimeError(f"Cursor GET {url} failed after retries (timeout/conn): {last_error}")


def cursor_post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    import requests as _rq
    url = f"{API_URL}{path}"
    last_error = None
    for attempt in range(4):
        try:
            response = session.post(url, json=payload, timeout=TIMEOUT)
        except (_rq.exceptions.Timeout, _rq.exceptions.ConnectionError) as e:
            logging.warning("Cursor POST %s timeout/conn (attempt %d/4): %s", url, attempt + 1, e)
            time.sleep(3 * (attempt + 1))
            last_error = e
            continue
        if response.status_code == 429:
            wait = int(response.headers.get("Retry-After", "5"))
            logging.warning("Cursor POST %s 429, retry %d in %ss", url, attempt + 1, wait)
            time.sleep(wait)
            continue
        if response.status_code >= 500:
            logging.warning("Cursor POST %s %s, retry %d", url, response.status_code, attempt + 1)
            last_error = response
            time.sleep(2 * (attempt + 1))
            continue
        if response.status_code >= 400:
            raise RuntimeError(
                f"Cursor POST {url} failed: HTTP {response.status_code}: {response.text[:1000]}"
            )
        return response.json()
    if last_error is not None and hasattr(last_error, "status_code"):
        raise RuntimeError(
            f"Cursor POST {url} failed after retries: HTTP {last_error.status_code}: {last_error.text[:1000]}"
        )
    raise RuntimeError(f"Cursor POST {url} failed after retries (timeout/conn): {last_error}")


def get_all_directory_groups() -> List[Dict[str, Any]]:
    groups = []
    page = 1

    while True:
        data = cursor_get(
            "/teams/directory-groups",
            {"page": page, "pageSize": 200},
        )
        groups.extend(data.get("groups", []))
        pagination = data.get("pagination", {})
        if not pagination.get("hasNextPage", False):
            break
        page += 1

    return groups


def get_group_members(group_id: str) -> List[Dict[str, Any]]:
    members = []
    page = 1

    while True:
        data = cursor_get(
            f"/teams/directory-groups/{group_id}/members",
            {"page": page, "pageSize": 200},
        )
        members.extend(data.get("members", []))
        pagination = data.get("pagination", {})
        if not pagination.get("hasNextPage", False):
            break
        page += 1

    return members


def cents_to_dollars(value) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value)) / Decimal("100")


def get_spend() -> tuple[Dict[str, Dict[str, Any]], Any]:
    """Return (spend keyed by user ID, subscriptionCycleStart).

    Total consumption must include included usage, not just on-demand:
    - pooled plans return overallSpendCents (total)
    - per-member allowance plans return spendCents (overage) + includedSpendCents
    - old responses may only have spendCents
    """
    result = {}
    cycle_start = None
    page = 1

    while True:
        data = cursor_post(
            "/teams/spend",
            {
                "sortBy": "amount",
                "sortDirection": "desc",
                "page": page,
                "pageSize": 1000,
            },
        )

        if cycle_start is None:
            cycle_start = data.get("subscriptionCycleStart")

        if page == 1 and data.get("teamMemberSpend"):
            sample = data["teamMemberSpend"][0]
            logging.info("Spend sample keys: %s", sorted(sample.keys()))
            logging.info(
                "Spend sample: spendCents=%s overallSpendCents=%s includedSpendCents=%s effectiveLimit=%s",
                sample.get("spendCents"),
                sample.get("overallSpendCents"),
                sample.get("includedSpendCents"),
                sample.get("effectivePerUserLimitDollars"),
            )

        for member in data.get("teamMemberSpend", []):
            member = dict(member)
            spend_cents = member.get("spendCents")
            overall_cents = member.get("overallSpendCents")
            included_cents = member.get("includedSpendCents")

            if overall_cents is not None:
                total_cents = overall_cents
                overage_cents = spend_cents
                included_derived = Decimal(str(total_cents)) - Decimal(str(overage_cents or 0))
                if included_derived < 0:
                    included_derived = Decimal("0")
            elif included_cents is not None:
                total_cents = Decimal(str(spend_cents or 0)) + Decimal(str(included_cents))
                overage_cents = spend_cents
                included_derived = Decimal(str(included_cents))
            else:
                # Legacy shape: spendCents only. Assume it is the total.
                total_cents = spend_cents or 0
                overage_cents = spend_cents
                included_derived = Decimal("0")

            member["spend"] = cents_to_dollars(total_cents)
            member["spend_overage"] = cents_to_dollars(overage_cents)
            member["spend_included"] = cents_to_dollars(included_derived)
            result[member["userId"]] = member

        total_pages = int(data.get("totalPages", 1))
        if page >= total_pages:
            break
        page += 1

    logging.info("Fetched spend for %d users (cycleStart=%s)", len(result), cycle_start)
    return result, cycle_start


def get_usage_events_spend(start_ms: int, end_ms: int) -> Dict[str, Decimal]:
    """Sum chargedCents per userEmail from /teams/filtered-usage-events.

    This is the same Monthly Usage dollars the dashboard shows.
    Docs: sum chargedCents to reconcile with /teams/spend totals.
    Returns {email_lower: dollars}.
    """
    totals_cents: Dict[str, Decimal] = {}
    page = 1
    total_events = 0
    while True:
        data = cursor_post(
            "/teams/filtered-usage-events",
            {"startDate": start_ms, "endDate": end_ms, "page": page, "pageSize": 1000},
        )
        events = data.get("usageEvents", [])
        if page == 1:
            logging.info(
                "Usage-events sample keys: %s total=%s period=%s",
                sorted(events[0].keys()) if events else [],
                data.get("totalUsageEventsCount"),
                data.get("period"),
            )
        for ev in events:
            email = (ev.get("userEmail") or "").lower()
            if not email:
                continue
            charged = ev.get("chargedCents")
            if charged is None:
                # fallback for older shapes
                tu = ev.get("tokenUsage") or {}
                charged = tu.get("totalCents", 0)
            totals_cents[email] = totals_cents.get(email, Decimal("0")) + Decimal(str(charged))
            total_events += 1
        pagination = data.get("pagination", {})
        # API uses numPages/currentPage or totalPages; handle both
        num_pages = pagination.get("numPages") or data.get("totalPages") or 1
        has_next = pagination.get("hasNextPage", page < int(num_pages))
        if not has_next or not events:
            break
        page += 1
        if page > 200:
            logging.warning("Usage-events pagination stopped at page 200 (safety)")
            break
    result = {k: v / Decimal("100") for k, v in totals_cents.items()}
    logging.info("Summed usage-events: %d events, %d users, total=$%s", total_events, len(result), sum(result.values(), Decimal("0")))
    return result


def selected_groups(all_groups):
    # Dynamic mode: GROUP_NAMES empty, "*", or "auto" tracks every directory group.
    # Renames then can't break refresh; use EXCLUDE_GROUP_NAMES to skip any.
    if not GROUP_NAMES or GROUP_NAMES in (["*"], ["auto"], ["all"]):
        picked = [g for g in all_groups if g.get("name") not in EXCLUDE_GROUP_NAMES]
        picked.sort(key=lambda g: g.get("name", ""))
        logging.info("Auto-discovered %d groups (excluded=%s)", len(picked), sorted(EXCLUDE_GROUP_NAMES) or "none")
        return picked

    by_name = {g["name"]: g for g in all_groups}

    missing = [name for name in GROUP_NAMES if name not in by_name]
    if missing:
        logging.error("Available directory groups: %s", sorted(by_name.keys()))
        raise RuntimeError(
            "Configured groups not found in Cursor: " + ", ".join(missing)
            + ". Available: " + ", ".join(sorted(by_name.keys()))
        )

    return [by_name[name] for name in GROUP_NAMES]


def ensure_schema():
    """Create new columns on existing volumes (init.sql only runs on first boot)."""
    stmts = [
        "ALTER TABLE cursor_member_current ADD COLUMN IF NOT EXISTS spend_overage NUMERIC(14,4) NOT NULL DEFAULT 0",
        "ALTER TABLE cursor_member_current ADD COLUMN IF NOT EXISTS spend_included NUMERIC(14,4) NOT NULL DEFAULT 0",
        "ALTER TABLE cursor_group_member_current ADD COLUMN IF NOT EXISTS spend_overage NUMERIC(14,4) NOT NULL DEFAULT 0",
        "ALTER TABLE cursor_group_member_current ADD COLUMN IF NOT EXISTS spend_included NUMERIC(14,4) NOT NULL DEFAULT 0",
        "ALTER TABLE collector_status ADD COLUMN IF NOT EXISTS subscription_cycle_start BIGINT",
    ]
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            for s in stmts:
                cur.execute(s)
            cur.execute("DROP VIEW IF EXISTS cursor_user_report")
            cur.execute(
                """CREATE VIEW cursor_user_report AS
                   SELECT m.group_id, g.group_name, m.user_id, m.email, m.display_name,
                          m.spend, m.spend_overage, m.spend_included,
                          m.group_limit_per_user, m.effective_limit,
                          GREATEST(COALESCE(m.group_limit_per_user,0), COALESCE(m.effective_limit,0)) AS applicable_limit,
                          CASE WHEN GREATEST(COALESCE(m.group_limit_per_user,0), COALESCE(m.effective_limit,0)) = 0 THEN NULL
                               ELSE ROUND((m.spend / GREATEST(COALESCE(m.group_limit_per_user,0), COALESCE(m.effective_limit,0))) * 100, 2)
                          END AS group_usage_percent, m.refreshed_at
                   FROM cursor_group_member_current m JOIN cursor_group_current g ON g.group_id = m.group_id
                   ORDER BY g.group_name, group_usage_percent DESC NULLS LAST"""
            )
        conn.commit()


def refresh():
    try:
        ensure_schema()
    except Exception:
        logging.exception("ensure_schema failed (continuing)")
    logging.info("Fetching directory groups...")
    all_groups = get_all_directory_groups()
    logging.info("Fetched %d directory groups", len(all_groups))
    groups = selected_groups(all_groups)
    logging.info("Fetching spend...")
    spend, cycle_start = get_spend()
    logging.info("Spend done, fetching usage-events...")
    spend_by_email = {v.get("email", "").lower(): v for v in spend.values() if v.get("email")}

    # Monthly Usage total per user from usage-events (same dollars as dashboard).
    # Uses billing-cycle start when available, else last 30 days.
    import time as _time
    end_ms = int(_time.time() * 1000)
    try:
        start_ms = int(cycle_start) if cycle_start else end_ms - 30 * 24 * 3600 * 1000
    except (ValueError, TypeError):
        start_ms = end_ms - 30 * 24 * 3600 * 1000
    try:
        events_spend = get_usage_events_spend(start_ms, end_ms)
    except Exception:
        logging.exception("Usage-events fetch failed, falling back to /teams/spend only")
        events_spend = {}

    group_rows = []
    group_member_rows = []
    member_snapshot = {}

    # Track memberships so we can expose multiple-group membership.
    memberships = {}
    matched = 0
    unmatched_emails = []

    for group in groups:
        group_id = group["id"]
        group_name = group["name"]
        group_limit = group.get("monthlySpendingLimitDollars")
        members = get_group_members(group_id)

        for m in members:
            user_id = m["userId"]
            email = m.get("email", "")
            name = m.get("name", "")

            spend_record = spend.get(user_id)
            if spend_record is None and email:
                # userId namespace drift fallback
                spend_record = spend_by_email.get(email.lower(), {})
            if spend_record:
                matched += 1
            else:
                spend_record = {}
                if len(unmatched_emails) < 5:
                    unmatched_emails.append(email or user_id)
            # Primary: monthly usage from events (matches dashboard Members Monthly Usage).
            # Fallback: /teams/spend total.
            events_dollars = events_spend.get(email.lower()) if email else None
            if events_dollars is not None:
                user_spend = Decimal(str(events_dollars))
            else:
                user_spend = Decimal(str(spend_record.get("spend", 0)))
            user_overage = Decimal(str(spend_record.get("spend_overage", 0)))
            # included = total - overage (clamped), so breakdown stays consistent
            user_included = user_spend - user_overage
            if user_included < 0:
                user_included = Decimal("0")
            effective_limit = spend_record.get("effectivePerUserLimitDollars")

            if effective_limit is not None:
                effective_limit = Decimal(str(effective_limit))

            memberships.setdefault(user_id, set()).add(group_id)

            member_snapshot[user_id] = {
                "email": email,
                "name": name,
                "spend": user_spend,
                "spend_overage": user_overage,
                "spend_included": user_included,
                "effective_limit": effective_limit,
            }

            group_member_rows.append(
                (
                    group_id,
                    user_id,
                    email,
                    name,
                    user_spend,
                    user_overage,
                    user_included,
                    effective_limit,
                    Decimal(str(group_limit)) if group_limit is not None else None,
                )
            )

    logging.info(
        "Spend join: %d/%d group members matched, unmatched sample=%s",
        matched,
        len(group_member_rows),
        unmatched_emails,
    )
    events_total = sum(events_spend.values(), Decimal("0")) if events_spend else Decimal("0")
    spend_total = sum((Decimal(str(v.get("spend", 0))) for v in spend.values()), Decimal("0"))
    logging.info("Source compare: events_total=$%s spend_total=$%s", events_total, spend_total)

    # Unassigned: team users in /teams/spend who are in none of the selected groups.
    if SHOW_UNASSIGNED:
        assigned_ids = set(member_snapshot.keys())
        unassigned_count = 0
        for user_id, rec in spend.items():
            if user_id in assigned_ids:
                continue
            email = rec.get("email", "") or ""
            name = rec.get("name", "") or ""
            events_dollars = events_spend.get(email.lower()) if email else None
            if events_dollars is not None:
                user_spend = Decimal(str(events_dollars))
            else:
                user_spend = Decimal(str(rec.get("spend", 0)))
            user_overage = Decimal(str(rec.get("spend_overage", 0)))
            user_included = user_spend - user_overage
            if user_included < 0:
                user_included = Decimal("0")
            effective_limit = rec.get("effectivePerUserLimitDollars")
            if effective_limit is not None:
                effective_limit = Decimal(str(effective_limit))
            member_snapshot[user_id] = {
                "email": email,
                "name": name,
                "spend": user_spend,
                "spend_overage": user_overage,
                "spend_included": user_included,
                "effective_limit": effective_limit,
            }
            group_member_rows.append(
                (
                    UNASSIGNED_GROUP_ID,
                    user_id,
                    email,
                    name,
                    user_spend,
                    user_overage,
                    user_included,
                    effective_limit,
                    None,
                )
            )
            unassigned_count += 1
        logging.info("Unassigned users: %d (name=%s)", unassigned_count, UNASSIGNED_GROUP_NAME)

    # A directory-group limit is a per-user limit. If members have a
    # temporary user hard-limit override, Cursor's /teams/spend exposes
    # effectivePerUserLimitDollars. For the group report we use the group
    # limit as the allocation unless the effective user limit is higher.
    #
    # This mirrors Cursor's "highest applicable limit wins" behavior.
    for group in groups:
        group_id = group["id"]
        group_name = group["name"]
        group_limit_raw = group.get("monthlySpendingLimitDollars")
        group_limit = (
            Decimal(str(group_limit_raw))
            if group_limit_raw is not None
            else None
        )

        members = [r for r in group_member_rows if r[0] == group_id]

        total_consumption = sum((r[4] for r in members), Decimal("0"))

        total_limit = Decimal("0")
        has_unlimited = False
        over_80 = 0
        over_90 = 0

        for r in members:
            user_spend = r[4]
            user_effective = r[7]

            # Effective user limit can be higher because of a temporary
            # exception/hard-limit override.
            if user_effective is not None:
                applicable_limit = max(
                    group_limit if group_limit is not None else Decimal("0"),
                    user_effective,
                )
            else:
                applicable_limit = group_limit

            if applicable_limit is None:
                has_unlimited = True
            else:
                total_limit += applicable_limit
                pct = (
                    (user_spend / applicable_limit) * Decimal("100")
                    if applicable_limit > 0
                    else Decimal("0")
                )
                if pct >= 80:
                    over_80 += 1
                if pct >= 90:
                    over_90 += 1

        remaining = None if has_unlimited else total_limit - total_consumption
        utilization = (
            None
            if has_unlimited or total_limit == 0
            else (total_consumption / total_limit) * Decimal("100")
        )

        group_rows.append(
            (
                group_id,
                group_name,
                len(members),
                group_limit,
                total_limit if not has_unlimited else None,
                total_consumption,
                remaining,
                utilization,
                over_80,
                over_90,
            )
        )

    # Synthetic unassigned-users row: same stats, limit = sum of effective limits.
    if SHOW_UNASSIGNED:
        u_members = [r for r in group_member_rows if r[0] == UNASSIGNED_GROUP_ID]
        if u_members:
            u_consumption = sum((r[4] for r in u_members), Decimal("0"))
            u_limit = Decimal("0")
            u_has_unlimited = False
            u_80 = 0
            u_90 = 0
            for r in u_members:
                user_spend = r[4]
                applicable = r[7]  # effective limit only, no group limit
                if applicable is None:
                    u_has_unlimited = True
                else:
                    u_limit += applicable
                    pct = (user_spend / applicable) * Decimal("100") if applicable > 0 else Decimal("0")
                    if pct >= 80:
                        u_80 += 1
                    if pct >= 90:
                        u_90 += 1
            group_rows.append(
                (
                    UNASSIGNED_GROUP_ID,
                    UNASSIGNED_GROUP_NAME,
                    len(u_members),
                    None,
                    None if u_has_unlimited else u_limit,
                    u_consumption,
                    None if u_has_unlimited else u_limit - u_consumption,
                    None if (u_has_unlimited or u_limit == 0) else (u_consumption / u_limit) * Decimal("100"),
                    u_80,
                    u_90,
                )
            )

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            # This is deliberately a current-state database, not history.
            cur.execute("TRUNCATE cursor_group_current, cursor_group_member_current, cursor_member_current")

            cur.executemany(
                """
                INSERT INTO cursor_group_current
                (group_id, group_name, member_count, group_limit_per_user,
                 total_group_limit, current_consumption, remaining_amount,
                 utilization_percent, members_over_80, members_over_90, refreshed_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                """,
                group_rows,
            )

            # Member snapshot with group count.
            for user_id, data in member_snapshot.items():
                cur.execute(
                    """
                    INSERT INTO cursor_member_current
                    (user_id, email, display_name, spend, spend_overage, spend_included,
                     effective_limit, group_count, refreshed_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                    """,
                    (
                        user_id,
                        data["email"],
                        data["name"],
                        data["spend"],
                        data["spend_overage"],
                        data["spend_included"],
                        data["effective_limit"],
                        len(memberships.get(user_id, [])),
                    ),
                )

            cur.executemany(
                """
                INSERT INTO cursor_group_member_current
                (group_id, user_id, email, display_name, spend, spend_overage, spend_included,
                 effective_limit, group_limit_per_user, refreshed_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                """,
                group_member_rows,
            )

            cur.execute(
                """
                INSERT INTO collector_status(id,status,message,subscription_cycle_start,refreshed_at)
                VALUES (1,'OK',%s,%s,NOW())
                ON CONFLICT (id) DO UPDATE
                SET status=EXCLUDED.status,
                    message=EXCLUDED.message,
                    subscription_cycle_start=EXCLUDED.subscription_cycle_start,
                    refreshed_at=EXCLUDED.refreshed_at
                """,
                (f"Refreshed {len(groups)} groups and {len(member_snapshot)} users (unassigned={len([r for r in group_member_rows if r[0]==UNASSIGNED_GROUP_ID]) if SHOW_UNASSIGNED else 0}).", cycle_start),
            )

        conn.commit()

    logging.info(
        "Refresh successful: %s groups, %s users, total_spend=%s cycleStart=%s",
        len(groups),
        len(member_snapshot),
        sum((r[4] for r in group_member_rows), Decimal("0")),
        cycle_start,
    )


def record_error(message):
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO collector_status(id,status,message,refreshed_at)
                    VALUES (1,'ERROR',%s,NOW())
                    ON CONFLICT (id) DO UPDATE
                    SET status='ERROR',
                        message=EXCLUDED.message,
                        refreshed_at=EXCLUDED.refreshed_at
                    """,
                    (message[:4000],),
                )
            conn.commit()
    except Exception:
        logging.exception("Could not write collector error status")


def main():
    auto = (not GROUP_NAMES) or (GROUP_NAMES in (["*"], ["auto"], ["all"]))

    logging.info(
        "Group mode: %s",
        "auto-discover all" if auto else "Configured groups: " + ", ".join(GROUP_NAMES),
    )
    logging.info(
        "Schedule: interval=%ss show_unassigned=%s unassigned_name=%s",
        INTERVAL, SHOW_UNASSIGNED, UNASSIGNED_GROUP_NAME,
    )

    failures = 0
    while True:
        run_start = time.time()
        try:
            refresh()
            failures = 0
            wait = INTERVAL
            deadline = time.time() + wait
            logging.info(
                "Last run took %.1fs. Next refresh in %ss at %s",
                time.time() - run_start, wait, _fmt_next_run(deadline),
            )
        except Exception as exc:
            failures += 1
            logging.exception("Refresh failed")
            record_error(str(exc))
            # Retry soon on failure instead of waiting a full hour.
            wait = min(300, INTERVAL)
            deadline = time.time() + wait
            logging.info(
                "Last run took %.1fs. Retry %d in %ss at %s",
                time.time() - run_start, failures, wait, _fmt_next_run(deadline),
            )

        # Sleep in chunks so restart/stop is responsive.
        while time.time() < deadline:
            time.sleep(min(30, deadline - time.time()))


if __name__ == "__main__":
    main()
