# fiber.node.worker.memory.pin
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import sys
from typing import Any

from fiber.node.bound.memory.orm import (
    MemoryIdentity,
    MemoryIntegrityError,
    MemoryStore,
    NoteRecord,
)
from fiber.node.bound.memory.resolver import MemoryResolver, encode_pin_value, public_tags
from fiber.node.bound.memory.spec import RefStatus
from xphi.arch.contract.protocol.binder import ToolError, tool
from xphi.arch.contract.protocol.worker import AsyncWorkerProtocol


class PinWorker(AsyncWorkerProtocol):
    def __init__(
        self,
        root: str = ".",
        account: str | None = None,
        db_path: str = "",
    ) -> None:
        identity = MemoryIdentity.bind(account=account, db_path=db_path)
        self.store = MemoryStore(identity=identity)
        self.resolver = MemoryResolver(root=root)
        self._lock = asyncio.Lock()
        super().__init__(agent_name="worker.memory.pin")

    @staticmethod
    def _clean_key_and_project(key: str, project: str) -> tuple[str, str]:
        clean_key = key.strip()
        if not clean_key:
            raise ValueError("'key' must not be empty.")
        return clean_key, (project.strip() or "default")

    def _load_scope_records(self, norm_addrs: list[str], project: str) -> list[NoteRecord]:
        keys = self.store.find_keys_by_targets(norm_addrs, project)
        out: list[NoteRecord] = []
        for k in keys:
            rec = self.store.get(k, project)
            if rec is not None:
                out.append(rec)
        return out

    @tool
    async def set(
        self,
        key: str,
        value: str | dict[str, Any],
        refs: list[dict[str, Any]] | None = None,
        project: str = "default",
        tags: str = "",
    ) -> dict[str, Any]:
        """사용자의 판단·제약(value)과 원본 근거(refs), 또는 대화 기준점 객체를 고정 - 사용자 선언·승인 시에만 호출

        Args:
            key: 프로젝트 내 핀 식별 키 (예: 'refund-rounding', '@origin/main').
            value: 제약 텍스트 또는 대화 기준점 구조화 객체(origin/premises/layer/realignments).
            refs: 원본 참조 목록 (section, query, symbol, file, revision). 선언형 핀은 생략.
            project: 논리 프로젝트 구분자 (기본값 'default').
            tags: 쉼표 구분 사용자 태그.
        """
        clean_key, clean_proj = self._clean_key_and_project(key, project)
        encoded_note, encoded_tags = encode_pin_value(value, tags)
        prepared_refs = await self.resolver.prepare_all(refs or [])
        rec = NoteRecord(
            key=clean_key,
            note=encoded_note,
            project=clean_proj,
            tags=encoded_tags,
            refs=prepared_refs,
        )
        async with self._lock:
            saved = await asyncio.to_thread(self.store.upsert, rec)

        return {
            "key": saved.key,
            "project": saved.project,
            "ref_count": saved.ref_count,
            "updated_at": saved.updated_at,
        }

    @tool
    async def drop(self, key: str, project: str = "default") -> dict[str, Any]:
        """지정한 (project, key) 핀과 연결된 참조를 고정 해제(삭제) - 사용자 승인 대상

        Args:
            key: 삭제할 핀 키.
            project: 논리 프로젝트 구분자.
        """
        clean_key, clean_proj = self._clean_key_and_project(key, project)
        async with self._lock:
            dropped = await asyncio.to_thread(self.store.delete, clean_key, clean_proj)
        return {"dropped": dropped, "key": clean_key, "project": clean_proj}

    @tool
    async def resolve(self, key: str, project: str = "default") -> dict[str, Any]:
        """고정된 핀을 꺼내고 현재 디스크의 문서·데이터·코드 원본을 실시간 대조해 유효성(ok)을 판정

        Args:
            key: 조회할 핀 키.
            project: 논리 프로젝트 구분자.
        """
        clean_key, clean_proj = self._clean_key_and_project(key, project)
        async with self._lock:
            try:
                rec = await asyncio.to_thread(self.store.get, clean_key, clean_proj)
            except MemoryIntegrityError as ie:
                raise ToolError(str(ie)) from None

            if rec is None:
                raise ToolError(f"Pin not found: ({clean_proj}, {clean_key})")
            return await self.resolver.resolve_record(rec)

    @tool
    async def list(
        self,
        project: str = "",
        prefix: str = "",
        query: str = "",
        tag: str = "",
        limit: int = 30,
    ) -> dict[str, Any]:
        """저장된 핀 목록을 키 접두사(prefix), 태그, 또는 본문 키워드(query) 기준으로 탐색

        Args:
            project: 특정 프로젝트로 범위 제한 (빈 문자열이면 전체 프로젝트).
            prefix: 키 접두사 필터 (예: '@origin/', 'pay/').
            query: FTS5 키워드 검색어 (지정 시 BM25 순 정렬).
            tag: 태그 부분 일치 필터.
            limit: 최대 반환 개수 (1~100).
        """
        clean_proj = project.strip()
        clean_prefix = prefix.strip()
        clean_tag = tag.strip()
        tokens = [t for t in query.strip().split() if t]
        capped = max(1, min(limit, 100))
        fetch_limit = min(100, capped * 3) if (tokens and (clean_prefix or clean_tag)) else capped

        async with self._lock:
            if tokens:
                items = await asyncio.to_thread(
                    self.store.search, tokens, clean_proj, fetch_limit
                )
            else:
                items = await asyncio.to_thread(
                    self.store.list_recent, clean_proj, clean_tag, capped, clean_prefix
                )

        filtered: list[dict[str, Any]] = []
        for it in items:
            if clean_prefix and not it.key.startswith(clean_prefix):
                continue
            pub_tags = public_tags(it.tags)
            if clean_tag and clean_tag not in pub_tags:
                continue
            d = asdict(it)
            d["tags"] = pub_tags
            filtered.append(d)
            if len(filtered) >= capped:
                break

        return {"count": len(filtered), "items": filtered}

    @tool
    async def scope(
        self,
        addrs: list[str],
        project: str = "default",
    ) -> dict[str, Any]:
        """대상 주소(심볼·파일·문서 섹션·원천 데이터)를 참조하는 모든 핀을 역방향 조회해 일괄 resolve

        Args:
            addrs: 이번 단계에서 건드릴 대상 주소 목록 (예: ['src/pay/refund.py#RefundService.apply']).
            project: 논리 프로젝트 구분자.
        """
        if not addrs:
            raise ValueError("'addrs' must contain at least one target address.")
        clean_proj = project.strip() or "default"
        norm_addrs = [self.resolver.normalize_scope_addr(a) for a in addrs]

        async with self._lock:
            try:
                records = await asyncio.to_thread(
                    self._load_scope_records, norm_addrs, clean_proj
                )
            except MemoryIntegrityError as ie:
                raise ToolError(str(ie)) from None

            resolved_pins: list[dict[str, Any]] = []
            blocked_refs: list[dict[str, Any]] = []
            for rec in records:
                resolved = await self.resolver.resolve_record(rec)
                resolved_pins.append(resolved)
                for r in resolved["refs"]:
                    if r.get("status") != RefStatus.OK:
                        blocked_refs.append({"pin_key": rec.key, **r})

        base_rev = await self.resolver.current_head_rev()
        return {
            "pins": resolved_pins,
            "blocked_refs": blocked_refs,
            "base_rev": base_rev,
            "blocked": len(blocked_refs) > 0,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--account", default=None)
    parser.add_argument("--db", default="")
    args = parser.parse_args()
    worker = PinWorker(root=args.root, account=args.account, db_path=args.db)
    try:
        asyncio.run(worker.serve_forever_async())
    except KeyboardInterrupt:
        sys.exit(0)

if __name__ == "__main__":
    main()