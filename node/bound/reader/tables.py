# fiber.node.bound.reader.tables
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import re
from typing import Any

from fiber.node.bound.memory.sandbox import TabularSandbox
from xphi.arch.contract.protocol.binder import ToolError

_NUM_CLEAN_RE = re.compile(r"[,\s₩$€¥%]")
_TOTAL_LABELS = {"합계", "총계", "소계", "계", "total", "subtotal", "sum"}
_MAX_DIFF_ROWS = 100


def try_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = _NUM_CLEAN_RE.sub("", str(val).strip())
    if not s or s in ("-", "—"):
        return None
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return float(s)
    except ValueError:
        return None


def _canon_key_part(val: Any) -> str:
    """숫자형 키('1001.0' vs 1001)의 표현 차이를 제거합니다."""
    if val is None:
        return ""
    if isinstance(val, int) and not isinstance(val, bool):
        return str(val)
    if isinstance(val, float):
        return str(int(val)) if val.is_integer() else f"{val:g}"
    s = str(val).strip()
    num = try_float(s)
    if num is not None and not (len(s) > 1 and s.startswith("0") and s.isdigit()):
        return str(int(num)) if num.is_integer() else f"{num:g}"
    return s


def table_to_markdown(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    w = max(len(r) for r in rows)
    norm = [[(c or "").replace("\n", " ").strip() for c in r] + [""] * (w - len(r)) for r in rows]
    head = "| " + " | ".join(norm[0]) + " |\n| " + " | ".join(["---"] * w) + " |"
    body = "\n".join("| " + " | ".join(r) + " |" for r in norm[1:])
    return f"{head}\n{body}" if body else head


def _is_summary_row(row: Sequence[str]) -> bool:
    first_non_empty = next((c.strip() for c in row if c and c.strip()), "")
    if not first_non_empty:
        return False
    compact = re.sub(r"[\s:()\[\]·\-_]", "", first_non_empty).lower()
    return compact in _TOTAL_LABELS


def normalize_doc_table(
    raw_rows: list[list[str]], key_names: Sequence[str] = ()
) -> tuple[list[str], list[dict[str, Any]], str]:
    """문서 내 표 전용 정규화: 문자 범주 열만 병합 셀을 채우고 합계 행이 있을 때만 검산합니다."""
    clean = [[(c or "").strip() for c in r] for r in raw_rows if any((c or "").strip() for c in r)]
    if len(clean) < 2:
        return [], [], "EMPTY"

    w = max(len(r) for r in clean)
    padded = [r + [""] * (w - len(r)) for r in clean]
    headers = TabularSandbox.dedupe_headers(padded[0])
    key_set = set(key_names)

    has_total = len(padded) >= 3 and _is_summary_row(padded[-1])
    body_src = padded[1:-1] if has_total else padded[1:]
    total_row = padded[-1] if has_total else None

    num_cols: set[int] = {
        c
        for c in range(w)
        if headers[c] not in key_set
        and (vals := [r[c] for r in body_src if r[c]])
        and sum(try_float(v) is not None for v in vals) / len(vals) >= 0.6
    }

    prev = [""] * w
    filled: list[list[str]] = []
    for r in body_src:
        out = list(r)
        for c in range(w):
            if c not in num_cols and headers[c] not in key_set:
                if out[c]:
                    prev[c] = out[c]
                elif prev[c] and any(out):
                    out[c] = prev[c]
        filled.append(out)

    checks = "UNCHECKED"
    if total_row is not None and num_cols:
        checked_any = False
        for c in sorted(num_cols):
            exp = try_float(total_row[c])
            if exp is None:
                continue
            checked_any = True
            act = sum(try_float(r[c]) or 0.0 for r in filled)
            if abs(act - exp) > 1e-2:
                checks = f"MISMATCH(col={headers[c]}: sum={act:g} != total={exp:g})"
                break
        if checked_any and checks == "UNCHECKED":
            checks = "PASS"

    records: list[dict[str, Any]] = []
    for r in filled:
        rec: dict[str, Any] = {}
        for i, h in enumerate(headers):
            if i in num_cols and (num := try_float(r[i])) is not None:
                rec[h] = int(num) if num.is_integer() else num
            else:
                rec[h] = r[i]
        records.append(rec)
    return headers, records, checks


def compare_section_table(
    sections: list[Any],
    doc_rel: str,
    section_id: str,
    src_file: Path,
    src_rel: str,
    keys: list[str],
    tolerance: float,
    table_index: int = 0,
) -> dict[str, Any]:
    clean_id = section_id.strip().lstrip("#")
    sec = (
        next((s for s in sections if s.section_id == clean_id), None)
        if clean_id
        else (sections[0] if sections else None)
    )
    if sec is None or not sec.tables:
        raise ToolError(f"No table found in {doc_rel}#{clean_id}")
    if table_index < 0 or table_index >= len(sec.tables):
        raise ToolError(
            f"table_index {table_index} out of range (section has {len(sec.tables)} table(s))."
        )

    headers, doc_rows, checks = normalize_doc_table(sec.tables[table_index], key_names=keys)
    if not doc_rows:
        raise ToolError("Extracted table has no data rows.")
    if miss := [k for k in keys if k not in headers]:
        raise ToolError(f"Join key(s) {miss} missing in document table: {headers}")

    src_cols, src_rows = TabularSandbox.load_records(src_file)
    if not src_cols:
        raise ToolError(f"No columns found in source: {src_rel}")
    if miss_src := [k for k in keys if k not in src_cols]:
        raise ToolError(f"Join key(s) {miss_src} missing in source: {src_cols}")

    val_cols = [c for c in headers if c in src_cols and c not in keys]
    kfn = lambda r: tuple(_canon_key_part(r.get(k)) for k in keys)
    d_map = {kfn(r): r for r in doc_rows}
    s_map = {kfn(r): r for r in src_rows}

    only_doc_all = [d_map[k] for k in sorted(d_map.keys() - s_map.keys())]
    only_src_all = [s_map[k] for k in sorted(s_map.keys() - d_map.keys())]
    mismatched_all: list[dict[str, Any]] = []
    matched = 0

    for k in sorted(d_map.keys() & s_map.keys()):
        dr, sr = d_map[k], s_map[k]
        diffs: dict[str, Any] = {}
        for c in val_cols:
            dv, sv = dr.get(c), sr.get(c)
            dn, sn = try_float(dv), try_float(sv)
            if dn is not None and sn is not None:
                if abs(dn - sn) > tolerance:
                    diffs[c] = {"doc": dn, "source": sn, "diff": round(dn - sn, 6)}
            elif str(dv if dv is not None else "").strip() != str(sv if sv is not None else "").strip():
                diffs[c] = {"doc": dv, "source": sv}
        if diffs:
            mismatched_all.append(
                {"keys": {kk: _canon_key_part(dr.get(kk)) for kk in keys}, "diffs": diffs}
            )
        else:
            matched += 1

    truncated = (
        len(only_doc_all) > _MAX_DIFF_ROWS
        or len(only_src_all) > _MAX_DIFF_ROWS
        or len(mismatched_all) > _MAX_DIFF_ROWS
    )
    trunc_note = f" | TRUNCATED(cap={_MAX_DIFF_ROWS})" if truncated else ""
    manifest = (
        f"[COMPARE MANIFEST | doc={doc_rel}#{sec.section_id}[t={table_index}] | source={src_rel} | "
        f"matched={matched} | only_in_doc={len(only_doc_all)} | only_in_source={len(only_src_all)} | "
        f"mismatched={len(mismatched_all)} | checks={checks}{trunc_note}]"
    )
    return {
        "only_in_doc": only_doc_all[:_MAX_DIFF_ROWS],
        "only_in_source": only_src_all[:_MAX_DIFF_ROWS],
        "mismatched": mismatched_all[:_MAX_DIFF_ROWS],
        "truncated": truncated,
        "checks": checks,
        "manifest": manifest,
    }