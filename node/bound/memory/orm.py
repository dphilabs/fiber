# fiber.node.bound.memory.orm
"""@desc: Account & node-key bound typed data mapper (sqlite3 + AEAD/HMAC) for user memory."""
from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from fiber.gateway.bridge.action.sign import (
    LOCAL_CIPHER_KEY_PATH,
    _normalize_spiffe_id,
    ensure_node_keys,
)
from xphi.arch.bound.adapter.pta import NodeSigner
from xphi.node.space.bind.resolver import resolve_path

MAX_NOTE_CHARS = 8_000
MAX_SQL_CHARS = 4_000
MAX_REFS_PER_NOTE = 20


class MemoryIntegrityError(RuntimeError):
    """Raised when stored note or reference fails HMAC / AEAD verification."""


def _resolve_default_account() -> str:
    raw = (
        os.environ.get("FIBER_ACCOUNT", "").strip()
        or getpass.getuser().strip()
        or "fiber"
    )
    return _normalize_spiffe_id(raw)


@dataclass(frozen=True)
class MemoryIdentity:
    """TOTP와 무관하게 필수 Account(SPIFFE ID)와 로컬 노드 키(Ed25519 + local_cipher.key)만 바인딩."""

    account: str
    pubkey_hex: str
    db_path: Path
    _domain_key: bytes = field(repr=False)

    @classmethod
    def bind(cls, account: Optional[str] = None, db_path: str = "") -> "MemoryIdentity":
        ensure_node_keys()
        signer = NodeSigner.get_instance()
        pubkey_hex = signer.pubkey_hex

        canonical_account = (
            _normalize_spiffe_id(account.strip())
            if account and account.strip()
            else _resolve_default_account()
        )

        raw_seed = LOCAL_CIPHER_KEY_PATH.read_text(encoding="utf-8").strip()
        ikm = bytes.fromhex(raw_seed) if len(raw_seed) == 64 else raw_seed.encode("utf-8")
        ctx = f"dphi:domain:user_memory:v1:{pubkey_hex}:{canonical_account}".encode("utf-8")
        domain_key = hmac.new(ikm, ctx, hashlib.sha256).digest()

        if db_path and db_path.strip():
            resolved_db = Path(db_path).expanduser().resolve()
            resolved_db.parent.mkdir(parents=True, exist_ok=True)
        else:
            scope_hash = hashlib.sha256(
                f"{canonical_account}:{pubkey_hex}".encode("utf-8")
            ).hexdigest()[:16]
            mem_dir = Path(str(resolve_path("anchor"))) / "memory"
            mem_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(mem_dir, 0o700)
            except OSError:
                pass
            resolved_db = mem_dir / f"{scope_hash}.db"

        return cls(
            account=canonical_account,
            pubkey_hex=pubkey_hex,
            db_path=resolved_db,
            _domain_key=domain_key,
        )

    def sign_note_row(
        self, project: str, key: str, tags: str, note: str, ref_count: int
    ) -> str:
        payload = (
            f"{self.account}\x1f{project}\x1f{key}\x1f{tags}\x1f{ref_count}\x1f{note}"
        ).encode("utf-8")
        return hmac.new(self._domain_key, payload, hashlib.sha256).hexdigest()

    def verify_note_row(
        self, project: str, key: str, tags: str, note: str, ref_count: int, sig: str
    ) -> None:
        expected = self.sign_note_row(project, key, tags, note, ref_count)
        if not sig or not hmac.compare_digest(expected, sig):
            raise MemoryIntegrityError(
                f"Note integrity signature mismatch for ({project}, {key})."
            )

    def seal_ref(self, project: str, key: str, ord_idx: int, spec_json: str) -> str:
        nonce = os.urandom(12)
        aad = f"{self.account}|{project}|{key}|{ord_idx}".encode("utf-8")
        ct = AESGCM(self._domain_key).encrypt(nonce, spec_json.encode("utf-8"), aad)
        return (nonce + ct).hex()

    def open_ref(self, project: str, key: str, ord_idx: int, cipher_hex: str) -> str:
        try:
            raw = bytes.fromhex(cipher_hex)
            if len(raw) < 28:
                raise ValueError("Ciphertext too short.")
            aad = f"{self.account}|{project}|{key}|{ord_idx}".encode("utf-8")
            pt = AESGCM(self._domain_key).decrypt(raw[:12], raw[12:], aad)
            return pt.decode("utf-8")
        except (ValueError, InvalidTag) as exc:
            raise MemoryIntegrityError(
                f"Reference integrity verification failed for ({project}, {key})[#{ord_idx}]."
            ) from exc


@dataclass(frozen=True)
class RefSource:
    path: str
    hash: str = ""


@dataclass(frozen=True)
class RefSpec:
    """base 4.2 참조 규격: kind='section' | 'query'"""

    kind: str                                  # "section" | "query"
    addr: str = ""                             # section: "specs/refund.docx#sec-4"
    hash: str = ""                             # section: 고정 시점 섹션 해시
    sql: str = ""                              # query: "SELECT ..."
    sources: List[RefSource] = field(default_factory=list)  # query 전용

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "RefSpec":
        if not isinstance(raw, dict):
            raise ValueError("Each ref entry must be an object.")
        kind = str(raw.get("kind", "")).strip()
        if kind == "section":
            addr = str(raw.get("addr", "")).strip()
            if not addr or "#" not in addr:
                raise ValueError("Section ref requires 'addr' in '<doc_path>#<section_id>' format.")
            return cls(kind="section", addr=addr, hash=str(raw.get("hash", "")).strip())

        if kind == "query":
            sql = str(raw.get("sql", "")).strip()
            if not sql:
                raise ValueError("Query ref requires non-empty 'sql'.")
            if len(sql) > MAX_SQL_CHARS:
                raise ValueError(f"Query ref 'sql' exceeds {MAX_SQL_CHARS} chars.")
            raw_srcs = raw.get("sources") or []
            if not isinstance(raw_srcs, list) or not raw_srcs:
                raise ValueError("Query ref requires non-empty 'sources' list.")
            srcs = [
                RefSource(path=str(s["path"]).strip(), hash=str(s.get("hash", "")).strip())
                for s in raw_srcs
                if isinstance(s, dict) and str(s.get("path", "")).strip()
            ]
            if not srcs:
                raise ValueError("Query ref 'sources' must contain at least one {'path': ...} object.")
            return cls(kind="query", sql=sql, sources=srcs)

        raise ValueError(f"Unsupported ref kind: {kind!r} (expected 'section' or 'query').")

    def to_dict(self) -> Dict[str, Any]:
        if self.kind == "section":
            return {"kind": "section", "addr": self.addr, "hash": self.hash}
        return {"kind": "query", "sql": self.sql, "sources": [asdict(s) for s in self.sources]}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "RefSpec":
        return cls.from_dict(json.loads(text))


@dataclass(frozen=True)
class NoteRecord:
    key: str
    note: str
    project: str = "default"
    tags: str = ""
    refs: List[RefSpec] = field(default_factory=list)
    account: str = ""
    updated_at: str = ""

    def validate_limits(self) -> None:
        if not self.key.strip():
            raise ValueError("'key' must not be empty.")
        if not self.note.strip():
            raise ValueError("'note' must not be empty.")
        if len(self.note) > MAX_NOTE_CHARS:
            raise ValueError(f"'note' exceeds maximum length ({MAX_NOTE_CHARS} chars).")
        if len(self.refs) > MAX_REFS_PER_NOTE:
            raise ValueError(f"'refs' exceeds maximum count ({MAX_REFS_PER_NOTE}).")

    @property
    def ref_count(self) -> int:
        return len(self.refs)


@dataclass(frozen=True)
class NoteSummary:
    key: str
    project: str
    tags: str
    preview: str
    ref_count: int
    updated_at: str


_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    project TEXT NOT NULL DEFAULT 'default',
    key TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL,
    ref_count INTEGER NOT NULL DEFAULT 0,
    sig TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account, project, key)
);

CREATE TABLE IF NOT EXISTS refs (
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    kind TEXT NOT NULL,
    spec_cipher TEXT NOT NULL,
    PRIMARY KEY (note_id, ord)
);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    key, project, tags, note,
    content='notes', content_rowid='id', tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, key, project, tags, note)
    VALUES (new.id, new.key, new.project, new.tags, new.note);
END;
CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, key, project, tags, note)
    VALUES ('delete', old.id, old.key, old.project, old.tags, old.note);
END;
CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, key, project, tags, note)
    VALUES ('delete', old.id, old.key, old.project, old.tags, old.note);
    INSERT INTO notes_fts(rowid, key, project, tags, note)
    VALUES (new.id, new.key, new.project, new.tags, new.note);
END;
"""


class MemoryStore:
    """Account 스코프 격리, HMAC 행 무결성, 참조 AEAD 봉인을 캡슐화한 최소 데이터 매퍼."""

    def __init__(self, identity: Optional[MemoryIdentity] = None) -> None:
        self.identity = identity or MemoryIdentity.bind()
        self.db_path = self.identity.db_path
        self._init_or_migrate()
        self._enforce_file_perms()

    def _enforce_file_perms(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            p = Path(f"{self.db_path}{suffix}")
            if p.exists():
                try:
                    os.chmod(p, 0o600)
                except OSError:
                    pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_or_migrate(self) -> None:
        with self._connect() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(notes)")}
            # 구버전 단일 키 스키마 감지 시 현재 account 소유로 서명 부여하며 무손실 마이그레이션
            if cols and "sig" not in cols:
                body_col = "content" if "content" in cols else "note"
                old_rows = conn.execute(
                    f"SELECT key, project, tags, {body_col} AS body, updated_at FROM notes"
                ).fetchall()
                conn.executescript(
                    """
                    DROP TRIGGER IF EXISTS notes_ai;
                    DROP TRIGGER IF EXISTS notes_ad;
                    DROP TRIGGER IF EXISTS notes_au;
                    DROP TABLE IF EXISTS notes_fts;
                    DROP TABLE IF EXISTS refs;
                    DROP TABLE IF EXISTS notes;
                    """
                )
                conn.executescript(_SCHEMA_SQL)
                for r in old_rows:
                    proj = (r["project"] or "default").strip() or "default"
                    key = r["key"].strip()
                    tags = (r["tags"] or "").strip()
                    body = r["body"] or ""
                    sig = self.identity.sign_note_row(proj, key, tags, body, 0)
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO notes(account, project, key, tags, note, ref_count, sig, updated_at)
                        VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                        """,
                        (self.identity.account, proj, key, tags, body, sig, r["updated_at"]),
                    )
            else:
                conn.executescript(_SCHEMA_SQL)

    def upsert(self, record: NoteRecord) -> NoteRecord:
        record.validate_limits()
        now = datetime.now(timezone.utc).isoformat()
        proj = record.project.strip() or "default"
        key = record.key.strip()
        tags = record.tags.strip()
        ref_count = len(record.refs)
        sig = self.identity.sign_note_row(proj, key, tags, record.note, ref_count)
        sealed_refs = [
            (idx, r.kind, self.identity.seal_ref(proj, key, idx, r.to_json()))
            for idx, r in enumerate(record.refs)
        ]

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO notes (account, project, key, tags, note, ref_count, sig, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account, project, key) DO UPDATE SET
                    tags = excluded.tags,
                    note = excluded.note,
                    ref_count = excluded.ref_count,
                    sig = excluded.sig,
                    updated_at = excluded.updated_at
                """,
                (self.identity.account, proj, key, tags, record.note, ref_count, sig, now),
            )
            note_id = conn.execute(
                "SELECT id FROM notes WHERE account = ? AND project = ? AND key = ?",
                (self.identity.account, proj, key),
            ).fetchone()["id"]

            conn.execute("DELETE FROM refs WHERE note_id = ?", (note_id,))
            if sealed_refs:
                conn.executemany(
                    "INSERT INTO refs (note_id, ord, kind, spec_cipher) VALUES (?, ?, ?, ?)",
                    [(note_id, ord_idx, kind, cipher) for ord_idx, kind, cipher in sealed_refs],
                )

        self._enforce_file_perms()
        return NoteRecord(
            key=key,
            note=record.note,
            project=proj,
            tags=tags,
            refs=list(record.refs),
            account=self.identity.account,
            updated_at=now,
        )

    def get(self, key: str, project: str = "default") -> Optional[NoteRecord]:
        proj = project.strip() or "default"
        k = key.strip()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, account, project, key, tags, note, ref_count, sig, updated_at
                FROM notes
                WHERE account = ? AND project = ? AND key = ?
                """,
                (self.identity.account, proj, k),
            ).fetchone()
            if row is None:
                return None

            ref_rows = conn.execute(
                "SELECT ord, spec_cipher FROM refs WHERE note_id = ? ORDER BY ord",
                (row["id"],),
            ).fetchall()

        if len(ref_rows) != row["ref_count"]:
            raise MemoryIntegrityError(
                f"Reference count mismatch for ({proj}, {k}): expected {row['ref_count']}, found {len(ref_rows)}."
            )
        self.identity.verify_note_row(
            row["project"], row["key"], row["tags"], row["note"], row["ref_count"], row["sig"]
        )

        refs = [
            RefSpec.from_json(self.identity.open_ref(proj, k, r["ord"], r["spec_cipher"]))
            for r in ref_rows
        ]
        return NoteRecord(
            key=row["key"],
            note=row["note"],
            project=row["project"],
            tags=row["tags"],
            refs=refs,
            account=row["account"],
            updated_at=row["updated_at"],
        )

    def delete(self, key: str, project: str = "default") -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM notes WHERE account = ? AND project = ? AND key = ?",
                (self.identity.account, project.strip() or "default", key.strip()),
            )
            return cur.rowcount > 0

    def search(
        self, tokens: Sequence[str], project: str = "", limit: int = 10
    ) -> List[NoteSummary]:
        clean_tokens = [t.replace('"', '""') for t in tokens if t]
        if not clean_tokens:
            return []
        fts_expr = " OR ".join(f'"{t}"*' for t in clean_tokens)

        clauses = ["notes_fts MATCH ?", "n.account = ?"]
        params: List[Any] = [fts_expr, self.identity.account]
        if project.strip():
            clauses.append("n.project = ?")
            params.append(project.strip())
        params.append(max(1, min(limit, 50)))

        sql = f"""
            SELECT n.key, n.project, n.tags, substr(n.note, 1, 160) AS preview,
                   n.ref_count, n.updated_at
            FROM notes_fts f
            JOIN notes n ON n.id = f.rowid
            WHERE {' AND '.join(clauses)}
            ORDER BY bm25(notes_fts)
            LIMIT ?
        """
        with self._connect() as conn:
            return [
                NoteSummary(
                    key=r["key"],
                    project=r["project"],
                    tags=r["tags"],
                    preview=r["preview"],
                    ref_count=r["ref_count"],
                    updated_at=r["updated_at"],
                )
                for r in conn.execute(sql, params).fetchall()
            ]

    def list_recent(
        self, project: str = "", tag: str = "", limit: int = 30
    ) -> List[NoteSummary]:
        clauses = ["account = ?"]
        params: List[Any] = [self.identity.account]
        if project.strip():
            clauses.append("project = ?")
            params.append(project.strip())
        if tag.strip():
            clauses.append("tags LIKE ?")
            params.append(f"%{tag.strip()}%")
        params.append(max(1, min(limit, 100)))

        sql = f"""
            SELECT key, project, tags, substr(note, 1, 160) AS preview,
                   ref_count, updated_at
            FROM notes
            WHERE {' AND '.join(clauses)}
            ORDER BY updated_at DESC
            LIMIT ?
        """
        with self._connect() as conn:
            return [
                NoteSummary(
                    key=r["key"],
                    project=r["project"],
                    tags=r["tags"],
                    preview=r["preview"],
                    ref_count=r["ref_count"],
                    updated_at=r["updated_at"],
                )
                for r in conn.execute(sql, params).fetchall()
            ]