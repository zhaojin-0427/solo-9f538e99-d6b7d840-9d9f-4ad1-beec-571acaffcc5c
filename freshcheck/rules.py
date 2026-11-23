"""Blocking-rule engine.

An arrival is *released* only when it has zero blocking reasons:

purchase side (keyed by po_line_no)
    * 采购行缺失        no purchase_lines row for the arrival's po_line_no
    * 条码不符          arrival.barcode != purchase_lines.agreed_barcode

barcode side (keyed by arrival.barcode and arrival.arrived_at)
    * 到货时该码无归属  no scan_records row with that barcode whose
                        [starts_at, ends_at) window contains arrived_at
    * 该码对应多种商品  the covering scan records name more than one product

The scan window contains its start and excludes its end.  A scan record that
ends exactly at the arrival instant does not cover it; one that starts exactly
at the arrival instant does.
"""

from __future__ import annotations

import sqlite3

from .csvio import human_reason, human_side
from .sqlutil import chunks, placeholders

PURCHASE_SIDE = "purchase"
BARCODE_SIDE = "barcode"


def covering_products(conn: sqlite3.Connection, barcode: str, at: str) -> list[str]:
    """Distinct products whose scan window covers ``at`` for ``barcode``.

    start inclusive, end exclusive.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT product
          FROM scan_records
         WHERE barcode = ?
           AND starts_at <= ?
           AND ends_at > ?
         ORDER BY product
        """,
        (barcode, at, at),
    ).fetchall()
    return [row["product"] for row in rows]


def evaluate_arrival(conn: sqlite3.Connection, arrival: sqlite3.Row | dict) -> list[dict]:
    """Return every blocking issue for one arrival, both sides included."""
    issues: list[dict] = []
    po_line_no = arrival["po_line_no"]
    barcode = arrival["barcode"]
    arrived_at = arrival["arrived_at"]

    line = conn.execute(
        "SELECT agreed_barcode FROM purchase_lines WHERE po_line_no = ?", (po_line_no,)
    ).fetchone()
    if line is None:
        issues.append(
            _issue(PURCHASE_SIDE, "purchase_line_missing", po_line_no,
                   f"采购行 {po_line_no} 不存在，未约定条码")
        )
    elif line["agreed_barcode"] != barcode:
        issues.append(
            _issue(PURCHASE_SIDE, "barcode_mismatch", po_line_no,
                   f"采购约定条码 {line['agreed_barcode']}，扫码条码 {barcode}")
        )

    products = covering_products(conn, barcode, arrived_at)
    if not products:
        issues.append(
            _issue(BARCODE_SIDE, "barcode_unowned", barcode,
                   f"条码 {barcode} 在到货时刻 {arrived_at} 无有效用码归属")
        )
    elif len(products) > 1:
        issues.append(
            _issue(BARCODE_SIDE, "barcode_ambiguous", barcode,
                   "到货时刻同时归属: " + "、".join(products))
        )
    return issues


def _issue(side: str, reason: str, ref: str, detail: str) -> dict:
    return {
        "side": side,
        "reason": reason,
        "ref": ref,
        "detail": detail,
        "reason_label": human_reason(reason),
        "side_label": human_side(side),
    }


def recheck_arrivals(conn: sqlite3.Connection, arrival_nos: set[str]) -> None:
    """Recompute the issue cache for just the given arrivals.

    Used after a correction: only arrivals whose verdict can possibly have
    changed (those carrying the changed po_line_no / barcode, plus the one
    whose identifier was corrected) are touched.
    """
    if not arrival_nos:
        return
    payload: list[tuple] = []
    for batch in chunks(sorted(arrival_nos)):
        marks = placeholders(len(batch))
        conn.execute(f"DELETE FROM arrival_issues WHERE arrival_no IN ({marks})", tuple(batch))
        rows = conn.execute(
            f"SELECT arrival_no, po_line_no, barcode, arrived_at FROM arrivals "
            f"WHERE arrival_no IN ({marks})",
            tuple(batch),
        ).fetchall()
        for arrival in rows:
            for issue in evaluate_arrival(conn, arrival):
                payload.append(
                    (arrival["arrival_no"], issue["side"], issue["reason"], issue["ref"], issue["detail"])
                )
    if payload:
        conn.executemany(
            "INSERT INTO arrival_issues (arrival_no, side, reason, ref, detail) VALUES (?, ?, ?, ?, ?)",
            payload,
        )
