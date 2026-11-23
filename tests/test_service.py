from __future__ import annotations

import pytest

from freshcheck import service
from freshcheck.db import connect, init_db


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "test.db")
    init_db(path)
    return path


def up(db, kind, body, filename="f.csv"):
    return service.upload(kind, filename, body.encode("utf-8"), db)


def arrivals(db):
    with connect(db) as conn:
        return {row["arrival_no"]: row["issue_count"] for row in service.list_arrivals(conn)}


def worklists(db):
    with connect(db) as conn:
        return service.worklist(conn)


def detail(db, arrival_no):
    with connect(db) as conn:
        return service.arrival_detail(conn, arrival_no)


def seed_basic(db):
    up(db, "purchase_lines", "编号,约定条码\nP1,6901\nP2,6902\n")
    up(db, "scan_records", "编号,条码,商品,UTC起,UTC止\nS1,6901,苹果,2026-01-01T00:00:00Z,2026-01-02T00:00:00Z\n")
    up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\n"
                       "A1,P1,6901,2026-01-01T12:00:00Z\n"
                       "A2,P2,6902,2026-01-01T12:00:00Z\n")


def test_clean_arrival_released(db):
    seed_basic(db)
    assert arrivals(db) == {"A1": 0, "A2": 1}


def test_english_headers_and_utc_offset_normalised(db):
    up(db, "purchase_lines", "po_line_no,agreed_barcode\nP1,6901\n")
    up(db, "scan_records", "record_no,barcode,product,starts_at,ends_at\n"
                           "S1,6901,Apple,2026-01-01T08:00:00+08:00,2026-01-02T08:00:00+08:00\n")
    result = up(db, "arrivals", "arrival_no,po_line_no,barcode,arrived_at\n"
                                "A1,P1,6901,2026-01-01T12:00:00+08:00\n")
    assert result["blocked_arrivals"] == 0
    with connect(db) as conn:
        row = conn.execute("SELECT arrived_at FROM arrivals").fetchone()
    assert row["arrived_at"] == "2026-01-01T04:00:00.000000Z"


def test_all_four_reasons_and_both_sides(db):
    # no purchase line; barcode has no scan window
    up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\nA1,P1,7777,2026-01-01T12:00:00Z\n")
    d = detail(db, "A1")
    assert d["released"] is False
    assert {issue["reason"] for issue in d["issues"]} == {
        "purchase_line_missing",
        "barcode_unowned",
    }
    w = worklists(db)
    assert w["purchase_total"] == 1
    assert w["purchase"][0]["ref"] == "P1"
    assert w["barcode_total"] == 1
    assert w["barcode"][0]["ref"] == "7777"

    # purchase line exists but barcode differs; barcode has one product
    up(db, "purchase_lines", "编号,约定条码\nP1,6901\n", "p.csv")
    up(db, "scan_records", "编号,条码,商品,UTC起,UTC止\n"
                           "S1,6901,苹果,2026-01-01T00:00:00Z,2026-01-02T00:00:00Z\n", "s.csv")
    d = detail(db, "A1")
    assert {issue["reason"] for issue in d["issues"]} == {"barcode_mismatch", "barcode_unowned"}

    # make the scanned barcode owned by a single product -> only mismatch
    up(db, "scan_records", "编号,条码,商品,UTC起,UTC止\n"
                           "S2,7777,香蕉,2026-01-01T00:00:00Z,2026-01-02T00:00:00Z\n", "s2.csv")
    d = detail(db, "A1")
    assert {issue["reason"] for issue in d["issues"]} == {"barcode_mismatch"}

    # a second product under the same barcode during the same window -> ambiguous
    up(db, "scan_records", "编号,条码,商品,UTC起,UTC止\n"
                           "S3,7777,橙子,2026-01-01T06:00:00Z,2026-01-01T18:00:00Z\n", "s3.csv")
    d = detail(db, "A1")
    assert {issue["reason"] for issue in d["issues"]} == {"barcode_mismatch", "barcode_ambiguous"}


def test_window_start_inclusive_end_exclusive(db):
    up(db, "purchase_lines", "编号,约定条码\nP1,6901\n")
    up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\n"
                       "A_START,P1,6901,2026-01-02T00:00:00Z\n"
                       "A_END,P1,6901,2026-01-02T00:00:01Z\n")
    up(db, "scan_records", "编号,条码,商品,UTC起,UTC止\n"
                           "S1,6901,苹果,2026-01-01T00:00:00Z,2026-01-02T00:00:00Z\n")
    got = arrivals(db)
    # A_START is exactly at end -> not covered; A_END is after end -> not covered
    assert got["A_START"] == 1 and got["A_END"] == 1

    up(db, "scan_records", "编号,条码,商品,UTC起,UTC止\n"
                           "S2,6901,苹果,2026-01-02T00:00:00Z,2026-01-03T00:00:00Z\n", "s2.csv")
    got = arrivals(db)
    # A_START is exactly at start -> covered; A_END falls inside the new window
    assert got["A_START"] == 0
    assert got["A_END"] == 0
    # First half already showed that ending exactly at the arrival instant does
    # not cover it (start inclusive, end exclusive).


def test_latest_upload_wins_for_same_number(db):
    seed_basic(db)
    assert arrivals(db)["A1"] == 0
    # same arrival number corrected to a wrong barcode, same upload wins
    up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\nA1,P1,9999,2026-01-01T12:00:00Z\n", "a2.csv")
    assert arrivals(db)["A1"] == 2  # mismatch + unowned
    # corrected back
    up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\nA1,P1,6901,2026-01-01T12:00:00Z\n", "a3.csv")
    assert arrivals(db)["A1"] == 0


def test_duplicate_number_inside_one_file_last_row_wins(db):
    up(db, "purchase_lines", "编号,约定条码\nP1,1111\nP1,6901\n")
    up(db, "scan_records", "编号,条码,商品,UTC起,UTC止\n"
                           "S1,6901,苹果,2026-01-01T00:00:00Z,2026-01-02T00:00:00Z\n")
    up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\nA1,P1,6901,2026-01-01T12:00:00Z\n")
    assert arrivals(db)["A1"] == 0


def test_supplement_releases_and_updates_both_lists(db):
    up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\nA1,P1,6901,2026-01-01T12:00:00Z\n")
    assert worklists(db)["purchase_total"] == 1
    assert worklists(db)["barcode_total"] == 1

    up(db, "purchase_lines", "编号,约定条码\nP1,6901\n", "p.csv")
    w = worklists(db)
    assert w["purchase_total"] == 0  # purchase side cleared
    assert w["barcode_total"] == 1   # barcode side still blocked
    assert detail(db, "A1")["released"] is False

    up(db, "scan_records", "编号,条码,商品,UTC起,UTC止\n"
                           "S1,6901,苹果,2026-01-01T00:00:00Z,2026-01-02T00:00:00Z\n", "s.csv")
    assert worklists(db)["barcode_total"] == 0
    assert detail(db, "A1")["released"] is True


def test_correction_can_reblock_released_arrival(db):
    seed_basic(db)
    assert detail(db, "A1")["released"] is True

    # shrink the scan window so it no longer covers the arrival
    up(db, "scan_records", "编号,条码,商品,UTC起,UTC止\n"
                           "S1,6901,苹果,2026-01-01T13:00:00Z,2026-01-02T00:00:00Z\n", "s2.csv")
    d = detail(db, "A1")
    assert d["released"] is False
    assert {issue["reason"] for issue in d["issues"]} == {"barcode_unowned"}
    assert worklists(db)["barcode"][0]["ref"] == "6901"

    # correcting the purchase line's agreed barcode also re-blocks
    up(db, "purchase_lines", "编号,约定条码\nP1,6902\n", "p2.csv")
    d = detail(db, "A1")
    assert {issue["reason"] for issue in d["issues"]} == {"barcode_mismatch", "barcode_unowned"}


def test_scan_correction_moving_barcode_rechecks_old_and_new(db):
    up(db, "purchase_lines", "编号,约定条码\nP1,6901\nP2,6902\n")
    up(db, "scan_records", "编号,条码,商品,UTC起,UTC止\n"
                           "S1,6901,苹果,2026-01-01T00:00:00Z,2026-01-02T00:00:00Z\n"
                           "S2,6902,香蕉,2026-01-01T00:00:00Z,2026-01-02T00:00:00Z\n", "s.csv")
    up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\n"
                       "A1,P1,6901,2026-01-01T12:00:00Z\nA2,P2,6902,2026-01-01T12:00:00Z\n", "a.csv")
    assert arrivals(db) == {"A1": 0, "A2": 0}

    # move S1's barcode from 6901 to 6902: A1 loses coverage, A2 gains ambiguity
    up(db, "scan_records", "编号,条码,商品,UTC起,UTC止\n"
                           "S1,6902,苹果,2026-01-01T00:00:00Z,2026-01-02T00:00:00Z\n", "s2.csv")
    d1, d2 = detail(db, "A1"), detail(db, "A2")
    assert {i["reason"] for i in d1["issues"]} == {"barcode_unowned"}
    assert {i["reason"] for i in d2["issues"]} == {"barcode_ambiguous"}


def test_arrival_correction_moves_between_lines(db):
    up(db, "purchase_lines", "编号,约定条码\nP1,6901\n")
    up(db, "scan_records", "编号,条码,商品,UTC起,UTC止\n"
                           "S1,6901,苹果,2026-01-01T00:00:00Z,2026-01-02T00:00:00Z\n")
    up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\nA1,P1,6901,2026-01-01T12:00:00Z\n")
    assert arrivals(db)["A1"] == 0
    # correct the line to one that does not exist -> missing; worklist moves ref
    up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\nA1,P9,6901,2026-01-01T12:00:00Z\n", "a2.csv")
    d = detail(db, "A1")
    assert {i["reason"] for i in d["issues"]} == {"purchase_line_missing"}
    w = worklists(db)
    assert [bucket["ref"] for bucket in w["purchase"]] == ["P9"]
    assert w["purchase_total"] == 1


def test_failed_upload_keeps_previous_results(db):
    seed_basic(db)
    before = (arrivals(db), worklists(db))

    with pytest.raises(Exception):
        up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\nA1,P1,6901,not-a-time\n", "bad.csv")
    assert (arrivals(db), worklists(db)) == before
    assert detail(db, "A1")["released"] is True

    with pytest.raises(Exception):
        up(db, "purchase_lines", "编号,约定条码\nP1,\n", "bad2.csv")
    assert (arrivals(db), worklists(db)) == before

    # the failed attempt is still visible in the audit trail
    with connect(db) as conn:
        last = conn.execute(
            "SELECT status, note FROM upload_batches ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert last["status"] == "failed"
    assert "约定条码" in last["note"]


def test_malformed_time_range_rejected(db):
    with pytest.raises(Exception):
        up(db, "scan_records", "编号,条码,商品,UTC起,UTC止\n"
                               "S1,6901,苹果,2026-01-02T00:00:00Z,2026-01-01T00:00:00Z\n")


def test_mid_transaction_failure_rolls_back_base_and_derived_tables(db, monkeypatch):
    """Base tables and the worklist snapshot must commit or roll back together."""
    seed_basic(db)
    before = (arrivals(db), worklists(db))

    from freshcheck import rules

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(rules, "recheck_arrivals", boom)
    with pytest.raises(RuntimeError):
        up(db, "purchase_lines", "编号,约定条码\nP1,1111\n", "mutate.csv")
    monkeypatch.undo()

    assert (arrivals(db), worklists(db)) == before
    with connect(db) as conn:
        agreed = conn.execute(
            "SELECT agreed_barcode FROM purchase_lines WHERE po_line_no = 'P1'"
        ).fetchone()["agreed_barcode"]
    assert agreed == "6901"
    assert detail(db, "A1")["released"] is True


def test_one_arrival_in_both_worklists(db):
    up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\nA1,P1,6901,2026-01-01T12:00:00Z\n")
    w = worklists(db)
    assert w["purchase_total"] == 1 and w["barcode_total"] == 1
    assert w["purchase"][0]["rows"][0]["arrival_no"] == "A1"
    assert w["barcode"][0]["rows"][0]["arrival_no"] == "A1"


def test_gb18030_csv_accepted(db):
    body = "编号,约定条码\nP1,6901\n".encode("gb18030")
    service.upload("purchase_lines", "gb.csv", body, db)
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM purchase_lines").fetchone()["n"] == 1


def test_sub_second_boundary_is_respected(db):
    up(db, "purchase_lines", "编号,约定条码\nP1,6901\n")
    up(db, "scan_records", "编号,条码,商品,UTC起,UTC止\n"
                           "S1,6901,苹果,2026-01-01T00:00:00Z,2026-01-01T12:00:00.500000Z\n")
    up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\n"
                       "IN,P1,6901,2026-01-01T12:00:00.499999Z\n"
                       "OUT,P1,6901,2026-01-01T12:00:00.500000Z\n")
    got = arrivals(db)
    assert got["IN"] == 0   # just before the exclusive end
    assert got["OUT"] == 1  # exactly at the exclusive end


def test_changing_only_product_rechecks_barcode_side(db):
    up(db, "purchase_lines", "编号,约定条码\nP1,6901\n")
    up(db, "scan_records", "编号,条码,商品,UTC起,UTC止\n"
                           "S1,6901,苹果,2026-01-01T00:00:00Z,2026-01-02T00:00:00Z\n")
    up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\nA1,P1,6901,2026-01-01T12:00:00Z\n")
    assert detail(db, "A1")["released"] is True
    # same record number, same barcode, same window: only product changes.
    # Nothing about the arrival's ownership changes, so it stays released.
    up(db, "scan_records", "编号,条码,商品,UTC起,UTC止\n"
                           "S1,6901,红富士苹果,2026-01-01T00:00:00Z,2026-01-02T00:00:00Z\n", "s2.csv")
    assert detail(db, "A1")["released"] is True
    # now split ownership with a second record for the same barcode
    up(db, "scan_records", "编号,条码,商品,UTC起,UTC止\n"
                           "S2,6901,青苹果,2026-01-01T00:00:00Z,2026-01-02T00:00:00Z\n", "s3.csv")
    d = detail(db, "A1")
    assert {i["reason"] for i in d["issues"]} == {"barcode_ambiguous"}
    assert "红富士苹果" in d["issues"][0]["detail"] and "青苹果" in d["issues"][0]["detail"]


def test_whole_batch_rejected_if_any_row_invalid(db):
    seed_basic(db)
    before = (arrivals(db), worklists(db))
    body = (
        "编号,采购行号,扫码条码,UTC时刻\n"
        "A1,P1,6901,2026-01-01T12:00:00Z\n"   # would re-block if applied alone
        "A2,P2,6902,2026-01-01T12:00:00Z\n"
        "A3,P1,6901,bad-time\n"                # invalid row
    )
    with pytest.raises(Exception):
        up(db, "arrivals", body, "mixed.csv")
    assert (arrivals(db), worklists(db)) == before
    with connect(db) as conn:
        # no third arrival leaked in
        assert conn.execute("SELECT COUNT(*) AS n FROM arrivals").fetchone()["n"] == 2


def test_purchase_barcode_correction_flips_both_ways(db):
    seed_basic(db)
    # A2 is blocked on barcode until 6902 is owned; its purchase line is fine.
    up(db, "scan_records", "编号,条码,商品,UTC起,UTC止\n"
                           "S2,6902,香蕉,2026-01-01T00:00:00Z,2026-01-02T00:00:00Z\n", "s2.csv")
    assert arrivals(db)["A2"] == 0
    # fixing the purchase line to a different barcode re-blocks A2
    up(db, "purchase_lines", "编号,约定条码\nP2,6903\n", "p2.csv")
    assert arrivals(db)["A2"] == 1
    assert worklists(db)["purchase"][0]["ref"] == "P2"
    # and restoring releases it again
    up(db, "purchase_lines", "编号,约定条码\nP2,6902\n", "p3.csv")
    assert arrivals(db)["A2"] == 0


def test_worklist_counts_match_details(db):
    up(db, "purchase_lines", "编号,约定条码\nP1,6901\nP2,6902\n")
    up(db, "scan_records", "编号,条码,商品,UTC起,UTC止\n"
                           "S1,6901,苹果,2026-01-01T00:00:00Z,2026-01-02T00:00:00Z\n")
    up(db, "arrivals", "编号,采购行号,扫码条码,UTC时刻\n"
                       "A1,P1,6901,2026-01-01T12:00:00Z\n"   # OK
                       "A2,P2,6902,2026-01-01T12:00:00Z\n"   # unowned
                       "A3,P2,6902,2026-01-01T13:00:00Z\n"   # unowned, same barcode
                       "A4,P9,6901,2026-01-01T12:00:00Z\n")  # missing line
    w = worklists(db)
    for side in ("purchase", "barcode"):
        total = sum(bucket["blocked"] for bucket in w[side])
        assert total == w[f"{side}_total"]
        for bucket in w[side]:
            assert bucket["blocked"] == len(bucket["rows"])
    assert w["barcode_total"] == 2  # A2, A3
    assert w["purchase_total"] == 1  # A4


def test_detail_lists_covering_scan_records(db):
    seed_basic(db)
    d = detail(db, "A1")
    assert d["released"] is True
    assert [scan["record_no"] for scan in d["covering_scans"]] == ["S1"]
    assert d["purchase_line"]["agreed_barcode"] == "6901"
