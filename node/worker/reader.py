# fiber.node.worker.reader
from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from pathlib import Path
from typing import Any, Dict, List, Set

from fiber.node.bound.reader.parsers import parse_document
from fiber.node.bound.reader.runtime import ReaderRuntimeOpts
from fiber.node.bound.reader.store import CorpusStore
from fiber.node.bound.reader.tables import compare_section_table
from xphi.arch.contract.protocol.binder import ToolError, tool
from xphi.arch.contract.protocol.worker import AsyncWorkerProtocol
from xphi.node.space.bind.resolver import resolve_path

ANCHOR_ROOT = resolve_path("anchor")
MIN_BUDGET_CHARS = 200
MAX_BUDGET_CHARS = 50_000


class ReaderWorker(AsyncWorkerProtocol):
    def __init__(self, root: str = ".", db_path: str = "") -> None:
        self.root = Path(root).expanduser().resolve()
        root_id = hashlib.sha256(self.root.as_posix().encode()).hexdigest()[:12]
        resolved_db = (
            Path(db_path).expanduser().resolve()
            if db_path
            else ANCHOR_ROOT / "memory" / "reader" / f"{root_id}.db"
        )
        self.opts = ReaderRuntimeOpts.from_daemon_env()
        self.store = CorpusStore(self.root, resolved_db, self.opts)
        self._lock = asyncio.Lock()
        self._full_synced: bool = False
        self._synced_prefixes: Set[str] = set()
        super().__init__(agent_name="worker.reader")

    def _resolve_rel(self, rel: str, *, must_exist: bool = True) -> Path:
        clean = rel.strip()
        if not clean:
            raise ToolError("Path must not be empty.")
        target = (self.root / clean).resolve()
        if not target.is_relative_to(self.root):
            raise ToolError("Path is outside the workspace root.")
        if must_exist and not target.exists():
            raise ToolError(f"Not found: {rel}")
        return target

    def _is_scope_covered(self, norm_prefix: str) -> bool:
        if self._full_synced:
            return True
        if not norm_prefix:
            return False
        return any(
            norm_prefix == p or norm_prefix.startswith(f"{p}/")
            for p in self._synced_prefixes
        )

    async def _ensure_scope_synced(self, prefix: str, *, cross_module: bool = False) -> None:
        """첫 호출에서 일부 하위 경로만 동기화된 뒤 전체 인덱스가 누락되는 문제(#6)와 이벤트 루프 블로킹(#13)을 함께 방지합니다."""
        target_prefix = "" if cross_module else prefix.strip().strip("/")
        is_empty = await asyncio.to_thread(self.store.is_empty)
        if is_empty or not self._is_scope_covered(target_prefix):
            await self.sync_corpus(target_prefix)

    @tool
    async def sync_corpus(self, subpath: str = "") -> Dict[str, Any]:
        """Incrementally index documents under workspace root (or subpath) and detect stale entries.

        Args:
            subpath: Subdirectory relative to workspace root (empty for full workspace).
        """
        clean_sub = subpath.strip().strip("/")
        base = self._resolve_rel(clean_sub) if clean_sub else self.root
        rel_norm = base.relative_to(self.root).as_posix() if base != self.root else ""
        is_partial = bool(rel_norm and rel_norm != ".")

        async with self._lock:
            try:
                res = await asyncio.to_thread(self.store.sync, base, is_partial)
            except ValueError as exc:
                raise ToolError(str(exc)) from None

            if not is_partial:
                self._full_synced = True
            elif base.is_dir():
                self._synced_prefixes.add(rel_norm)
            return res

    @tool
    async def list_modules(self, subpath: str = "") -> Dict[str, Any]:
        """Return folder-grouped document lists and H1-H3 section outlines without body text.

        Args:
            subpath: Subdirectory prefix to filter the outline (empty for all).
        """
        prefix = subpath.strip().strip("/")
        if prefix:
            self._resolve_rel(prefix)
        await self._ensure_scope_synced(prefix)
        try:
            return await asyncio.to_thread(self.store.list_outline, prefix)
        except ValueError as exc:
            raise ToolError(str(exc)) from None

    @tool
    async def retrieve_scoped(
        self,
        query: str,
        scope_prefix: str = "",
        cross_module: bool = False,
        max_chars: int = 4000,
        latest_only: bool = True,
    ) -> Dict[str, Any]:
        """Retrieve matching sections within scope and character budget, including a MANIFEST.

        Args:
            query: Search query string.
            scope_prefix: Path prefix to restrict search scope.
            cross_module: Allow matches outside scope_prefix if True.
            max_chars: Maximum character budget for returned content.
            latest_only: Exclude superseded document versions if True.
        """
        if not query.strip():
            raise ToolError("Query must not be empty.")
        prefix = scope_prefix.strip().strip("/")
        if prefix:
            self._resolve_rel(prefix, must_exist=False)
        capped_chars = max(MIN_BUDGET_CHARS, min(max_chars, MAX_BUDGET_CHARS))

        await self._ensure_scope_synced(prefix, cross_module=cross_module)
        try:
            return await asyncio.to_thread(
                self.store.retrieve, query.strip(), prefix, cross_module, capped_chars, latest_only
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from None

    @tool
    async def read_exact_section(
        self, doc_path: str, section_id: str, expect_hash: str = ""
    ) -> Dict[str, Any]:
        """Read raw section text from disk, flagging STALE if expect_hash mismatches.

        Args:
            doc_path: Document path relative to workspace root.
            section_id: Target section identifier (with or without leading '#').
            expect_hash: Expected content hash to verify freshness.
        """
        sec_id = section_id.strip().lstrip("#")
        if not sec_id:
            raise ToolError("'section_id' must not be empty.")
        target = self._resolve_rel(doc_path)
        if not target.is_file():
            raise ToolError(f"Document is not a file: {doc_path}")
        rel = target.relative_to(self.root).as_posix()

        disk_mtime = target.stat().st_mtime
        db_mtime = await asyncio.to_thread(self.store.get_doc_mtime, rel)
        if db_mtime is None or abs(db_mtime - disk_mtime) >= 1e-3:
            await self.sync_corpus(rel)

        row = await asyncio.to_thread(self.store.get_section, rel, sec_id)
        if row is None:
            raise ToolError(f"Section not found: {rel}#{sec_id}")
        return self.store.format_exact_section(row, expect_hash.strip())

    @tool
    async def changed_sections(self, since: str = "7d") -> Dict[str, Any]:
        """List section addresses whose content hash changed within the given window.

        Args:
            since: Lookback window or date ('7d', '24h', or 'YYYY-MM-DD').
        """
        clean_since = since.strip()
        if not clean_since:
            raise ToolError("'since' must not be empty.")
        try:
            return await asyncio.to_thread(self.store.changed_since, clean_since)
        except ValueError as exc:
            raise ToolError(str(exc)) from None

    @tool
    async def compare_table(
        self,
        doc_path: str,
        section_id: str,
        source: str,
        keys: List[str],
        tolerance: float = 0.0,
        table_index: int = 0,
    ) -> Dict[str, Any]:
        """Validate a document section table and diff it against a source data file (.csv, .xlsx, .jsonl, .parquet).

        Args:
            doc_path: Document path relative to workspace root.
            section_id: Section identifier containing the target table.
            source: Source data file path relative to workspace root.
            keys: Key column names used to join rows.
            tolerance: Allowed numeric diff tolerance.
            table_index: Zero-based table index within the section.
        """
        if not keys or not any(k.strip() for k in keys):
            raise ToolError("'keys' must contain at least one column name.")
        if tolerance < 0:
            raise ToolError("'tolerance' must be >= 0.")
        if table_index < 0:
            raise ToolError("'table_index' must be >= 0.")

        doc_file = self._resolve_rel(doc_path)
        src_file = self._resolve_rel(source)
        if not doc_file.is_file():
            raise ToolError(f"Document is not a file: {doc_path}")
        if not src_file.is_file():
            raise ToolError(f"Source is not a file: {source}")

        def _exec() -> Dict[str, Any]:
            secs = parse_document(doc_file, self.opts)
            rel_doc = doc_file.relative_to(self.root).as_posix()
            rel_src = src_file.relative_to(self.root).as_posix()
            return compare_section_table(
                secs, rel_doc, section_id.strip().lstrip("#"), src_file, rel_src, keys, tolerance, table_index
            )

        try:
            return await asyncio.to_thread(_exec)
        except ValueError as exc:
            raise ToolError(str(exc)) from None

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--db", default="")
    args = parser.parse_args()
    worker = ReaderWorker(root=args.root, db_path=args.db)
    try:
        asyncio.run(worker.serve_forever_async())
    except KeyboardInterrupt:
        sys.exit(0)

if __name__ == "__main__":
    main()