"""Tests for the deadline (入仓时限) review feature.

Timing vocabulary used here:

* ``arrived_at`` inside the CSV is the business arrival instant and is
  irrelevant to the review clock;
* the review clock is the successful batch's *write* instant
  (``upload_batches.received_at``), which tests control by freezing
  ``freshcheck.service.utc_now``.

The whole file runs on a fixed UTC timeline so the feature switch and the
maturity window are deterministic:

* feature switched on at ``SWITCH_AT``;
* first successful batch writes at ``T0`` (well after the switch).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from freshcheck import review, service
from freshcheck.db import REVIEW_ENABLED_KEY, TS_FMT, connect, init_db, read_conn

SWITCH_AT = "2026-06-01T00:00:00.000000Z"
T0 = datetime(2026, 6, 10, 8, 0, 0, tzinfo=timezone.utc)
T0_STR = T0.strftime(TS_FMT)
DAY = T0.strftime("%Y-%m-%d")
# Far enough ahead that every test day is mature (day end + 2h settled).
FUTURE = T0 + timedelta(days=2)


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "review.db")
    init_db(path)
    # Pin the feature switch to the fixed timeline.
    with connect(path) as conn:
        conn.execute(
            "UPDATE app_meta SET value = ? WHERE key = ?",
            (SWITCH_AT, REVIEW_ENABLED_KEY),
        )
        conn.commit()
    return path


class FrozenClock:
    """Stand-in for utc_now(): holds one value until the test moves it."""

    def __init__(self, value: str):
        self.value = value

    def __call__(self) -> str:
        return self.value


@pytest.fixture()
def clock(monkeypatch):
    frozen = FrozenClock(T0_STR)
    monkeypatch.setattr("freshcheck.service.utc_now", frozen)
    return frozen


def up(db, kind, body, filename="f.csv"):
    return service.upload(kind, filename, body.encode("utf-8"), db)


def iso(dt: datetime) -> str:
    return dt.strftime(TS_FMT)


def query(db, day: str, minutes: int, now: datetime | None = None) -> dict:
    with read_conn(db) as conn:
        d = date.fromisoformat(day)
        result = review.deadline_daily(conn, d, d, minutes, now=now or FUTURE)
    assert len(result["rows"]) == 1
    return result["rows"][0]


def state_rows(db, arrival_no: str) -> list:
    with connect(db) as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT batch_id, state_at, side_mask FROM arrival_side_states "
                "WHERE arrival_no = ? ORDER BY batch_id",
                (arrival_no,),
            ).fetchall()
        ]


def count_state_rows(db) -> int:
    with connect(db) as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM arrival_side_states").fetchone()["n"]


# ---------------------------------------------------------------- basics

def test_initial_masks_classified_at_deadline(db, clock):
    # Four arrivals, one per possible deadline state, uploaded together.
    up(db, "purchase_lines", "编号,约定条码\nP1,6901\nP2,6902\n")
    up(db, "scan_records", "编号,条码,商品,UTC起,UTC止\n"
                           "S1,6902,香蕉,2026-01-01T00:00:00Z,2026-07-01T00:00:00Z\n")
    up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\n"
                       "REL,P1,6901,2026-01-01T00:00:00Z\n"      # line agrees, 6901 unowned
                       "PUR,P9,6902,2026-01-01T00:00:00Z\n"      # missing line, 6902 owned
                       "BOTH,P8,7777,2026-01-01T00:00:00Z\n"     # missing line + unowned
                       "FREE,P2,6902,2026-01-01T00:00:00Z\n")    # clean
    row = query(db, DAY, 30)
    assert row == {
        "day": DAY,
        "total": 4,
        "purchase_only": 1,
        "barcode_only": 1,
        "both_blocked": 1,
    }
    # released = total - three blocked buckets
    assert row["total"] == (
        row["purchase_only"] + row["barcode_only"] + row["both_blocked"] + 1
    )


def test_groups_by_first_successful_upload_day_not_correction_day(db, clock):
    up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\nA1,P1,6901,2026-01-01T00:00:00Z\n")
    # A correction lands on the next UTC day; the arrival must stay grouped
    # under its first successful upload day.
    clock.value = iso(T0 + timedelta(days=1))
    up(db, "purchase_lines", "编号,约定条码\nP1,6901\n", "p.csv")
    with read_conn(db) as conn:
        result = review.deadline_daily(
            conn, date.fromisoformat(DAY), date.fromisoformat(clock.value[:10]), 60
        )
    days = {r["day"]: r for r in result["rows"]}
    assert days[DAY]["total"] == 1
    assert days[clock.value[:10]]["total"] == 0


# ------------------------------------------------------- deadline boundary

def test_correction_exactly_on_deadline_is_included(db, clock):
    up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\nA1,P1,6901,2026-01-01T00:00:00Z\n")
    assert state_rows(db, "A1") == [
        {"batch_id": 1, "state_at": T0_STR, "side_mask": review.MASK_BOTH}
    ]

    # Purchase line arrives exactly at T0 + 30 minutes -> purchase side clears,
    # and the inclusive deadline must count the new state (barcode only).
    clock.value = iso(T0 + timedelta(minutes=30))
    up(db, "purchase_lines", "编号,约定条码\nP1,6901\n", "p.csv")

    row = query(db, DAY, 30)
    assert row["barcode_only"] == 1
    assert row["both_blocked"] == 0


def test_correction_one_second_after_deadline_keeps_old_state(db, clock):
    up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\nA1,P1,6901,2026-01-01T00:00:00Z\n")
    clock.value = iso(T0 + timedelta(minutes=30, seconds=1))
    up(db, "purchase_lines", "编号,约定条码\nP1,6901\n", "p.csv")

    row = query(db, DAY, 30)
    assert row["both_blocked"] == 1  # the late correction does not count
    assert row["barcode_only"] == 0


def test_deadline_state_follows_the_whole_timeline(db, clock):
    up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\nA1,P1,6901,2026-01-01T00:00:00Z\n")
    clock.value = iso(T0 + timedelta(minutes=10))
    up(db, "purchase_lines", "编号,约定条码\nP1,6901\n", "p.csv")          # -> barcode only
    clock.value = iso(T0 + timedelta(minutes=20))
    up(db, "scan_records", "编号,条码,商品,UTC起,UTC止\n"
                           "S1,6901,苹果,2026-01-01T00:00:00Z,2026-07-01T00:00:00Z\n",
        "s.csv")                                                           # -> released

    assert query(db, DAY, 5)["both_blocked"] == 1
    assert query(db, DAY, 15)["barcode_only"] == 1
    released_row = query(db, DAY, 20)  # exactly at the second fix
    assert released_row["total"] == 1
    assert released_row["purchase_only"] == released_row["barcode_only"] == 0
    assert released_row["both_blocked"] == 0


def test_same_instant_batches_last_submission_wins(db, clock):
    up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\nA1,P1,6901,2026-01-01T00:00:00Z\n")
    # Two corrections sharing one write instant (same state_at); batch ids
    # still follow submission order, so the later batch's state must win.
    clock.value = iso(T0 + timedelta(minutes=10))
    up(db, "purchase_lines", "编号,约定条码\nP1,6901\n", "p1.csv")          # -> barcode only
    up(db, "scan_records", "编号,条码,商品,UTC起,UTC止\n"
                           "S1,6901,苹果,2026-01-01T00:00:00Z,2026-07-01T00:00:00Z\n",
        "s1.csv")                                                           # -> released

    rows = state_rows(db, "A1")
    assert [r["state_at"] for r in rows[1:]] == [clock.value, clock.value]
    assert rows[-1]["side_mask"] == review.MASK_NONE
    row = query(db, DAY, 30)
    assert row["total"] == 1 and row["both_blocked"] == 0
    assert row["purchase_only"] == row["barcode_only"] == 0


# ------------------------------------------------------------- write shape

def test_only_new_or_changed_arrivals_get_timeline_rows(db, clock):
    up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\n"
                       "A1,P1,6901,2026-01-01T00:00:00Z\n"
                       "A2,P2,6902,2026-01-01T00:00:00Z\n")
    assert count_state_rows(db) == 2  # one initial row per new arrival

    # An unrelated purchase line touches no arrival -> no timeline writes.
    up(db, "purchase_lines", "编号,约定条码\nP9,9999\n", "unrelated.csv")
    assert count_state_rows(db) == 2

    # This line clears only A1's purchase side; A2's mask is unchanged.
    up(db, "purchase_lines", "编号,约定条码\nP1,6901\n", "p1.csv")
    assert count_state_rows(db) == 3
    assert [r["side_mask"] for r in state_rows(db, "A1")] == [
        review.MASK_BOTH,
        review.MASK_BARCODE,
    ]
    assert len(state_rows(db, "A2")) == 1

    # Re-uploading the same unchanged state writes nothing.
    up(db, "purchase_lines", "编号,约定条码\nP1,6901\n", "p1-again.csv")
    assert count_state_rows(db) == 3


def test_no_per_minute_rows_and_failed_upload_keeps_old_numbers(db, clock):
    up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\nA1,P1,6901,2026-01-01T00:00:00Z\n")
    before = state_rows(db, "A1")
    with pytest.raises(Exception):
        up(db, "purchase_lines", "编号,约定条码\nP1,\n", "bad.csv")
    assert state_rows(db, "A1") == before  # failed batch leaves no trace

    row = query(db, DAY, 120)
    assert row["both_blocked"] == 1


def test_state_accumulates_across_connections_like_a_restart(db, clock):
    up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\nA1,P1,6901,2026-01-01T00:00:00Z\n")
    clock.value = iso(T0 + timedelta(minutes=5))
    up(db, "purchase_lines", "编号,约定条码\nP1,6901\n", "p.csv")

    # A brand-new connection (no in-process caches) still sees the full history.
    fresh = connect(db)
    try:
        result = review.deadline_daily(fresh, date.fromisoformat(DAY), date.fromisoformat(DAY), 60)
    finally:
        fresh.close()
    assert result["rows"][0]["barcode_only"] == 1


# -------------------------------------------------- enablement / migration

def test_arrivals_before_enablement_are_excluded(db, clock):
    old = "2026-05-01T08:00:00.000000Z"  # strictly before SWITCH_AT
    with connect(db) as conn:
        # Simulate an arrival that existed before the feature switch.
        conn.execute(
            "INSERT INTO upload_batches (id, kind, filename, row_count, received_at, status) "
            "VALUES (99, 'arrivals', 'old.csv', 1, ?, 'committed')",
            (old,),
        )
        conn.execute(
            "INSERT INTO arrivals (arrival_no, po_line_no, barcode, arrived_at, batch_id, updated_at) "
            "VALUES ('OLD', 'P1', '6901', ?, 99, ?)",
            (old, old),
        )
        conn.execute(
            "INSERT INTO arrival_first_seen (arrival_no, first_upload_at) VALUES ('OLD', ?)",
            (old,),
        )
        conn.execute(
            "INSERT INTO arrival_side_states (arrival_no, batch_id, state_at, side_mask) "
            "VALUES ('OLD', 99, ?, 3)",
            (old,),
        )
        conn.commit()

    up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\nNEW,P1,6901,2026-01-01T00:00:00Z\n")
    with read_conn(db) as conn:
        result = review.deadline_daily(
            conn, date.fromisoformat(old[:10]), date.fromisoformat(DAY), 60
        )
    by_day = {r["day"]: r for r in result["rows"]}
    assert by_day[DAY]["total"] == 1            # NEW counts
    assert by_day[old[:10]]["total"] == 0       # OLD is invisible to the review


def test_migration_backfills_first_seen_before_switch(tmp_path):
    # Build a database in the pre-feature shape: no app_meta / summary tables.
    path = str(tmp_path / "migrated.db")
    conn = connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE upload_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, filename TEXT,
                row_count INTEGER, received_at TEXT, status TEXT, note TEXT
            );
            CREATE TABLE arrivals (
                arrival_no TEXT PRIMARY KEY, po_line_no TEXT, barcode TEXT,
                arrived_at TEXT, batch_id INTEGER, updated_at TEXT
            );
            """
        )
        old = "2026-01-01T08:00:00.000000Z"
        conn.execute(
            "INSERT INTO upload_batches (id, kind, filename, row_count, received_at, status) "
            "VALUES (7, 'arrivals', 'old.csv', 1, ?, 'committed')",
            (old,),
        )
        conn.execute(
            "INSERT INTO arrivals (arrival_no, po_line_no, barcode, arrived_at, batch_id, updated_at) "
            "VALUES ('OLD', 'P1', '6901', ?, 7, ?)",
            (old, old),
        )
        conn.commit()
    finally:
        conn.close()

    # First run of the new code creates the new tables and backfills first-seen.
    init_db(path)
    with connect(path) as conn:
        enabled_at = conn.execute(
            "SELECT value FROM app_meta WHERE key = 'review_enabled_at'"
        ).fetchone()["value"]
        seen = conn.execute(
            "SELECT first_upload_at FROM arrival_first_seen WHERE arrival_no = 'OLD'"
        ).fetchone()["first_upload_at"]
        assert seen < enabled_at
        result = review.deadline_daily(
            conn, date(2026, 1, 1), date(2026, 1, 2), 60,
            now=datetime(2026, 1, 5, tzinfo=timezone.utc),
        )
    assert all(r["total"] == 0 for r in result["rows"])


# ------------------------------------------------------- maturity / params

def test_day_is_skipped_until_two_hours_after_day_end(db, clock):
    up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\nA1,P1,6901,2026-01-01T00:00:00Z\n")
    day_start = datetime(T0.year, T0.month, T0.day, tzinfo=timezone.utc)

    with read_conn(db) as conn:
        almost = review.deadline_daily(
            conn, date.fromisoformat(DAY), date.fromisoformat(DAY), 30,
            now=day_start + timedelta(hours=25, minutes=59, seconds=59),
        )
        exact = review.deadline_daily(
            conn, date.fromisoformat(DAY), date.fromisoformat(DAY), 30,
            now=day_start + timedelta(hours=26),
        )
    assert almost["rows"] == [] and almost["skipped_days"] == [DAY]
    assert exact["rows"][0]["total"] == 1 and exact["skipped_days"] == []


def test_param_validation():
    with pytest.raises(review.ReviewParamError):
        review.parse_params("2026-01-31", "2026-01-01", "30")          # end before start
    with pytest.raises(review.ReviewParamError):
        review.parse_params("2026-01-01", "2026-02-01", "30")         # 32 days
    with pytest.raises(review.ReviewParamError):
        review.parse_params("2026-01-01", "2026-01-02", "0")          # below range
    with pytest.raises(review.ReviewParamError):
        review.parse_params("2026-01-01", "2026-01-02", "121")        # above range
    with pytest.raises(review.ReviewParamError):
        review.parse_params("2026-01-01", "2026-01-02", "30.5")       # not integer
    with pytest.raises(review.ReviewParamError):
        review.parse_params("01/01/2026", "2026-01-02", "30")         # bad date format
    start, end, minutes = review.parse_params("2026-01-01", "2026-01-31", "120")
    assert (start, end, minutes) == (date(2026, 1, 1), date(2026, 1, 31), 120)


def test_review_query_reads_only_summary_tables(db, clock):
    up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\nA1,P1,6901,2026-01-01T00:00:00Z\n")
    before = query(db, DAY, 30)

    # Wiping the base/derived business tables must not move review numbers:
    # the read path is allowed to touch the summary tables only.
    with connect(db) as conn:
        conn.execute("DELETE FROM arrivals")
        conn.execute("DELETE FROM arrival_issues")
        conn.execute("DELETE FROM purchase_lines")
        conn.execute("DELETE FROM scan_records")
        conn.commit()
    assert query(db, DAY, 30) == before


def test_daily_fetch_contract_constants():
    assert review.MAX_DAILY_RECORDS == 24
    assert review.MAX_RANGE_DAYS == 31
    assert review.MIN_LIMIT_MINUTES == 1
    assert review.MAX_LIMIT_MINUTES == 120
    # The cap is applied to the rows the statement returns, not to the
    # per-arrival work that stays inside CTEs; totals must never be truncated.
    assert review._DAILY_SQL.count("LIMIT 24") == 1
    assert "FROM at_deadline" not in review._DAILY_SQL.split("daily_rollup")[-1]


def test_totals_stay_accurate_beyond_twenty_four_arrivals(db, clock):
    body = ["编号,采购行号,扫码条码,UTC时刻"]
    for i in range(60):  # more than MAX_DAILY_RECORDS arrivals on one day
        body.append(f"A{i:03d},P{i % 4},B{i % 3},2026-01-01T00:00:00Z")
    up(db, "arrivals", "\n".join(body) + "\n", "many.csv")

    # Exactly one sparse timeline row per new arrival — no per-minute writes.
    assert count_state_rows(db) == 60

    row = query(db, DAY, 30)
    assert row["total"] == 60  # not clipped at 24
    assert (
        row["purchase_only"] + row["barcode_only"] + row["both_blocked"]
    ) == 60  # every arrival starts blocked on at least one side

    # The read statement for the day returns a single counter row, i.e. far
    # below the 24-record/day fetch budget.
    with read_conn(db) as conn:
        cur = conn.execute(
            review._DAILY_SQL,
            (
                30,
                SWITCH_AT,
                T0.strftime(TS_FMT),
                (T0 + timedelta(days=1)).strftime(TS_FMT),
            ),
        )
        returned = cur.fetchall()
    assert len(returned) == 1
    assert returned[0]["total"] == 60
