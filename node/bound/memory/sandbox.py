# fiber.node.bound.memory.sandbox
"""@desc: Workspace-confined read-only DuckDB SQL sandbox and shared tabular/xlsx loaders."""
from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime, time
from decimal import Decimal
import glob as pyglob
import math
from pathlib import Path
import re
from typing import Any
import xml.etree.ElementTree as ET
import zipfile

import duckdb
from xphi.arch.contract.protocol.binder import ToolError

_CELL_REF_RE = re.compile(r"^([A-Za-z]+)")
_READONLY_PREFIX_RE = re.compile(r"^(?:SELECT|WITH|DESCRIBE|SUMMARIZE|EXPLAIN)\b", re.IGNORECASE)
_BLOCKED_SQL_RE = re.compile(
    r"\b(?:COPY|ATTACH|DETACH|INSTALL|LOAD|EXPORT|PRAGMA|SET|"
    r"CREATE|INSERT|UPDATE|DELETE|DROP|ALTER|CALL|TRUNCATE|REPLACE)\b",
    re.IGNORECASE,
)
_SQL_COMMENT_OR_STR_RE = re.compile(
    r"--[^\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\""
)
_PATH_TRAVERSAL_TOKEN_RE = re.compile(r"(?:^|[\s'\"(/\\])\.\.(?:[\s'\")/\\]|$)")
_READ_FN_PATH_RE = re.compile(
    r"read_(?:csv_auto|csv|json_auto|json|parquet)\s*\(\s*'((?:''|[^'])+)'\s*\)",
    re.IGNORECASE,
)
_CLAUSE_PATH_RE = re.compile(
    r"\b(FROM|JOIN|DESCRIBE|SUMMARIZE)\s+'((?:''|[^'])+)'",
    re.IGNORECASE,
)
_GLOB_CHARS = frozenset("*?[")

_DUCK_READER_BY_EXT: dict[str, str] = {
    ".csv": "read_csv_auto",
    ".tsv": "read_csv_auto",
    ".json": "read_json_auto",
    ".jsonl": "read_json_auto",
    ".ndjson": "read_json_auto",
    ".parquet": "read_parquet",
}


class TabularSandbox:
    """워크스페이스 경계 검증, 읽기 전용 DuckDB 격리 실행, 정형/XLSX 로더를 단일화한 샌드박스."""

    def __init__(self, root: Path | str = ".") -> None:
        self.root = Path(root).resolve()

    # ------------------------------------------------------------------ Static Tabular & SQL Primitives

    @staticmethod
    def dedupe_headers(raw_headers: Sequence[str]) -> list[str]:
        seen: dict[str, int] = {}
        out: list[str] = []
        for i, h in enumerate(raw_headers):
            base = (h or "").strip() or f"col_{i + 1}"
            seen[base] = seen.get(base, 0) + 1
            out.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
        return out

    @staticmethod
    def json_safe_cell(val: Any) -> Any:
        if val is None or isinstance(val, (bool, int, str)):
            return val
        if isinstance(val, float):
            return None if (math.isnan(val) or math.isinf(val)) else val
        if isinstance(val, Decimal):
            return str(val)
        if isinstance(val, (datetime, date, time)):
            return val.isoformat()
        if isinstance(val, bytes):
            return val.decode("utf-8", "replace")
        return str(val)

    @staticmethod
    def validate_sql(sql: str) -> str:
        cleaned = sql.strip().rstrip(";").strip()
        if not cleaned:
            raise ToolError("SQL query must not be empty.")
        if _PATH_TRAVERSAL_TOKEN_RE.search(cleaned):
            raise ToolError("Parent directory traversal ('..') is not allowed in SQL queries.")

        scrubbed = _SQL_COMMENT_OR_STR_RE.sub(" ", cleaned).strip()
        if not _READONLY_PREFIX_RE.match(scrubbed) or _BLOCKED_SQL_RE.search(scrubbed):
            raise ToolError(
                "Only read-only analytical queries (SELECT, WITH, DESCRIBE, SUMMARIZE, EXPLAIN) are allowed."
            )
        return cleaned

    @classmethod
    def read_xlsx(cls, path: Path) -> tuple[list[str], list[dict[str, Any]]]:
        """원천 .xlsx 파일을 셀 좌표(r='C2') 그대로 읽으며 문서용 정규화를 적용하지 않습니다."""
        local_tag = lambda el: el.tag.rsplit("}", 1)[-1]
        try:
            with zipfile.ZipFile(path, "r") as zf:
                shared: list[str] = []
                if "xl/sharedStrings.xml" in zf.namelist():
                    for si in ET.fromstring(zf.read("xl/sharedStrings.xml")):
                        if local_tag(si) == "si":
                            shared.append(
                                "".join(t.text or "" for t in si.iter() if local_tag(t) == "t")
                            )

                sheets = sorted(
                    n
                    for n in zf.namelist()
                    if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
                )
                if not sheets:
                    return [], []

                raw_rows: list[list[str]] = []
                max_cols = 0
                for row_el in ET.fromstring(zf.read(sheets[0])).iter():
                    if local_tag(row_el) != "row":
                        continue
                    row_map: dict[int, str] = {}
                    next_col = 0
                    for c_el in row_el:
                        if local_tag(c_el) != "c":
                            continue
                        coord = _CELL_REF_RE.match(c_el.attrib.get("r", "").strip())
                        if coord:
                            col_idx = 0
                            for ch in coord.group(1).upper():
                                col_idx = col_idx * 26 + (ord(ch) - ord("A") + 1)
                            col_idx -= 1
                        else:
                            col_idx = next_col
                        next_col = col_idx + 1

                        t_attr = c_el.attrib.get("t", "")
                        v_txt = next((ch.text or "" for ch in c_el if local_tag(ch) == "v"), "")
                        if t_attr == "s" and v_txt.isdigit() and int(v_txt) < len(shared):
                            val = shared[int(v_txt)]
                        elif t_attr == "inlineStr":
                            val = "".join(t.text or "" for t in c_el.iter() if local_tag(t) == "t")
                        elif t_attr == "b":
                            val = "TRUE" if v_txt == "1" else "FALSE"
                        else:
                            val = v_txt
                        row_map[col_idx] = val.strip()

                    if row_map and any(row_map.values()):
                        width = max(row_map) + 1
                        max_cols = max(max_cols, width)
                        raw_rows.append([row_map.get(i, "") for i in range(width)])
        except (zipfile.BadZipFile, ET.ParseError, KeyError, OSError) as exc:
            raise ToolError(f"Invalid or unreadable .xlsx file '{path.name}': {exc}") from None

        if len(raw_rows) < 2:
            return (cls.dedupe_headers(raw_rows[0]) if raw_rows else []), []

        padded = [r + [""] * (max_cols - len(r)) for r in raw_rows]
        headers = cls.dedupe_headers(padded[0])
        records = [{headers[i]: r[i] for i in range(max_cols)} for r in padded[1:]]
        return headers, records

    @classmethod
    def load_records(cls, src_file: Path) -> tuple[list[str], list[dict[str, Any]]]:
        if src_file.suffix.lower() == ".xlsx":
            return cls.read_xlsx(src_file)
        with cls._locked_session({"_src": [src_file]}) as db:
            rel = db.sql('SELECT * FROM "_src"')
            cols = [d[0] for d in rel.description]
            return cols, [dict(zip(cols, r)) for r in rel.fetchall()]

    # ------------------------------------------------------------------ Workspace-Bound Operations

    def resolve_files(self, raw_spec: str) -> tuple[list[Path], str]:
        clean = raw_spec.strip().replace("\\", "/")
        if not clean:
            raise ToolError("Source path must not be empty.")
        if clean.startswith("/") or ".." in clean.split("/"):
            raise ToolError(f"Path outside workspace root is forbidden: {raw_spec}")

        if any(ch in clean for ch in _GLOB_CHARS):
            matched: list[Path] = []
            for hit in sorted(pyglob.glob(str(self.root / clean), recursive=True)):
                p = Path(hit).resolve()
                if not p.is_relative_to(self.root):
                    raise ToolError(f"Glob matched path outside workspace root: {raw_spec}")
                if p.is_file():
                    matched.append(p)
            if not matched:
                raise ToolError(f"No files matched pattern: {raw_spec}")
            return matched, clean

        candidate = (self.root / clean).resolve()
        if not candidate.is_relative_to(self.root):
            raise ToolError(f"Path outside workspace root is forbidden: {raw_spec}")
        if not candidate.is_file():
            raise ToolError(f"File not found: {raw_spec}")
        return [candidate], candidate.relative_to(self.root).as_posix()

    def describe(self, rel_source: str) -> list[dict[str, Any]]:
        files, _ = self.resolve_files(rel_source)
        if len(files) > 1:
            raise ToolError("'describe_data' requires a single file path, not a glob pattern.")
        with self._locked_session({"_desc_target": files}) as db:
            rel = db.sql('DESCRIBE SELECT * FROM "_desc_target"')
            cols = [d[0] for d in rel.description]
            return [
                {c: (str(v) if v is not None else None) for c, v in zip(cols, row)}
                for row in rel.fetchall()
            ]

    def query(
        self,
        sql: str,
        limit: int = 100,
        pinned_sources: Mapping[str, Path] | None = None,
    ) -> list[dict[str, Any]]:
        cleaned_sql = self.validate_sql(sql)
        rewritten, mounts = self._bind_sources(cleaned_sql, pinned_sources)

        with self._locked_session(mounts) as db:
            scrubbed = _SQL_COMMENT_OR_STR_RE.sub(" ", rewritten).strip().upper()
            capped = max(1, limit)
            wrapped = (
                f"SELECT * FROM ({rewritten}) AS _q LIMIT {capped}"
                if scrubbed.startswith(("SELECT", "WITH"))
                else rewritten
            )
            rel = db.sql(wrapped)
            cols = [d[0] for d in rel.description]
            return [
                {col: self.json_safe_cell(val) for col, val in zip(cols, row)}
                for row in rel.fetchall()
            ]

    # ------------------------------------------------------------------ Internal DuckDB Isolation

    def _bind_sources(
        self,
        cleaned_sql: str,
        pinned_sources: Mapping[str, Path] | None,
    ) -> tuple[str, dict[str, list[Path]]]:
        mounts: dict[str, list[Path]] = {}
        alias_by_key: dict[str, str] = {}

        if pinned_sources is not None:
            for idx, (norm_rel, fpath) in enumerate(pinned_sources.items()):
                alias = f"src_{idx}"
                alias_by_key[norm_rel] = alias
                mounts[alias] = [fpath]

        def _resolve_literal(raw_lit: str) -> str | None:
            unescaped = raw_lit.replace("''", "'").strip()
            if not unescaped:
                return None
            if pinned_sources is not None:
                if unescaped.startswith("/") or ".." in unescaped.replace("\\", "/").split("/"):
                    raise ToolError(f"Path outside workspace root is forbidden: {unescaped}")
                cand = (self.root / unescaped).resolve()
                if not cand.is_relative_to(self.root):
                    raise ToolError(f"Path outside workspace root is forbidden: {unescaped}")
                alias = alias_by_key.get(cand.relative_to(self.root).as_posix())
                return f'"{alias}"' if alias else None

            if "/" not in unescaped and "." not in unescaped:
                return None
            files, canonical_key = self.resolve_files(unescaped)
            alias = alias_by_key.get(canonical_key)
            if alias is None:
                alias = f"src_{len(alias_by_key)}"
                alias_by_key[canonical_key] = alias
                mounts[alias] = files
            return f'"{alias}"'

        rewritten = _READ_FN_PATH_RE.sub(
            lambda m: _resolve_literal(m.group(1)) or m.group(0), cleaned_sql
        )
        rewritten = _CLAUSE_PATH_RE.sub(
            lambda m: f"{m.group(1)} {mapped}"
            if (mapped := _resolve_literal(m.group(2)))
            else m.group(0),
            rewritten,
        )
        return rewritten, mounts

    @classmethod
    @contextmanager
    def _locked_session(
        cls,
        mounts: Mapping[str, Sequence[Path]],
    ) -> Iterator[duckdb.DuckDBPyConnection]:
        try:
            with duckdb.connect(":memory:") as db:
                db.execute("SET autoinstall_known_extensions = false;")
                db.execute("SET autoload_known_extensions = false;")
                for alias, files in mounts.items():
                    cls._mount_table(db, alias, files)
                db.execute("SET enable_external_access = false;")
                yield db
        except duckdb.Error as de:
            raise ToolError(f"DuckDB SQL Error: {de}") from None

    @classmethod
    def _mount_table(
        cls,
        db: duckdb.DuckDBPyConnection,
        alias: str,
        files: Sequence[Path],
    ) -> None:
        if not files:
            raise ToolError(f"No matching source files to mount for '{alias}'.")
        exts = {f.suffix.lower() for f in files}
        if len(exts) > 1:
            raise ToolError(
                f"Cannot mix heterogeneous file extensions in one source: {sorted(exts)}"
            )

        ext = next(iter(exts))
        db.execute(f'DROP TABLE IF EXISTS "{alias}"')

        if ext == ".xlsx":
            if len(files) > 1:
                raise ToolError(
                    "Globbing multiple .xlsx files in a single table reference is not supported."
                )
            headers, rows = cls.read_xlsx(files[0])
            if not headers:
                raise ToolError(f"No columns found in {files[0].name}")
            col_defs = ", ".join(f'"{h.replace(chr(34), "")}" VARCHAR' for h in headers)
            db.execute(f'CREATE TEMP TABLE "{alias}" ({col_defs})')
            if rows:
                ph = ", ".join("?" for _ in headers)
                db.executemany(
                    f'INSERT INTO "{alias}" VALUES ({ph})',
                    [[r.get(h, "") for h in headers] for r in rows],
                )
            return

        reader_fn = _DUCK_READER_BY_EXT.get(ext)
        if reader_fn is None:
            raise ToolError(
                f"Unsupported tabular format: {ext!r} (expected .csv, .tsv, .jsonl, .json, .parquet, .xlsx)"
            )
        quoted = [f"'{f.as_posix().replace(chr(39), chr(39) * 2)}'" for f in files]
        src_expr = quoted[0] if len(quoted) == 1 else f"[{', '.join(quoted)}]"
        db.execute(f'CREATE TEMP TABLE "{alias}" AS SELECT * FROM {reader_fn}({src_expr})')