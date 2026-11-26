# fiber.node.worker.reader
from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from pathlib import Path
from typing import Any, Dict, List

from xphi.arch.contract.protocol.binder import ToolError, tool
from xphi.arch.contract.protocol.worker import AsyncWorkerProtocol
from xphi.node.space.bind.resolver import resolve_path

from fiber.node.bound.reader.parsers import parse_document
from fiber.node.bound.reader.runtime import ReaderRuntimeOpts
from fiber.node.bound.reader.store import CorpusStore
from fiber.node.bound.reader.tables import compare_section_table

ANCHOR_ROOT = resolve_path("anchor")

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
        super().__init__(agent_name="worker.reader")

    def _resolve_rel(self, rel: str, *, must_exist: bool = True) -> Path:
        target = (self.root / rel).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("Path is outside the workspace root.")
        if must_exist and not target.exists():
            raise ToolError(f"Not found: {rel}")
        return target

    @tool
    async def sync_corpus(self, subpath: str = "") -> Dict[str, Any]:
        """작업 루트(또는 하위 경로) 문서를 증분 색인하고 구버전 문서를 판별합니다."""
        base = self._resolve_rel(subpath.strip()) if subpath.strip() else self.root
        async with self._lock:
            return await asyncio.to_thread(self.store.sync, base, bool(subpath.strip()))

    @tool
    async def list_modules(self, subpath: str = "") -> Dict[str, Any]:
        """본문 없이 폴더별 문서 목록과 섹션 목차(H1~H3) 트리만 반환합니다."""
        prefix = subpath.strip().strip("/")
        if self.store.is_empty():
            await self.sync_corpus(prefix)
        return await asyncio.to_thread(self.store.list_outline, prefix)

    @tool
    async def retrieve_scoped(
        self,
        query: str,
        scope_prefix: str = "",
        cross_module: bool = False,
        max_chars: int = 4000,
        latest_only: bool = True,
    ) -> Dict[str, Any]:
        """지정 범위 내에서 질의와 일치하는 최신 섹션만 예산 안에서 MANIFEST와 함께 반환합니다."""
        if not query.strip():
            raise ToolError("Query must not be empty.")
        prefix = scope_prefix.strip().strip("/")
        if self.store.is_empty():
            await self.sync_corpus(prefix)
        return await asyncio.to_thread(
            self.store.retrieve, query.strip(), prefix, cross_module, max_chars, latest_only
        )

    @tool
    async def read_exact_section(
        self, doc_path: str, section_id: str, expect_hash: str = ""
    ) -> Dict[str, Any]:
        """현재 디스크 원본 기준으로 지정 섹션 원문을 반환하며, expect_hash 불일치 시 STALE을 표시합니다."""
        target = self._resolve_rel(doc_path)
        rel = target.relative_to(self.root).as_posix()
        sec_id = section_id.strip().lstrip("#")

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
        """지정 기간('7d', '24h', 'YYYY-MM-DD') 내 해시가 바뀐 섹션 주소 목록을 반환합니다."""
        return await asyncio.to_thread(self.store.changed_since, since.strip())

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
        """문서 섹션의 표를 자체 검증한 뒤 원천 데이터(.csv, .xlsx, .jsonl, .parquet)와 대조합니다."""
        if not keys:
            raise ToolError("'keys' must contain at least one column name.")
        doc_file = self._resolve_rel(doc_path)
        src_file = self._resolve_rel(source)

        def _exec() -> Dict[str, Any]:
            secs = parse_document(doc_file, self.opts)
            rel_doc = doc_file.relative_to(self.root).as_posix()
            rel_src = src_file.relative_to(self.root).as_posix()
            return compare_section_table(
                secs, rel_doc, section_id, src_file, rel_src, keys, tolerance, table_index
            )

        return await asyncio.to_thread(_exec)


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