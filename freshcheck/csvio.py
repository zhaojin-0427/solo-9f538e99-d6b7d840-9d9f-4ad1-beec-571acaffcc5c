"""CSV parsing for the three upload kinds.

Headers are accepted in Chinese (as used by the operations team) or English.
Both UTF-8 with/without BOM and comma/semicolon/tab delimiters are supported.
Every row is validated *before* anything is written, so one bad row rejects
the whole batch and the previous results stay intact.
"""

from __future__ import annotations

import csv
import io
import re

from .db import REASON_LABELS, SIDE_LABELS, UploadError, parse_utc

KINDS = {
    "arrivals": {
        "label": "到货",
        "columns": [
            ("arrival_no", ["arrival_no", "arrival", "到货编号", "编号"], "到货编号"),
            ("po_line_no", ["po_line_no", "po_line", "purchase_line_no", "采购行号", "采购行"], "采购行号"),
            ("barcode", ["barcode", "扫码条码", "条码"], "扫码条码"),
            ("arrived_at", ["arrived_at", "arrival_time", "arrived_time", "到货时刻", "到货时间", "utc时刻", "utc时间", "time"], "UTC 时刻"),
        ],
    },
    "purchase_lines": {
        "label": "采购行",
        "columns": [
            ("po_line_no", ["po_line_no", "po_line", "purchase_line_no", "采购行号", "采购行", "编号"], "采购行号"),
            ("agreed_barcode", ["agreed_barcode", "purchase_barcode", "约定条码", "条码"], "约定条码"),
        ],
    },
    "scan_records": {
        "label": "用码记录",
        "columns": [
            ("record_no", ["record_no", "scan_no", "用码记录编号", "记录编号", "编号"], "用码记录编号"),
            ("barcode", ["barcode", "条码"], "条码"),
            ("product", ["product", "product_name", "商品", "商品名称"], "商品"),
            ("starts_at", ["starts_at", "start", "valid_from", "utc起", "utc起始", "起始", "起"], "UTC 起"),
            ("ends_at", ["ends_at", "end", "valid_to", "utc止", "utc结束", "结束", "止"], "UTC 止"),
        ],
    },
}


def _norm_header(value: str) -> str:
    value = (value or "").strip().lower().lstrip("﻿")
    value = value.replace("（", "(").replace("）", ")")
    return re.sub(r"[\s\-_/]+", "", value)


def _alias_index(kind: str) -> dict[str, str]:
    index: dict[str, str] = {}
    for field, aliases, _label in KINDS[kind]["columns"]:
        for alias in aliases:
            index[_norm_header(alias)] = field
    return index


DELIMITERS = (",", ";", "\t", "|")


def parse_csv(kind: str, raw: bytes) -> list[dict]:
    if kind not in KINDS:
        raise UploadError(f"未知的上传类型: {kind}")
    if not raw or not raw.strip():
        raise UploadError("CSV 文件为空")

    text = _decode(raw)
    aliases = _alias_index(kind)
    required = [field for field, _aliases, _label in KINDS[kind]["columns"]]
    labels = {field: label for field, _aliases, label in KINDS[kind]["columns"]}

    # Try each plausible delimiter and keep the first one whose header maps
    # every required column.  Guessing from a sample alone is not reliable for
    # short files or numeric barcodes.
    best: tuple[list[list[str]], dict[int, str]] | None = None
    best_missing: list[str] = required
    best_delimiter = DELIMITERS[0]
    for delimiter in DELIMITERS:
        rows = _read_rows(text, delimiter)
        if not rows:
            continue
        mapping = _map_header(rows[0], aliases)
        missing = [field for field in required if field not in mapping.values()]
        if not missing:
            best, best_missing, best_delimiter = (rows, mapping), [], delimiter
            break
        if len(missing) < len(best_missing):
            best, best_missing, best_delimiter = (rows, mapping), missing, delimiter

    if best is None or best_missing:
        header = _read_rows(text, best_delimiter)
        seen = " | ".join(cell.strip() for cell in header[0]) if header else ""
        raise UploadError(
            "CSV 表头缺少列: " + "、".join(labels[field] for field in best_missing)
            + (f"（读取到的表头: {seen}）" if seen else "")
        )

    rows, mapping = best
    parsed: list[dict] = []
    for line_no, row in enumerate(rows[1:], start=2):
        if len(row) < len(rows[0]):
            row = list(row) + [""] * (len(rows[0]) - len(row))
        record = {field: (row[idx] or "").strip() for idx, field in mapping.items()}
        try:
            _validate_row(kind, record, line_no)
        except UploadError as exc:
            raise UploadError(f"第 {line_no} 行: {exc}") from exc
        record["_line"] = line_no
        parsed.append(record)
    if not parsed:
        raise UploadError("CSV 没有数据行")
    return parsed


def _read_rows(text: str, delimiter: str) -> list[list[str]]:
    try:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        rows = list(reader)
    except csv.Error as exc:
        raise UploadError(f"CSV 解析失败: {exc}") from exc
    return [row for row in rows if any((cell or "").strip() for cell in row)]


def _map_header(header: list[str], aliases: dict[str, str]) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for idx, cell in enumerate(header):
        field = aliases.get(_norm_header(cell))
        if field and field not in mapping.values():
            mapping[idx] = field
    return mapping


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UploadError("CSV 必须是 UTF-8 或 GB18030 编码")


def _validate_row(kind: str, row: dict, line_no: int) -> None:
    if kind == "arrivals":
        for field, label in (("arrival_no", "到货编号"), ("po_line_no", "采购行号"), ("barcode", "扫码条码")):
            if not row.get(field):
                raise UploadError(f"{label}不能为空")
        row["arrived_at"] = parse_utc(row.get("arrived_at"), "UTC 时刻")
    elif kind == "purchase_lines":
        for field, label in (("po_line_no", "采购行号"), ("agreed_barcode", "约定条码")):
            if not row.get(field):
                raise UploadError(f"{label}不能为空")
    elif kind == "scan_records":
        for field, label in (("record_no", "用码记录编号"), ("barcode", "条码"), ("product", "商品")):
            if not row.get(field):
                raise UploadError(f"{label}不能为空")
        row["starts_at"] = parse_utc(row.get("starts_at"), "UTC 起")
        row["ends_at"] = parse_utc(row.get("ends_at"), "UTC 止")
        if row["ends_at"] < row["starts_at"]:
            raise UploadError("UTC 止不能早于 UTC 起")


def human_reason(reason: str) -> str:
    return REASON_LABELS.get(reason, reason)


def human_side(side: str) -> str:
    return SIDE_LABELS.get(side, side)
