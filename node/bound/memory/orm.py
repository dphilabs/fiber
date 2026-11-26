# fiber.node.bound.memory.orm
"""@desc: SQLite + AEAD/HMAC data mapper with blind reverse-lookup index for user pins."""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from typing import Any

from fiber.node.bound.memory.spec import (
    MAX_NOTE_CHARS,
    MAX_REFS_PER_NOTE,
    MAX_SQL_CHARS,
    MemoryIdentity,
    MemoryIntegrityError,
    NoteRecord,
    NoteSummary,
    RefSource,
    RefSpec,
)

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

CREATE TABLE IF NOT EXISTS ref_targets (
    note_id INTEGER NOT NULL,
    ord INTEGER NOT NULL,
    target_hmac TEXT NOT NULL,
    PRIMARY KEY (note_id, ord, target_hmac),
    FOREIGN KEY (note_id, ord) REFERENCES refs(note_id, ord) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ref_targets_hmac ON ref_targets(target_hmac);

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
    """Account 스코프 격리, HMAC 행 무결성, 참조 AEAD 봉인, 역방향 블라인드 색인을 캡슐화한 매퍼."""

    def __init__(self, identity: MemoryIdentity | None = None) -> None:
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

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON;")
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_or_migrate(self) -> None:
        with self._connect() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(notes)")}
            had_ref_targets = bool(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ref_targets'"
                ).fetchone()
            )

            # 구버전(서명 도입 전) 단일 키 스키마 감지 시 무손실 마이그레이션
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
                    DROP TABLE IF EXISTS ref_targets;
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
                # v1 DB(refs는 있고 ref_targets가 없던 상태)에서 업그레이드 시 역방향 색인 1회 자동 백필
                if not had_ref_targets:
                    self._backfill_ref_targets(conn)

    def _backfill_ref_targets(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT n.id AS note_id, n.project, n.key, r.ord, r.spec_cipher
            FROM refs r
            JOIN notes n ON n.id = r.note_id
            WHERE n.account = ?
            """,
            (self.identity.account,),
        ).fetchall()

        target_rows: list[tuple[int, int, str]] = []
        for r in rows:
            try:
                spec_json = self.identity.open_ref(
                    r["project"], r["key"], r["ord"], r["spec_cipher"]
                )
                spec = RefSpec.from_json(spec_json)
            except (MemoryIntegrityError, ValueError):
                continue
            for addr in spec.target_addrs():
                thmac = self.identity.blind_target(r["project"], addr)
                target_rows.append((r["note_id"], r["ord"], thmac))

        if target_rows:
            conn.executemany(
                "INSERT OR IGNORE INTO ref_targets (note_id, ord, target_hmac) VALUES (?, ?, ?)",
                target_rows,
            )

    def upsert(self, record: NoteRecord) -> NoteRecord:
        record.validate_limits()
        now = datetime.now(timezone.utc).isoformat()
        proj = record.project.strip() or "default"
        key = record.key.strip()
        tags = record.tags.strip()
        ref_count = len(record.refs)
        sig = self.identity.sign_note_row(proj, key, tags, record.note, ref_count)

        sealed_refs: list[tuple[int, str, str]] = []
        target_entries: list[tuple[int, str]] = []
        for idx, r in enumerate(record.refs):
            sealed_refs.append((idx, r.kind, self.identity.seal_ref(proj, key, idx, r.to_json())))
            for addr in r.target_addrs():
                target_entries.append((idx, self.identity.blind_target(proj, addr)))

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

            # ON DELETE CASCADE로 기존 ref_targets도 함께 정리됨
            conn.execute("DELETE FROM refs WHERE note_id = ?", (note_id,))
            if sealed_refs:
                conn.executemany(
                    "INSERT INTO refs (note_id, ord, kind, spec_cipher) VALUES (?, ?, ?, ?)",
                    [(note_id, ord_idx, kind, cipher) for ord_idx, kind, cipher in sealed_refs],
                )
            if target_entries:
                conn.executemany(
                    "INSERT OR IGNORE INTO ref_targets (note_id, ord, target_hmac) VALUES (?, ?, ?)",
                    [(note_id, ord_idx, thmac) for ord_idx, thmac in target_entries],
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

    def get(self, key: str, project: str = "default") -> NoteRecord | None:
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

    def find_keys_by_targets(
        self, norm_addrs: Sequence[str], project: str = "default"
    ) -> list[str]:
        """정규화된 대상 주소 목록을 참조하는 핀 key 목록을 결정적 순서로 조회합니다 (scope 전용)."""
        proj = project.strip() or "default"
        hmacs = list(
            dict.fromkeys(
                self.identity.blind_target(proj, a.strip()) for a in norm_addrs if a and a.strip()
            )
        )
        if not hmacs:
            return []

        placeholders = ", ".join("?" for _ in hmacs)
        sql = f"""
            SELECT DISTINCT n.key
            FROM ref_targets rt
            JOIN notes n ON n.id = rt.note_id
            WHERE n.account = ?
              AND n.project = ?
              AND rt.target_hmac IN ({placeholders})
            ORDER BY n.key ASC
        """
        with self._connect() as conn:
            rows = conn.execute(sql, [self.identity.account, proj, *hmacs]).fetchall()
        return [r["key"] for r in rows]

    def delete(self, key: str, project: str = "default") -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM notes WHERE account = ? AND project = ? AND key = ?",
                (self.identity.account, project.strip() or "default", key.strip()),
            )
            return cur.rowcount > 0

    def search(
        self, tokens: Sequence[str], project: str = "", limit: int = 10
    ) -> list[NoteSummary]:
        clean_tokens = [t.replace('"', '""') for t in tokens if t]
        if not clean_tokens:
            return []
        fts_expr = " OR ".join(f'"{t}"*' for t in clean_tokens)

        clauses = ["notes_fts MATCH ?", "n.account = ?"]
        params: list[Any] = [fts_expr, self.identity.account]
        if project.strip():
            clauses.append("n.project = ?")
            params.append(project.strip())
        params.append(max(1, min(limit, 100)))

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
        self,
        project: str = "",
        tag: str = "",
        limit: int = 30,
        prefix: str = "",
    ) -> list[NoteSummary]:
        clauses = ["account = ?"]
        params: list[Any] = [self.identity.account]
        if project.strip():
            clauses.append("project = ?")
            params.append(project.strip())
        if prefix.strip():
            escaped_pfx = (
                prefix.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            clauses.append("key LIKE ? ESCAPE '\\'")
            params.append(f"{escaped_pfx}%")
        if tag.strip():
            clauses.append("tags LIKE ?")
            params.append(f"%{tag.strip()}%")
        params.append(max(1, min(limit, 100)))

        sql = f"""
            SELECT key, project, tags, substr(note, 1, 160) AS preview, ref_count, updated_at
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