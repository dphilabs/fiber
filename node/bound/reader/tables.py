# fiber.node.bound.reader.tables
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import duckdb
from xphi.arch.contract.protocol.binder import ToolError

_NUM_CLEAN_RE = re.compile(r"[,\s₩$€¥%]")
_CELL_REF_RE = re.compile(r"^([A-Za-z]+)")
_TOTAL_LABELS = {"합계", "총계", "소계", "계", "total", "subtotal", "sum"}
_MAX_DIFF_ROWS = 100


def try_float(val: Any) -> Optional[float]:
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


def _dedupe_headers(raw_headers: Sequence[str]) -> List[str]:
    seen: Dict[str, int] = {}
    out: List[str] = []
    for i, h in enumerate(raw_headers):
        base = (h or "").strip() or f"col_{i + 1}"
        seen[base] = seen.get(base, 0) + 1
        out.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    return out


def table_to_markdown(rows: List[List[str]]) -> str:
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
    raw_rows: List[List[str]], key_names: Sequence[str] = ()
) -> Tuple[List[str], List[Dict[str, Any]], str]:
    """문서 내 표 전용 정규화: 문자 범주 열(조인 키·숫자 열 제외)만 병합 셀을 채우고, 합계 행이 있을 때만 검증합니다."""
    clean = [[(c or "").strip() for c in r] for r in raw_rows if any((c or "").strip() for c in r)]
    if len(clean) < 2:
        return [], [], "EMPTY"

    w = max(len(r) for r in clean)
    padded = [r + [""] * (w - len(r)) for r in clean]
    headers = _dedupe_headers(padded[0])
    key_set = set(key_names)

    has_total = len(padded) >= 3 and _is_summary_row(padded[-1])
    body_src = padded[1:-1] if has_total else padded[1:]
    total_row = padded[-1] if has_total else None

    num_cols: Set[int] = {
        c for c in range(w)
        if headers[c] not in key_set
        and (vals := [r[c] for r in body_src if r[c]])
        and sum(try_float(v) is not None for v in vals) / len(vals) >= 0.6
    }

    prev = [""] * w
    filled: List[List[str]] = []
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

    records: List[Dict[str, Any]] = []
    for r in filled:
        rec: Dict[str, Any] = {}
        for i, h in enumerate(headers):
            if i in num_cols and (num := try_float(r[i])) is not None:
                rec[h] = int(num) if num.is_integer() else num
            else:
                rec[h] = r[i]
        records.append(rec)
    return headers, records, checks


def _col_letters_to_index(cell_ref: str) -> Optional[int]:
    m = _CELL_REF_RE.match(cell_ref.strip())
    if not m:
        return None
    idx = 0
    for ch in m.group(1).upper():
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def read_xlsx_records(path: Path) -> Tuple[List[str], List[Dict[str, Any]]]:
    """원천 .xlsx 파일을 셀 좌표(r='C2') 그대로 읽으며, 문서용 정규화나 행 제거를 적용하지 않습니다."""
    tag = lambda el: el.tag.rsplit("}", 1)[-1]
    with zipfile.ZipFile(path, "r") as zf:
        shared: List[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            for si in ET.fromstring(zf.read("xl/sharedStrings.xml")):
                if tag(si) == "si":
                    shared.append("".join(t.text or "" for t in si.iter() if tag(t) == "t"))

        sheets = sorted(n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        if not sheets:
            return [], []

        raw_rows: List[List[str]] = []
        max_cols = 0
        for row_el in ET.fromstring(zf.read(sheets[0])).iter():
            if tag(row_el) != "row":
                continue
            row_map: Dict[int, str] = {}
            next_col = 0
            for c_el in row_el:
                if tag(c_el) != "c":
                    continue
                coord_idx = _col_letters_to_index(c_el.attrib.get("r", ""))
                col_idx = coord_idx if coord_idx is not None else next_col
                next_col = col_idx + 1

                t_attr = c_el.attrib.get("t", "")
                v_txt = next((ch.text or "" for ch in c_el if tag(ch) == "v"), "")
                if t_attr == "s" and v_txt.isdigit() and int(v_txt) < len(shared):
                    val = shared[int(v_txt)]
                elif t_attr == "inlineStr":
                    val = "".join(t.text or "" for t in c_el.iter() if tag(t) == "t")
                elif t_attr == "b":
                    val = "TRUE" if v_txt == "1" else "FALSE"
                else:
                    val = v_txt
                row_map[col_idx] = val.strip()

            if row_map and any(v for v in row_map.values()):
                width = max(row_map.keys()) + 1
                max_cols = max(max_cols, width)
                raw_rows.append([row_map.get(i, "") for i in range(width)])

    if len(raw_rows) < 2:
        return (_dedupe_headers(raw_rows[0]) if raw_rows else []), []

    padded = [r + [""] * (max_cols - len(r)) for r in raw_rows]
    headers = _dedupe_headers(padded[0])
    records = [{headers[i]: r[i] for i in range(max_cols)} for r in padded[1:]]
    return headers, records


def duckdb_source_sql(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in (".csv", ".tsv"):
        return "SELECT * FROM read_csv_auto($1)"
    if ext in (".json", ".jsonl", ".ndjson"):
        return "SELECT * FROM read_json_auto($1)"
    if ext == ".parquet":
        return "SELECT * FROM read_parquet($1)"
    raise ToolError(f"Unsupported tabular source format: {ext!r} (expected .csv, .tsv, .jsonl, .json, .parquet, .xlsx)")


def _load_source_records(src_file: Path) -> Tuple[List[str], List[Dict[str, Any]]]:
    if src_file.suffix.lower() == ".xlsx":
        return read_xlsx_records(src_file)
    sql = duckdb_source_sql(src_file)
    with duckdb.connect(":memory:") as db:
        db.execute("SET autoinstall_known_extensions = false;")
        db.execute("SET autoload_known_extensions = false;")
        rel = db.execute(sql, [str(src_file)])
        cols = [d[0] for d in rel.description]
        rows = [dict(zip(cols, r)) for r in rel.fetchall()]
        return cols, rows


def compare_section_table(
    sections: List[Any],
    doc_rel: str,
    section_id: str,
    src_file: Path,
    src_rel: str,
    keys: List[str],
    tolerance: float,
    table_index: int = 0,
) -> Dict[str, Any]:
    clean_id = section_id.strip().lstrip("#")
    sec = next((s for s in sections if s.section_id == clean_id), None) if clean_id else (sections[0] if sections else None)
    if sec is None or not sec.tables:
        raise ToolError(f"No table found in {doc_rel}#{clean_id}")
    if table_index < 0 or table_index >= len(sec.tables):
        raise ToolError(f"table_index {table_index} out of range (section has {len(sec.tables)} table(s)).")

    headers, doc_rows, checks = normalize_doc_table(sec.tables[table_index], key_names=keys)
    if not doc_rows:
        raise ToolError("Extracted table has no data rows.")
    if miss := [k for k in keys if k not in headers]:
        raise ToolError(f"Join key(s) {miss} missing in document table: {headers}")

    src_cols, src_rows = _load_source_records(src_file)
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
    mismatched_all: List[Dict[str, Any]] = []
    matched = 0

    for k in sorted(d_map.keys() & s_map.keys()):
        dr, sr = d_map[k], s_map[k]
        diffs: Dict[str, Any] = {}
        for c in val_cols:
            dv, sv = dr.get(c), sr.get(c)
            dn, sn = try_float(dv), try_float(sv)
            if dn is not None and sn is not None:
                if abs(dn - sn) > tolerance:
                    diffs[c] = {"doc": dn, "source": sn, "diff": round(dn - sn, 6)}
            elif str(dv if dv is not None else "").strip() != str(sv if sv is not None else "").strip():
                diffs[c] = {"doc": dv, "source": sv}
        if diffs:
            mismatched_all.append({"keys": {kk: _canon_key_part(dr.get(kk)) for kk in keys}, "diffs": diffs})
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