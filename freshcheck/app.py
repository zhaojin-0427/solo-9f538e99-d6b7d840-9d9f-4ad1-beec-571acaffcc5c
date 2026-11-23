"""Flask app: upload page, worklists, arrival lookup."""

from __future__ import annotations

import logging
import os

from flask import Flask, flash, redirect, render_template, request, url_for

from . import service
from .csvio import KINDS
from .db import DB_PATH, UploadError, display_utc, init_db, read_conn

app = Flask(__name__)
app.secret_key = os.environ.get("FRESHCHECK_SECRET", "freshcheck-dev-secret")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
app.add_template_filter(display_utc, "utc")
log = logging.getLogger("freshcheck")


_initialised: set[str] = set()


def get_db_path() -> str:
    return app.config.get("DB_PATH") or DB_PATH


@app.before_request
def _ensure_schema() -> None:
    # Idempotent and cheap after the first request per worker/DB.
    path = get_db_path()
    if path not in _initialised:
        init_db(path)
        _initialised.add(path)


@app.route("/")
def index():
    with read_conn(get_db_path()) as conn:
        data = service.dashboard(conn)
    return render_template("index.html", kinds=KINDS, **data)


@app.post("/upload")
def upload():
    kind = request.form.get("kind", "")
    file = request.files.get("file")
    if kind not in KINDS:
        flash("请选择上传类型。", "error")
        return redirect(url_for("index"))
    if file is None or not file.filename:
        flash("请选择 CSV 文件。", "error")
        return redirect(url_for("index"))
    raw = file.read()
    try:
        result = service.upload(kind, file.filename, raw, get_db_path())
    except UploadError as exc:
        flash(f"上传失败，已保留上次结果：{exc}", "error")
        return redirect(url_for("index"))
    except Exception:
        log.exception("upload crashed: kind=%s file=%s", kind, file.filename)
        flash("上传失败（服务内部错误），已保留上次结果；请查看服务日志。", "error")
        return redirect(url_for("index"))
    flash(
        f"已上传{result['label']} {result['rows']} 行（批次 #{result['batch_id']}）；"
        f"共 {result['total_arrivals']} 笔到货，"
        f"{result['blocked_arrivals']} 笔受阻，{result['released_arrivals']} 笔放行。",
        "ok",
    )
    return redirect(url_for("index"))


@app.get("/worklist")
def worklist():
    with read_conn(get_db_path()) as conn:
        lists = service.worklist(conn)
    return render_template("worklist.html", **lists)


@app.get("/arrivals")
def arrivals():
    query = (request.args.get("q") or "").strip()
    with read_conn(get_db_path()) as conn:
        rows = service.list_arrivals(conn, query)
    return render_template("arrivals.html", rows=rows, query=query)


@app.get("/arrivals/<arrival_no>")
def arrival_detail(arrival_no: str):
    with read_conn(get_db_path()) as conn:
        detail = service.arrival_detail(conn, arrival_no)
    if detail is None:
        flash(f"找不到到货编号 {arrival_no}。", "error")
        return redirect(url_for("arrivals"))
    return render_template("arrival_detail.html", **detail)


@app.post("/check")
def check():
    """Dry-run: validate a CSV through the exact same parser without writing."""
    from .csvio import parse_csv

    kind = request.form.get("kind", "")
    file = request.files.get("file")
    if kind not in KINDS or file is None or not file.filename:
        flash("预检需要选择类型和 CSV 文件。", "error")
        return redirect(url_for("index"))
    try:
        rows = parse_csv(kind, file.read())
    except UploadError as exc:
        flash(f"预检未通过（未写入）：{exc}", "error")
        return redirect(url_for("index"))
    flash(
        f"预检通过：{KINDS[kind]['label']} {len(rows)} 行格式合法，"
        f"尚未写入；确认后可正式上传。",
        "ok",
    )
    return redirect(url_for("index"))


@app.errorhandler(413)
def too_large(_exc):
    flash("文件超过 20MB 上限，未写入，已保留上次结果。", "error")
    return redirect(url_for("index"))


if __name__ == "__main__":
    init_db(get_db_path())
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
