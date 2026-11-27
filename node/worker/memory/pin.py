# fiber.node.worker.memory.pin
from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stdout
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
        # MemoryIdentity 초기화 중 커널 로거가 stdout(JSON-RPC 파이프)으로 로그를 흘리는 현상 방지
        with redirect_stdout(sys.stderr):
            identity = MemoryIdentity.bind(account=account, db_path=db_path)
            self.store = MemoryStore(identity=identity)
            self.resolver = MemoryResolver(root=root)
        self._lock = asyncio.Lock()
        super().__init__(agent_name="worker.memory.pin")

    @staticmethod
    def _clean_key_and_project(key: str, project: str) -> tuple[str, str]:
        clean_key = key.strip()
        if not clean_key:
            raise ToolError("'key' must not be empty.")
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
        """Pin a constraint, decision, or anchor object with source refs. Call only upon explicit user declaration or approval.

        Args:
            key: Pin identifier within the project (e.g., 'refund-rounding', '@origin/main').
            value: Constraint text or structured anchor dict (origin/premises/layer/realignments).
            refs: Source references (section, query, symbol, file, revision). Omit for declarative pins.
            project: Logical project namespace (default 'default').
            tags: Comma-separated user tags.
        """
        clean_key, clean_proj = self._clean_key_and_project(key, project)
        try:
            encoded_note, encoded_tags = encode_pin_value(value, tags)
            prepared_refs = await self.resolver.prepare_all(refs or [])
        except (ValueError, MemoryIntegrityError) as exc:
            raise ToolError(str(exc)) from None

        rec = NoteRecord(
            key=clean_key,
            note=encoded_note,
            project=clean_proj,
            tags=encoded_tags,
            refs=prepared_refs,
        )
        async with self._lock:
            try:
                saved = await asyncio.to_thread(self.store.upsert, rec)
            except (ValueError, MemoryIntegrityError) as exc:
                raise ToolError(str(exc)) from None

        return {
            "key": saved.key,
            "project": saved.project,
            "ref_count": saved.ref_count,
            "updated_at": saved.updated_at,
        }

    @tool
    async def drop(self, key: str, project: str = "default") -> dict[str, Any]:
        """Delete a pin and its linked references. Requires user approval.

        Args:
            key: Pin key to delete.
            project: Logical project namespace.
        """
        clean_key, clean_proj = self._clean_key_and_project(key, project)
        async with self._lock:
            dropped = await asyncio.to_thread(self.store.delete, clean_key, clean_proj)
        return {"dropped": dropped, "key": clean_key, "project": clean_proj}

    @tool
    async def resolve(self, key: str, project: str = "default") -> dict[str, Any]:
        """Fetch a pin and verify its references live against on-disk docs, data, and code.

        Args:
            key: Pin key to resolve.
            project: Logical project namespace.
        """
        clean_key, clean_proj = self._clean_key_and_project(key, project)
        async with self._lock:
            try:
                rec = await asyncio.to_thread(self.store.get, clean_key, clean_proj)
                if rec is None:
                    raise ToolError(f"Pin not found: ({clean_proj}, {clean_key})")
                return await self.resolver.resolve_record(rec)
            except (ValueError, MemoryIntegrityError) as exc:
                raise ToolError(str(exc)) from None

    @tool
    async def list(
        self,
        project: str = "",
        prefix: str = "",
        query: str = "",
        tag: str = "",
        limit: int = 30,
    ) -> dict[str, Any]:
        """Search stored pins by key prefix, tag, or full-text query.

        Args:
            project: Filter by project (empty for all projects).
            prefix: Key prefix filter (e.g., '@origin/', 'pay/').
            query: FTS5 keyword query (ranked by BM25 when provided).
            tag: Substring tag filter.
            limit: Max items to return (1-100).
        """
        clean_proj = project.strip()
        clean_prefix = prefix.strip()
        clean_tag = tag.strip().lower()
        tokens = [t for t in query.strip().split() if t]
        capped = max(1, min(limit, 100))
        fetch_limit = min(200, max(capped * 3, 50)) if (clean_prefix or clean_tag) else capped

        async with self._lock:
            try:
                if tokens:
                    items = await asyncio.to_thread(
                        self.store.search, tokens, clean_proj, fetch_limit
                    )
                else:
                    # DB 레벨 태그 필터와의 미묘한 불일치(#12)를 피하기 위해 빈 태그로 조회 후 public_tags 기준 일관 필터링
                    items = await asyncio.to_thread(
                        self.store.list_recent, clean_proj, "", fetch_limit, clean_prefix
                    )
            except (ValueError, MemoryIntegrityError) as exc:
                raise ToolError(str(exc)) from None

        filtered: list[dict[str, Any]] = []
        for it in items:
            if clean_prefix and not it.key.startswith(clean_prefix):
                continue
            pub_tags = public_tags(it.tags)
            pub_tags_str = ",".join(pub_tags) if isinstance(pub_tags, (list, tuple, set)) else str(pub_tags)
            if clean_tag and clean_tag not in pub_tags_str.lower():
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
        """Reverse-lookup and resolve all pins referencing the target addresses (symbols, files, sections, or data).

        Args:
            addrs: Target addresses to touch in this step (e.g., ['src/pay/refund.py#RefundService.apply']).
            project: Logical project namespace.
        """
        clean_addrs = [a.strip() for a in (addrs or []) if a and a.strip()]
        if not clean_addrs:
            raise ToolError("'addrs' must contain at least one target address.")
        clean_proj = project.strip() or "default"

        try:
            norm_addrs = [self.resolver.normalize_scope_addr(a) for a in clean_addrs]
        except ValueError as exc:
            raise ToolError(str(exc)) from None

        async with self._lock:
            try:
                records = await asyncio.to_thread(
                    self._load_scope_records, norm_addrs, clean_proj
                )
                resolved_pins: list[dict[str, Any]] = []
                blocked_refs: list[dict[str, Any]] = []
                for rec in records:
                    resolved = await self.resolver.resolve_record(rec)
                    resolved_pins.append(resolved)
                    for r in resolved["refs"]:
                        if r.get("status") != RefStatus.OK:
                            blocked_refs.append({"pin_key": rec.key, **r})
                # 잠금 내부에서 base_rev를 함께 읽어 참조 검증 시점과 정합성 보장 (#12)
                base_rev = await self.resolver.current_head_rev()
            except (ValueError, MemoryIntegrityError) as exc:
                raise ToolError(str(exc)) from None

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