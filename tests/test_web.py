from __future__ import annotations

import io

import pytest

from freshcheck.app import app as flask_app


@pytest.fixture()
def client(tmp_path):
    flask_app.config.update(TESTING=True, DB_PATH=str(tmp_path / "web.db"), SECRET_KEY="test")
    with flask_app.test_client() as client:
        yield client


def post(client, kind, body, filename="up.csv"):
    return client.post(
        "/upload",
        data={"kind": kind, "file": (io.BytesIO(body.encode("utf-8")), filename)},
        content_type="multipart/form-data",
        follow_redirects=True,
    )


PURCHASE = "编号,约定条码\nP1,6901\n"
SCANS = "编号,条码,商品,UTC起,UTC止\nS1,6901,苹果,2026-01-01T00:00:00Z,2026-01-02T00:00:00Z\n"
ARRIVAL = "编号,采购行号,扫码条码,UTC时刻\nA1,P1,6901,2026-01-01T12:00:00Z\n"


def test_upload_flow_and_pages(client):
    assert client.get("/").status_code == 200
    assert post(client, "purchase_lines", PURCHASE).status_code == 200
    assert post(client, "scan_records", SCANS).status_code == 200
    body = post(client, "arrivals", ARRIVAL).data.decode()
    assert "A1" in client.get("/arrivals").data.decode()
    assert "当前放行" in client.get("/arrivals/A1").data.decode()
    assert "受阻" in body or "放行" in body


def test_blocked_arrival_shows_in_both_lists(client):
    # arrival first: both blockers
    post(client, "arrivals", ARRIVAL)
    worklist = client.get("/worklist").data.decode()
    assert "P1" in worklist and "6901" in worklist
    assert "采购行缺失" in worklist
    assert "到货时该码无归属" in worklist

    # supplement purchase line only: purchase side clears, barcode side stays
    post(client, "purchase_lines", PURCHASE)
    worklist = client.get("/worklist").data.decode()
    assert "采购行缺失" not in worklist
    assert "到货时该码无归属" in worklist

    post(client, "scan_records", SCANS)
    worklist = client.get("/worklist").data.decode()
    assert "条码侧无待修项" in worklist
    detail = client.get("/arrivals/A1").data.decode()
    assert "当前放行" in detail


def test_bad_upload_keeps_previous_and_is_reported(client):
    post(client, "purchase_lines", PURCHASE)
    post(client, "scan_records", SCANS)
    post(client, "arrivals", ARRIVAL)
    assert "当前放行" in client.get("/arrivals/A1").data.decode()

    bad = client.post(
        "/upload",
        data={"kind": "arrivals", "file": (io.BytesIO(b"number,foo\nA1,x\n"), "bad.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    ).data.decode()
    assert "上传失败" in bad
    assert "当前放行" in client.get("/arrivals/A1").data.decode()


def test_correction_reblocks_released_arrival_via_web(client):
    post(client, "purchase_lines", PURCHASE)
    post(client, "scan_records", SCANS)
    post(client, "arrivals", ARRIVAL)
    assert "当前放行" in client.get("/arrivals/A1").data.decode()

    # shrink window -> released arrival must fall back to blocked
    post(
        client,
        "scan_records",
        "编号,条码,商品,UTC起,UTC止\nS1,6901,苹果,2026-01-01T13:00:00Z,2026-01-02T00:00:00Z\n",
        "s2.csv",
    )
    assert "当前受阻" in client.get("/arrivals/A1").data.decode()
    assert "到货时该码无归属" in client.get("/worklist").data.decode()


def test_search_and_unknown_arrival(client):
    post(client, "purchase_lines", PURCHASE)
    post(client, "scan_records", SCANS)
    post(client, "arrivals", ARRIVAL)
    assert "A1" in client.get("/arrivals?q=A1").data.decode()
    assert "没有匹配" in client.get("/arrivals?q=ZZZ").data.decode()
    resp = client.get("/arrivals/NOPE", follow_redirects=True)
    assert "找不到到货编号" in resp.data.decode()


def test_dry_run_checks_without_writing(client):
    ok = client.post(
        "/check",
        data={"kind": "purchase_lines", "file": (io.BytesIO(PURCHASE.encode()), "p.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    ).data.decode()
    assert "预检通过" in ok
    # no batch recorded and no data visible
    assert "尚未上传" in client.get("/").data.decode()

    bad = client.post(
        "/check",
        data={"kind": "purchase_lines", "file": (io.BytesIO("编号,foo\nP1,1\n".encode()), "p.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    ).data.decode()
    assert "预检未通过" in bad
    assert "尚未上传" in client.get("/").data.decode()
