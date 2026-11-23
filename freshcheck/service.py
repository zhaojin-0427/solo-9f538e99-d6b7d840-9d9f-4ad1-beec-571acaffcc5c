"""Upload / correction service.

Every write path follows the same contract:

1. parse and validate the whole file up front;
2. open one SQLite transaction;
3. upsert by natural key ("same kind + same number: this upload wins");
4. recompute the verdicts of exactly the affected arrivals;
5. write the audit row and commit.

If anything raises, the transaction rolls back and the previous base tables,
worklists and release flags remain exactly as they were.
"""

from __future__ import annotations

import sqlite3

from . import rules
from .csvio import KINDS, parse_csv
from .db import utc_now
from .sqlutil import chunks, placeholders

UPSERT = {
    "arrivals": (
        """
        INSERT INTO arrivals (arrival_no, po_line_no, barcode, arrived_at, batch_id, updated_at)
        VALUES (:arrival_no, :po_line_no, :barcode, :arrived_at, :batch_id, :updated_at)
        ON CONFLICT(arrival_no) DO UPDATE SET
            po_line_no = excluded.po_line_no,
            barcode    = excluded.barcode,
            arrived_at = excluded.arrived_at,
            batch_id   = excluded.batch_id,
            updated_at = excluded.updated_at
        """,
        ["arrival_no", "po_line_no", "barcode", "arrived_at"],
    ),
    "purchase_lines": (
        """
        INSERT INTO purchase_lines (po_line_no, agreed_barcode, batch_id, updated_at)
        VALUES (:po_line_no, :agreed_barcode, :batch_id, :updated_at)
        ON CONFLICT(po_line_no) DO UPDATE SET
            agreed_barcode = excluded.agreed_barcode,
            batch_id       = excluded.batch_id,
            updated_at     = excluded.updated_at
        """,
        ["po_line_no", "agreed_barcode"],
    ),
    "scan_records": (
        """
        INSERT INTO scan_records (record_no, barcode, product, starts_at, ends_at, batch_id, updated_at)
        VALUES (:record_no, :barcode, :product, :starts_at, :ends_at, :batch_id, :updated_at)
        ON CONFLICT(record_no) DO UPDATE SET
            barcode    = excluded.barcode,
            product    = excluded.product,
            starts_at  = excluded.starts_at,
            ends_at    = excluded.ends_at,
            batch_id   = excluded.batch_id,
            updated_at = excluded.updated_at
        """,
        ["record_no", "barcode", "product", "starts_at", "ends_at"],
    ),
}


def upload(kind: str, filename: str, raw: bytes, db_path: str | None = None) -> dict:
    from .db import transaction

    display_name = filename or f"{kind}.csv"
    rows: list[dict] = []
    now = utc_now()
    batch_id: int | None = None

    try:
        rows = parse_csv(kind, raw)  # validates everything before the transaction
        with transaction(db_path) as conn:
            previous = _existing_keys(conn, kind, rows)
            affected = _affected_arrivals(conn, kind, rows, previous)
            cursor = conn.execute(
                "INSERT INTO upload_batches (kind, filename, row_count, received_at, status) "
                "VALUES (?, ?, ?, ?, 'committed')",
                (kind, display_name, len(rows), now),
            )
            batch_id = cursor.lastrowid
            sql, fields = UPSERT[kind]
            payload = []
            for row in rows:
                record = {field: row[field] for field in fields}
                record["batch_id"] = batch_id
                record["updated_at"] = now
                payload.append(record)
            conn.executemany(sql, payload)
            rules.recheck_arrivals(conn, affected)
            stats = _summary(conn)
    except Exception as exc:
        # The data transaction rolled back, so base tables, worklists and
        # release flags are untouched.  Record the failed attempt on a fresh
        # connection purely for the audit list.
        _record_failed(kind, display_name, len(rows) if rows else 0, now, str(exc), db_path)
        raise

    return {"kind": kind, "label": KINDS[kind]["label"], "rows": len(rows), "batch_id": batch_id, **stats}


def _record_failed(kind, filename, row_count, received_at, note, db_path) -> None:
    from .db import connect

    try:
        with connect(db_path) as conn:
            conn.execute(
                "INSERT INTO upload_batches (kind, filename, row_count, received_at, status, note) "
                "VALUES (?, ?, ?, ?, 'failed', ?)",
                (kind, filename, row_count, received_at, note[:500]),
            )
    except Exception:
        # Never let audit bookkeeping mask the original upload error.
        pass


def _existing_keys(conn: sqlite3.Connection, kind: str, rows: list[dict]) -> dict[str, dict]:
    key_field = {
        "arrivals": "arrival_no",
        "purchase_lines": "po_line_no",
        "scan_records": "record_no",
    }[kind]
    keys = sorted({row[key_field] for row in rows})
    if not keys:
        return {}
    columns = {
        "arrivals": "arrival_no, po_line_no, barcode, arrived_at",
        "purchase_lines": "po_line_no, agreed_barcode",
        "scan_records": "record_no, barcode, product, starts_at, ends_at",
    }[kind]
    found_rows: list[sqlite3.Row] = []
    for batch in chunks(keys):
        found_rows.extend(
            conn.execute(
                f"SELECT {columns} FROM {kind} WHERE {key_field} IN ({placeholders(len(batch))})",
                tuple(batch),
            ).fetchall()
        )
    return {row[key_field]: dict(row) for row in found_rows}


def _affected_arrivals(
    conn: sqlite3.Connection, kind: str, rows: list[dict], previous: dict[str, dict]
) -> set[str]:
    """Arrivals whose verdict may change because of this upload.

    * arrivals: the identifiers in the file, plus identifiers they used to
      have (a corrected arrival may have moved to another barcode/line);
    * purchase_lines: all arrivals using the changed po_line_no, before and
      after the change;
    * scan_records: all arrivals using the changed barcode, before and after.
    """
    affected: set[str] = set()

    def add_arrivals_by(field: str, values: set[str]) -> None:
        values = {v for v in values if v}
        if not values:
            return
        for batch in chunks(sorted(values)):
            found = conn.execute(
                f"SELECT arrival_no FROM arrivals WHERE {field} IN ({placeholders(len(batch))})",
                tuple(batch),
            ).fetchall()
            affected.update(row["arrival_no"] for row in found)

    if kind == "arrivals":
        affected.update(row["arrival_no"] for row in rows)
        # Only the corrected arrivals themselves need re-checking: their old
        # po_line_no/barcode associations are no longer used by them, and no
        # other arrival's verdict depends on which arrival used to point where.
        return affected

    if kind == "purchase_lines":
        lines = {row["po_line_no"] for row in rows}
        lines |= {old["po_line_no"] for old in previous.values()}
        add_arrivals_by("po_line_no", lines)
    elif kind == "scan_records":
        barcodes = {row["barcode"] for row in rows}
        barcodes |= {old["barcode"] for old in previous.values()}
        add_arrivals_by("barcode", barcodes)

    # Deliberately scoped to the changed line/barcode (old ∪ new), not a
    # full-table rebuild: only arrivals whose verdict could move are touched.
    return affected


def _summary(conn: sqlite3.Connection) -> dict:
    blocked = conn.execute(
        "SELECT COUNT(DISTINCT arrival_no) AS n FROM arrival_issues"
    ).fetchone()["n"]
    total = conn.execute("SELECT COUNT(*) AS n FROM arrivals").fetchone()["n"]
    return {"total_arrivals": total, "blocked_arrivals": blocked, "released_arrivals": total - blocked}


def list_arrivals(conn: sqlite3.Connection, query: str = "") -> list[sqlite3.Row]:
    sql = """
        SELECT a.arrival_no, a.po_line_no, a.barcode, a.arrived_at,
               (SELECT COUNT(*) FROM arrival_issues i WHERE i.arrival_no = a.arrival_no) AS issue_count
          FROM arrivals a
    """
    params: tuple = ()
    if query:
        sql += " WHERE a.arrival_no LIKE ? OR a.po_line_no LIKE ? OR a.barcode LIKE ?"
        like = f"%{query}%"
        params = (like, like, like)
    sql += " ORDER BY a.arrival_no"
    return conn.execute(sql, params).fetchall()


def arrival_detail(conn: sqlite3.Connection, arrival_no: str) -> dict | None:
    arrival = conn.execute(
        "SELECT * FROM arrivals WHERE arrival_no = ?", (arrival_no,)
    ).fetchone()
    if arrival is None:
        return None
    issues = conn.execute(
        "SELECT side, reason, ref, detail FROM arrival_issues WHERE arrival_no = ? ORDER BY side, reason, ref",
        (arrival_no,),
    ).fetchall()
    line = conn.execute(
        "SELECT agreed_barcode FROM purchase_lines WHERE po_line_no = ?", (arrival["po_line_no"],)
    ).fetchone()
    covering = conn.execute(
        """SELECT record_no, product, starts_at, ends_at FROM scan_records
            WHERE barcode = ? AND starts_at <= ? AND ends_at > ?
            ORDER BY starts_at, record_no""",
        (arrival["barcode"], arrival["arrived_at"], arrival["arrived_at"]),
    ).fetchall()
    from .csvio import human_reason, human_side

    return {
        "arrival": dict(arrival),
        "issues": [
            {**dict(issue), "reason_label": human_reason(issue["reason"]), "side_label": human_side(issue["side"])}
            for issue in issues
        ],
        "released": len(issues) == 0,
        "purchase_line": dict(line) if line else None,
        "covering_scans": [dict(row) for row in covering],
    }


def worklist(conn: sqlite3.Connection) -> dict:
    """Two worklists keyed by (reason, po_line_no) and (reason, barcode).

    One blocked arrival can appear in both lists.  Within one side the two
    reasons are mutually exclusive (missing line vs. mismatch, unowned vs.
    ambiguous), so each row is one reason for one key.  ``blocked`` is the
    number of distinct blocked arrivals in that row and is always the length
    of its own detail list.
    """
    from .csvio import human_reason

    result: dict[str, list[dict]] = {"purchase": [], "barcode": []}
    for side in ("purchase", "barcode"):
        rows = conn.execute(
            """
            SELECT i.ref AS ref, i.reason, i.detail, i.arrival_no,
                   a.arrived_at, a.barcode, a.po_line_no
              FROM arrival_issues i
              JOIN arrivals a ON a.arrival_no = i.arrival_no
             WHERE i.side = ?
             ORDER BY i.ref, i.reason, i.arrival_no
            """,
            (side,),
        ).fetchall()
        buckets: dict[tuple[str, str], dict] = {}
        for row in rows:
            key = (row["reason"], row["ref"])
            bucket = buckets.setdefault(
                key,
                {
                    "ref": row["ref"],
                    "reason": row["reason"],
                    "reason_label": human_reason(row["reason"]),
                    "blocked": 0,
                    "rows": [],
                },
            )
            bucket["blocked"] += 1
            bucket["rows"].append(
                {
                    "arrival_no": row["arrival_no"],
                    "po_line_no": row["po_line_no"],
                    "barcode": row["barcode"],
                    "arrived_at": row["arrived_at"],
                    "detail": row["detail"],
                }
            )
        result[side] = sorted(buckets.values(), key=lambda item: (item["ref"], item["reason"]))
        result[f"{side}_total"] = sum(bucket["blocked"] for bucket in result[side])
    return result


def dashboard(conn: sqlite3.Connection) -> dict:
    counts = conn.execute(
        """
        SELECT i.reason, COUNT(DISTINCT i.arrival_no) AS n
          FROM arrival_issues i GROUP BY i.reason ORDER BY i.reason
        """
    ).fetchall()
    from .csvio import human_reason

    summary = _summary(conn)
    return {
        **summary,
        "by_reason": [
            {"reason": row["reason"], "label": human_reason(row["reason"]), "count": row["n"]}
            for row in counts
        ],
        "batches": [
            dict(row)
            for row in conn.execute(
                "SELECT id, kind, filename, row_count, received_at, status, note FROM upload_batches ORDER BY id DESC LIMIT 20"
            ).fetchall()
        ],
    }
