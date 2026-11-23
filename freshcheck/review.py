"""Deadline (入仓时限) review.

The reviewer asks: for each arrival, *when the warehouse-entry deadline
(1–120 minutes after the first successful upload) was reached*, which side
was still blocking it — purchase only, barcode only, both, or neither?

Timeline model
--------------
* Time is the UTC instant at which a successful batch *started writing*
  (``upload_batches.received_at``, produced inside the write transaction).
* The base tables, blocking reasons and this timeline are all written in the
  same transaction, so they always become visible together ("同批生效").
  A failed batch rolls the whole transaction back, so its (never-written)
  states simply do not exist and the previous numbers stand
  ("上传失败沿用原数").
* One sparse row per arrival is appended only when the arrival is newly seen
  or its set of blocking sides changes — there are no per-minute rows
  ("不按分钟逐项写入").  Rows live in SQLite, so accumulation continues across
  process restarts ("重启后接着累计").

Deadline semantics
------------------
* Only arrivals whose number first appears in a *committed* batch at or after
  the feature switch (``app_meta.review_enabled_at``) count.
* The deadline instant is ``first_upload_at + limit`` and is **inclusive**:
  a correction landing exactly on it is counted ("恰好到点的更正算在内").
* When several batches share one write instant, the one with the larger batch
  id — the later in submission order — wins ("同刻按提交顺序取最后状态").

Read model
----------
The page never touches base tables; it only reads the summary tables.  It
issues at most one aggregate statement per calendar day, and each statement
is hard-capped at :data:`MAX_DAILY_RECORDS` rows crossing into Python.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

from .db import REVIEW_ENABLED_KEY, TS_FMT
from .sqlutil import chunks, placeholders

# Side masks stored in ``arrival_side_states.side_mask``.
MASK_NONE = 0          # released: neither side blocked at that instant
MASK_PURCHASE = 1      # 仅采购侧受阻
MASK_BARCODE = 2       # 仅用码侧受阻
MASK_BOTH = 3          # 两侧均受阻

# Hard cap on rows any single read statement may return for one reviewed day.
MAX_DAILY_RECORDS = 24

MAX_RANGE_DAYS = 31
MIN_LIMIT_MINUTES = 1
MAX_LIMIT_MINUTES = 120
# A day only becomes reviewable after its end plus this settling window.
SETTLING_HOURS = 2


class ReviewParamError(ValueError):
    """Raised when the review form parameters are invalid."""


def review_enabled_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT value FROM app_meta WHERE key = ?", (REVIEW_ENABLED_KEY,)
    ).fetchone()
    return row["value"] if row else None


def sync_batch(
    conn: sqlite3.Connection, batch_id: int, state_at: str, affected: set[str]
) -> None:
    """Maintain the first-seen table and the sparse side-state timeline.

    Called inside a successful upload transaction, after
    ``rules.recheck_arrivals`` has refreshed ``arrival_issues`` for exactly
    the arrivals this batch could move.  Only arrivals first seen at/after the
    feature switch are tracked; within that set a timeline row is inserted
    only for brand-new arrivals or when the blocked-side set differs from the
    last recorded one.
    """
    if not affected:
        return
    enabled_at = review_enabled_at(conn)
    if enabled_at is None:  # defensive: _migrate always sets the switch
        return

    affected = sorted(affected)
    # 1. Register first appearances.  An INSERT OR IGNORE keeps the very first
    #    successful batch even if the same number is corrected later.
    for batch in chunks(affected):
        marks = placeholders(len(batch))
        conn.execute(
            f"""
            INSERT OR IGNORE INTO arrival_first_seen (arrival_no, first_upload_at)
            SELECT arrival_no, ?
              FROM arrivals
             WHERE arrival_no IN ({marks})
            """,
            (state_at, *batch),
        )

    # 2. Only arrivals that came into the world after the switch are tracked.
    tracked_rows = []
    for batch in chunks(affected):
        marks = placeholders(len(batch))
        tracked_rows.extend(
            conn.execute(
                f"""
                SELECT fs.arrival_no, fs.first_upload_at
                  FROM arrival_first_seen fs
                 WHERE fs.arrival_no IN ({marks})
                   AND fs.first_upload_at >= ?
                """,
                (*batch, enabled_at),
            ).fetchall()
        )
    if not tracked_rows:
        return
    tracked = {row["arrival_no"]: row["first_upload_at"] for row in tracked_rows}

    # 3. Current blocking-side masks, straight from the issue cache that the
    #    same transaction just refreshed.
    masks: dict[str, int] = {}
    for batch in chunks(sorted(tracked)):
        marks = placeholders(len(batch))
        rows = conn.execute(
            f"""
            SELECT arrival_no,
                   SUM(CASE WHEN side = 'purchase' THEN 1 ELSE 0 END) AS purchase_side,
                   SUM(CASE WHEN side = 'barcode'  THEN 2 ELSE 0 END) AS barcode_side
              FROM arrival_issues
             WHERE arrival_no IN ({marks})
             GROUP BY arrival_no
            """,
            tuple(batch),
        ).fetchall()
        for row in rows:
            masks[row["arrival_no"]] = int(
                (row["purchase_side"] or 0) + (row["barcode_side"] or 0)
            )

    # 4. Compare with the last recorded state per arrival; append only on
    #    first sight or on an actual side-mask change.
    payload: list[tuple] = []
    for batch in chunks(sorted(tracked)):
        marks = placeholders(len(batch))
        last_rows = conn.execute(
            f"""
            SELECT s.arrival_no, s.side_mask
              FROM arrival_side_states s
              JOIN (
                    SELECT arrival_no, MAX(batch_id) AS max_batch
                      FROM arrival_side_states
                     WHERE arrival_no IN ({marks})
                     GROUP BY arrival_no
                   ) m ON m.arrival_no = s.arrival_no AND m.max_batch = s.batch_id
            """,
            tuple(batch),
        ).fetchall()
        last = {row["arrival_no"]: row["side_mask"] for row in last_rows}
        for arrival_no in batch:
            current = masks.get(arrival_no, MASK_NONE)
            if arrival_no not in last or last[arrival_no] != current:
                payload.append((arrival_no, batch_id, state_at, current))
    if payload:
        conn.executemany(
            "INSERT INTO arrival_side_states (arrival_no, batch_id, state_at, side_mask) "
            "VALUES (?, ?, ?, ?)",
            payload,
        )


def _parse_utc_date(value: str, field: str) -> date:
    raw = (value or "").strip()
    if not raw:
        raise ReviewParamError(f"请选择{field}。")
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ReviewParamError(f"{field}必须是 UTC 日期（YYYY-MM-DD）：{raw!r}") from exc


def parse_params(start: str, end: str, limit: str) -> tuple[date, date, int]:
    start_date = _parse_utc_date(start, "起始日期")
    end_date = _parse_utc_date(end, "截止日期")
    if end_date < start_date:
        raise ReviewParamError("截止日期不能早于起始日期。")
    if (end_date - start_date).days + 1 > MAX_RANGE_DAYS:
        raise ReviewParamError(f"日期范围最多 {MAX_RANGE_DAYS} 天（含首尾）。")
    raw_limit = (str(limit) if limit is not None else "").strip()
    try:
        minutes = int(raw_limit)
    except ValueError as exc:
        raise ReviewParamError("时限必须是 1～120 之间的整数分钟。") from exc
    if not (MIN_LIMIT_MINUTES <= minutes <= MAX_LIMIT_MINUTES):
        raise ReviewParamError("时限必须是 1～120 之间的整数分钟。")
    return start_date, end_date, minutes


def _mature_cutoff(now: datetime) -> date:
    """Newest UTC day whose end is already ``SETTLING_HOURS`` in the past."""
    return (now - timedelta(hours=SETTLING_HOURS)).date() - timedelta(days=1)


# One aggregate statement per reviewed day.  The window picks, for every
# arrival first seen on that day, the last timeline row whose write instant is
# at or before its (inclusive) deadline; ties at one instant resolve to the
# larger batch id (submission order).  All per-arrival work happens *inside*
# the database; the statement hands the application a single counter row per
# day (hard-capped at MAX_DAILY_RECORDS returned rows, well below the budget),
# so the read side never pulls raw timeline records across the driver.
_DAILY_SQL = f"""
WITH eligible AS (
    SELECT fs.arrival_no, fs.first_upload_at,
           add_minutes(fs.first_upload_at, ?) AS deadline
      FROM arrival_first_seen fs
     WHERE fs.first_upload_at >= ?
       AND fs.first_upload_at >= ?
       AND fs.first_upload_at < ?
),
chosen AS (
    SELECT e.arrival_no, e.deadline, s.side_mask, s.state_at,
           ROW_NUMBER() OVER (
               PARTITION BY e.arrival_no
               ORDER BY CASE WHEN s.state_at <= e.deadline THEN 0 ELSE 1 END,
                        s.state_at DESC,
                        s.batch_id DESC
           ) AS rn
      FROM eligible e
      JOIN arrival_side_states s ON s.arrival_no = e.arrival_no
),
at_deadline AS (
    SELECT arrival_no, side_mask
      FROM chosen
     WHERE rn = 1
       AND state_at <= deadline
)
SELECT *
  FROM (
        SELECT COUNT(*) AS total,
               COALESCE(SUM(CASE WHEN side_mask = {MASK_PURCHASE} THEN 1 ELSE 0 END), 0) AS purchase_only,
               COALESCE(SUM(CASE WHEN side_mask = {MASK_BARCODE}  THEN 1 ELSE 0 END), 0) AS barcode_only,
               COALESCE(SUM(CASE WHEN side_mask = {MASK_BOTH}     THEN 1 ELSE 0 END), 0) AS both_blocked
          FROM at_deadline
       ) daily_rollup
 LIMIT {MAX_DAILY_RECORDS}
"""


def deadline_daily(
    conn: sqlite3.Connection,
    start_date: date,
    end_date: date,
    limit_minutes: int,
    now: datetime | None = None,
) -> dict:
    """Daily rollup of deadline-time blocking sides.

    Returns one entry per *mature* UTC day in the inclusive range (days whose
    end is less than two hours ago are skipped and reported separately).
    Read-only: only ``arrival_first_seen`` / ``arrival_side_states`` and
    ``app_meta`` are touched.
    """
    now = now or datetime.now(timezone.utc)
    cutoff_day = _mature_cutoff(now)
    enabled_at = review_enabled_at(conn)

    rows: list[dict] = []
    skipped: list[str] = []
    day = start_date
    while day <= end_date:
        if day > cutoff_day:
            skipped.append(day.isoformat())
            day += timedelta(days=1)
            continue
        day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        next_start = day_start + timedelta(days=1)
        params = (
            limit_minutes,
            enabled_at,
            day_start.strftime(TS_FMT),
            next_start.strftime(TS_FMT),
        )
        result = conn.execute(_DAILY_SQL, params).fetchone()
        rows.append(
            {
                "day": day.isoformat(),
                "total": result["total"],
                "purchase_only": result["purchase_only"],
                "barcode_only": result["barcode_only"],
                "both_blocked": result["both_blocked"],
            }
        )
        day += timedelta(days=1)

    return {
        "rows": rows,
        "skipped_days": skipped,
        "cutoff_day": cutoff_day.isoformat(),
        "enabled_at": enabled_at,
    }
