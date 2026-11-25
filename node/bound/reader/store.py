# fiber.node.bound.reader.store
from __future__ import annotations

import datetime as dt
import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from xphi.arch.contract.protocol.binder import ToolError

from fiber.node.bound.reader.parsers import SUPPORTED_EXTS, ParsedSection, parse_document
from fiber.node.bound.reader.runtime import ReaderRuntimeOpts

_SKIP_DIRS = {".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv", ".anchor"}
_DEMOTED_DIRS = {"archive", "archives", "old", "backup", "deprecated", "구버전", "백업"}
_VER_RE = re.compile(
    r"(?:[_\-\s]*(?:v\d+(?:\.\d+)?|rev\d*|r\d+|draft|final|최종|수정|초안|완성|\(\d+\)|\d{6,8}))+$",
    re.IGNORECASE,
)


def _sha12(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def _version_group(rel_posix: str) -> str:
    p = Path(rel_posix)
    parent = "/".join(x for x in p.parent.parts if x.lower() not in _DEMOTED_DIRS)
    stem = _VER_RE.sub("", p.stem).strip("_- ").lower() or p.stem.lower()
    return f"{parent}/{stem}"


class CorpusStore:
    def __init__(self, root: Path, db_path: Path, opts: ReaderRuntimeOpts) -> None:
        self.root = root
        self.db_path = db_path
        self.opts = opts
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.db_path))
        c.row_factory = sqlite3.Row
        return c

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS docs (
                    doc_path TEXT PRIMARY KEY, module TEXT NOT NULL, version_group TEXT NOT NULL,
                    mtime REAL NOT NULL, is_latest INTEGER NOT NULL DEFAULT 1, superseded_by TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS sections (
                    addr TEXT PRIMARY KEY, doc_path TEXT NOT NULL, section_id TEXT NOT NULL, ord INTEGER NOT NULL,
                    heading TEXT NOT NULL, level INTEGER NOT NULL, loc TEXT NOT NULL, body TEXT NOT NULL,
                    content_hash TEXT NOT NULL, prev_hash TEXT NOT NULL DEFAULT '', changed_at REAL NOT NULL, flags TEXT NOT NULL DEFAULT ''
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS fts_uni USING fts5(addr UNINDEXED, doc_path, heading, body, tokenize='unicode61');
                """
            )
            if self.opts.fts_trigram:
                conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts_tri USING fts5(addr UNINDEXED, doc_path, heading, body, tokenize='trigram');")

    def is_empty(self) -> bool:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 0

    def get_doc_mtime(self, doc_rel: str) -> Optional[float]:
        with self._conn() as conn:
            row = conn.execute("SELECT mtime FROM docs WHERE doc_path = ?", (doc_rel,)).fetchone()
            return float(row["mtime"]) if row is not None else None

    def _iter_files(self, base: Path) -> Iterable[Path]:
        if base.is_file():
            if base.suffix.lower() in SUPPORTED_EXTS:
                yield base
            return
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in SUPPORTED_EXTS or p.name.startswith(("~$", ".")):
                continue
            if any(part in _SKIP_DIRS or part.startswith(".") for part in p.relative_to(self.root).parts[:-1]):
                continue
            yield p

    def sync(self, base: Path, partial: bool) -> Dict[str, Any]:
        now = dt.datetime.now(dt.timezone.utc).timestamp()
        docs_n, secs_n, skipped = 0, 0, 0
        failed_docs: List[Dict[str, str]] = []

        with self._conn() as conn:
            existing = {r["doc_path"]: r["mtime"] for r in conn.execute("SELECT doc_path, mtime FROM docs")}
            seen: Set[str] = set()

            for fpath in self._iter_files(base):
                rel = fpath.relative_to(self.root).as_posix()
                seen.add(rel)
                try:
                    mtime = fpath.stat().st_mtime
                except OSError as oe:
                    failed_docs.append({"path": rel, "error": str(oe)})
                    continue

                if rel in existing and abs(existing[rel] - mtime) < 1e-3:
                    skipped += 1
                    continue

                prev_map = {
                    r["section_id"]: (r["content_hash"], r["changed_at"])
                    for r in conn.execute("SELECT section_id, content_hash, changed_at FROM sections WHERE doc_path = ?", (rel,))
                }
                try:
                    parsed = parse_document(fpath, self.opts)
                except Exception as exc:
                    err_msg = f"{type(exc).__name__}: {str(exc)[:200]}"
                    failed_docs.append({"path": rel, "error": err_msg})
                    parsed = [
                        ParsedSection("parse-error", fpath.name, 1, "error", f"[PARSE_ERROR: {err_msg}]", [], ["PARSE_ERROR"])
                    ]

                mod = Path(rel).parts[0] if len(Path(rel).parts) > 1 else "."

                for tbl in ("sections", "fts_uni", *(("fts_tri",) if self.opts.fts_trigram else ())):
                    conn.execute(f"DELETE FROM {tbl} WHERE doc_path = ?", (rel,))

                conn.execute(
                    "INSERT INTO docs(doc_path, module, version_group, mtime) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(doc_path) DO UPDATE SET module=excluded.module, version_group=excluded.version_group, mtime=excluded.mtime",
                    (rel, mod, _version_group(rel), mtime),
                )

                for idx, s in enumerate(parsed):
                    addr = f"{rel}#{s.section_id}"
                    chash = _sha12(f"{s.heading}\n{s.body}")
                    old_h, old_t = prev_map.get(s.section_id, ("", mtime))
                    conn.execute(
                        "INSERT INTO sections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (addr, rel, s.section_id, idx, s.heading, s.level, s.loc, s.body, chash, old_h, (old_t if old_h == chash else now), ",".join(s.flags)),
                    )
                    conn.execute("INSERT INTO fts_uni VALUES (?, ?, ?, ?)", (addr, rel, s.heading, s.body))
                    if self.opts.fts_trigram:
                        conn.execute("INSERT INTO fts_tri VALUES (?, ?, ?, ?)", (addr, rel, s.heading, s.body))
                    secs_n += 1
                docs_n += 1

            if not partial:
                for dead in set(existing.keys()) - seen:
                    for tbl in ("docs", "sections", "fts_uni", *(("fts_tri",) if self.opts.fts_trigram else ())):
                        conn.execute(f"DELETE FROM {tbl} WHERE doc_path = ?", (dead,))

            groups: Dict[str, List[Tuple[int, float, str]]] = {}
            for r in conn.execute("SELECT doc_path, version_group, mtime FROM docs"):
                demoted_dir = any(pt.lower() in _DEMOTED_DIRS for pt in Path(r["doc_path"]).parts[:-1])
                groups.setdefault(r["version_group"], []).append((0 if demoted_dir else 1, float(r["mtime"]), r["doc_path"]))

            for items in groups.values():
                items.sort(reverse=True)
                win = items[0][2]
                conn.execute("UPDATE docs SET is_latest = 1, superseded_by = '' WHERE doc_path = ?", (win,))
                for _, _, lose in items[1:]:
                    conn.execute("UPDATE docs SET is_latest = 0, superseded_by = ? WHERE doc_path = ?", (win, lose))

        return {
            "indexed_docs": docs_n,
            "indexed_sections": secs_n,
            "unchanged_docs": skipped,
            "failed_docs": failed_docs,
        }

    def list_outline(self, prefix: str) -> Dict[str, Any]:
        where = "WHERE d.doc_path = ? OR d.doc_path LIKE ?" if prefix else ""
        params = [prefix, f"{prefix}/%"] if prefix else []
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT d.doc_path, d.is_latest, d.superseded_by, s.section_id, s.heading, s.level, s.loc "
                f"FROM docs d JOIN sections s ON d.doc_path = s.doc_path {where} ORDER BY d.doc_path, s.ord",
                params,
            ).fetchall()
        docs: Dict[str, Any] = {}
        for r in rows:
            d = docs.setdefault(r["doc_path"], {"is_latest": bool(r["is_latest"]), "superseded_by": r["superseded_by"], "sections": []})
            d["sections"].append({"id": r["section_id"], "heading": r["heading"], "level": r["level"], "loc": r["loc"]})
        return {"doc_count": len(docs), "documents": docs}

    def retrieve(self, query: str, prefix: str, cross_module: bool, max_chars: int, latest_only: bool) -> Dict[str, Any]:
        tokens = [t.replace('"', '""') for t in query.split() if t]
        budget = max(500, min(max_chars, 20_000))
        scores: Dict[str, float] = {}

        with self._conn() as conn:
            for r in conn.execute("SELECT addr, bm25(fts_uni, 0.0, 3.0, 5.0, 1.0) AS rk FROM fts_uni WHERE fts_uni MATCH ? LIMIT 60", (" OR ".join(f'"{t}"*' for t in tokens),)):
                scores[r["addr"]] = scores.get(r["addr"], 0.0) - float(r["rk"])
            if self.opts.fts_trigram and (tri := [f'"{t}"' for t in tokens if len(t) >= 3]):
                for r in conn.execute("SELECT addr, bm25(fts_tri) AS rk FROM fts_tri WHERE fts_tri MATCH ? LIMIT 60", (" OR ".join(tri),)):
                    scores[r["addr"]] = scores.get(r["addr"], 0.0) + max(0.1, -float(r["rk"]))

            if not scores:
                return {
                    "manifest": f'[READER MANIFEST | ZERO_HITS | scope="{prefix or "*"}" | query="{query}" -> outline]',
                    "outline": self.list_outline(prefix)["documents"],
                }

            ph = ",".join("?" for _ in scores)
            sql = f"SELECT s.*, d.module, d.is_latest, d.superseded_by FROM sections s JOIN docs d ON s.doc_path = d.doc_path WHERE s.addr IN ({ph})"
            params: List[Any] = list(scores.keys())
            if prefix and not cross_module:
                sql += " AND (s.doc_path = ? OR s.doc_path LIKE ?)"
                params.extend([prefix, f"{prefix}/%"])
            cands = [dict(r) for r in conn.execute(sql, params).fetchall()]

        demoted, active, ql = [], [], query.lower()
        for c in cands:
            if latest_only and not c["is_latest"]:
                note = f"{c['doc_path']} (superseded by {c['superseded_by']})"
                if note not in demoted:
                    demoted.append(note)
                continue
            c["_sc"] = scores[c["addr"]] + (4.0 if ql in c["heading"].lower() else (2.0 if ql in c["doc_path"].lower() else 0.0))
            active.append(c)
        active.sort(key=lambda x: x["_sc"], reverse=True)

        if cross_module:
            cnts: Dict[str, int] = {}
            active = [c for c in active if (cnts.update({c["module"]: cnts.get(c["module"], 0) + 1}) or cnts[c["module"]] <= 2)]

        inc, omitted, used, flags = [], 0, 0, []
        for c in active:
            if len(inc) >= 7 or (used + len(c["body"]) > budget and inc):
                omitted += 1
                continue
            body = c["body"][:budget] if not inc and len(c["body"]) > budget else c["body"]
            used += len(body)
            flags.extend(f for f in (c["flags"] or "").split(",") if f and f not in flags)
            inc.append({**c, "body": body})

        mode = "CROSS_MODULE" if cross_module else "INTRA_MODULE"
        ml = [f'[READER MANIFEST | mode={mode} | scope="{prefix or "*"}" | query="{query}" | {len(inc)} sections ({used} / {budget} chars)]']
        ml += [f"- INCLUDED[{i}]: {x['addr']} (H{x['level']}: {x['heading']}, {x['loc']}, hash={x['content_hash']})" for i, x in enumerate(inc, 1)]
        if omitted:
            ml.append(f"- OMITTED: {omitted} lower-ranked sections excluded by budget cap")
        ml += [f"- DEMOTED: {d}" for d in demoted[:5]]
        if flags:
            ml.append(f"- FLAGS: [{', '.join(flags)}]")
        ml.append("=" * 80)

        return {
            "manifest": "\n".join(ml),
            "content": "\n\n".join(f"### [{i}] {x['addr']} — {x['heading']}\n{x['body']}" for i, x in enumerate(inc, 1)),
            "sections": [x["addr"] for x in inc],
        }

    def get_section(self, doc_rel: str, section_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM sections WHERE addr = ?", (f"{doc_rel}#{section_id}",)).fetchone()
            return dict(r) if r else None

    @staticmethod
    def format_exact_section(row: Dict[str, Any], expect_hash: str) -> Dict[str, Any]:
        flags = [f for f in (row["flags"] or "").split(",") if f]
        stale = bool(expect_hash and expect_hash != row["content_hash"])
        if stale:
            flags.insert(0, f"STALE(expected={expect_hash}, actual={row['content_hash']})")
        return {"addr": row["addr"], "heading": row["heading"], "loc": row["loc"], "hash": row["content_hash"], "stale": stale, "flags": flags, "body": row["body"]}

    def changed_since(self, since: str) -> Dict[str, Any]:
        s, now = since.lower(), dt.datetime.now(dt.timezone.utc)
        if s.endswith("d") and s[:-1].isdigit():
            cut = (now - dt.timedelta(days=int(s[:-1]))).timestamp()
        elif s.endswith("h") and s[:-1].isdigit():
            cut = (now - dt.timedelta(hours=int(s[:-1]))).timestamp()
        else:
            try:
                cut = dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp()
            except ValueError:
                raise ToolError(f"Invalid 'since': {since!r}") from None
        with self._conn() as conn:
            rows = conn.execute("SELECT addr, heading, loc, content_hash, prev_hash, changed_at FROM sections WHERE changed_at >= ? ORDER BY changed_at DESC LIMIT 200", (cut,)).fetchall()
        return {"count": len(rows), "sections": [dict(r) for r in rows]}