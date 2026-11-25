# fiber.studio.runbook.unit.indexer
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from xphi.arch.reflex.flow import (
    UnitContract,
    collect_header_flows,
    expand_entries,
    is_flow_meta,
)
from xphi.arch.reflex.grammar.header import parse_unit_header_meta, prefix_target_clause
from xphi.arch.reflex.script import has_pep723
from xphi.arch.contract.registry.flow import FlowRegistry

from fiber.studio.runbook.stepper import emit_unit_dsl
from fiber.studio.runbook.unit.loader import UnitsLoader

_HEADER_SCAN_BYTES = 8192
_SKIP_SCAN_DIRS = frozenset({"__pycache__", "fixtures", "docs", "io", "build", "dist"})
_SKIP_FLOW_JSONS = frozenset({"scenarios.json"})


class UnitsIndexer:
    INDEX_FILENAME = "index.json"

    def __init__(self, loader: UnitsLoader) -> None:
        self.loader = loader

    @staticmethod
    def parse_script_header(raw_bytes: bytes, rel_path: str = "", ref_prefix: str = "") -> dict[str, Any] | None:
        """스크립트 상단 주석(@unit, @flow/@flow.<name>, @desc, @side_effect)에서 계약 원본을 파싱한다."""
        head = raw_bytes[:_HEADER_SCAN_BYTES].decode("utf-8", errors="replace")
        meta = parse_unit_header_meta(head)
        if meta is None:
            return None

        rewrite = (lambda s: prefix_target_clause(s, ref_prefix)) if ref_prefix else (lambda s: s)
        flow_meta = collect_header_flows(head, rewrite)
        if not flow_meta:
            return None

        return {
            "unit": meta.unit,
            **flow_meta,
            "desc": meta.desc or f"Unit {meta.unit} ({rel_path})",
            "isolated": has_pep723(head),
            "side_effect": meta.side_effect,
        }

    @classmethod
    def build_repo_index(cls, repo_root: str | Path, ref_prefix: str = "") -> dict[str, Any]:
        """파일 탐색 가속용 위치/해시 인덱스만 생성한다 (스크립트 계약 문자열은 복사하지 않음)."""
        root = Path(repo_root)
        units_manifest: dict[str, dict[str, Any]] = {}
        flows_manifest: dict[str, str] = {}

        # 1. Scan executable `.py` and `.sh`/`.bash` unit scripts (SSOT: path + sha256 locator only)
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel_parts = path.relative_to(root).parts
            if any(p.startswith(".") or p in _SKIP_SCAN_DIRS for p in rel_parts):
                continue
            if rel_parts[:2] == ("units", "adapters"):
                continue
            if path.suffix not in (".py", ".sh", ".bash") or path.name == "__init__.py":
                continue

            rel_path = path.relative_to(root).as_posix()
            raw_bytes = path.read_bytes()
            hdr = cls.parse_script_header(raw_bytes, rel_path, ref_prefix=ref_prefix)
            if not hdr:
                continue

            u_name = hdr["unit"]
            if u_name in units_manifest:
                prev = units_manifest[u_name].get("path") or units_manifest[u_name].get("source")
                raise ValueError(f"Duplicate @unit '{u_name}' declared in '{rel_path}' and '{prev}'.")

            entry: dict[str, Any] = {"path": rel_path, "sha256": hashlib.sha256(raw_bytes).hexdigest()}
            if path.suffix == ".py":
                entry["isolated"] = hdr["isolated"]
            if hdr["side_effect"]:
                entry["side_effect"] = hdr["side_effect"]
            units_manifest[u_name] = entry

        # 2. Recursively scan `flow/**/*.json` for domain flow specs & delegated units
        flow_dir = root / "flow"
        if flow_dir.is_dir():
            for spec_path in sorted(flow_dir.rglob("*.json")):
                rel_flow_parts = spec_path.relative_to(flow_dir).parts
                if any(p.startswith(".") for p in rel_flow_parts) or spec_path.name in _SKIP_FLOW_JSONS:
                    continue

                rel_spec = spec_path.relative_to(root).as_posix()
                try:
                    spec_data = json.loads(spec_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid JSON in flow spec '{rel_spec}': {exc}") from exc

                if not isinstance(spec_data, dict):
                    continue

                for u_name, u_meta in (spec_data.get("units") or {}).items():
                    if u_name not in units_manifest and isinstance(u_meta, dict) and is_flow_meta(u_meta):
                        units_manifest[u_name] = {"source": rel_spec}

                for f_name, f_meta in (spec_data.get("flows") or {}).items():
                    if is_flow_meta(f_meta):
                        if f_name in flows_manifest:
                            raise ValueError(
                                f"Duplicate flow '{f_name}' in '{rel_spec}' and '{flows_manifest[f_name]}'."
                            )
                        flows_manifest[f_name] = rel_spec

        result: dict[str, Any] = {"version": "1.0", "units": units_manifest}
        if flows_manifest:
            result["flows"] = flows_manifest
        return result

    @classmethod
    def write_repo_index(cls, repo_root: str | Path, ref_prefix: str = "") -> Path:
        root = Path(repo_root)
        data = cls.build_repo_index(root, ref_prefix=ref_prefix)
        out_path = root / cls.INDEX_FILENAME
        out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return out_path

    def load_catalog_index(self, ref: str | None = None, *, force_refresh: bool = False) -> dict[str, Any]:
        target_ref = ref or self.loader.default_ref
        if self.loader.local_repo_dir and self.loader.local_repo_dir.is_dir():
            local_idx = self.loader.local_repo_dir / self.INDEX_FILENAME
            if local_idx.is_file() and not force_refresh:
                index_data = json.loads(local_idx.read_text(encoding="utf-8"))
            else:
                index_data = self.build_repo_index(self.loader.local_repo_dir)
        else:
            cached_idx = self.loader.cache_dir / target_ref / self.INDEX_FILENAME
            if cached_idx.is_file() and not force_refresh:
                index_data = json.loads(cached_idx.read_text(encoding="utf-8"))
            else:
                raw_bytes = self.loader.fetch_raw_file(target_ref, self.INDEX_FILENAME)
                cached_idx.parent.mkdir(parents=True, exist_ok=True)
                cached_idx.write_bytes(raw_bytes)
                index_data = json.loads(raw_bytes.decode("utf-8"))

        hash_locks = {
            meta["path"]: meta["sha256"]
            for meta in index_data.get("units", {}).values()
            if isinstance(meta, dict) and "path" in meta and "sha256" in meta
        }
        self.loader.update_hash_locks(hash_locks)
        return index_data

    def sync_into_registry(
        self,
        registry: FlowRegistry,
        ref: str | None = None,
        *,
        force_refresh: bool = False,
    ) -> int:
        """index.json으로 파일 위치만 찾고, 실제 계약은 원본 스크립트 주석과 flow 파일에서 직접 읽는다."""
        target_ref = ref or self.loader.default_ref
        idx = self.load_catalog_index(ref=ref, force_refresh=force_refresh)
        spec_cache: dict[str, dict[str, Any]] = {}

        def _read_spec(rel_spec: str) -> dict[str, Any]:
            if rel_spec not in spec_cache:
                fp = self.loader.resolve_file(f"{target_ref}@{rel_spec}" if ref else rel_spec)
                spec_cache[rel_spec] = json.loads(fp.read_text(encoding="utf-8"))
            return spec_cache[rel_spec]

        count = 0
        for u_name, u_meta in idx.get("units", {}).items():
            if "path" in u_meta:
                fp = self.loader.resolve_file(f"{target_ref}@{u_meta['path']}" if ref else u_meta["path"])
                hdr = self.parse_script_header(fp.read_bytes(), u_meta["path"], ref_prefix=ref or "")
                if hdr:
                    for key, spec, desc in expand_entries(u_name, hdr):
                        registry.register_unit(key, spec, desc)
                        count += 1
            elif "source" in u_meta:
                d_meta = (_read_spec(u_meta["source"]).get("units") or {}).get(u_name)
                if is_flow_meta(d_meta):
                    for key, spec, desc in expand_entries(u_name, d_meta):
                        registry.register_unit(key, spec, desc)
                        count += 1

        for f_name, f_entry in idx.get("flows", {}).items():
            rel_spec = f_entry if isinstance(f_entry, str) else f_entry.get("source", "")
            if not rel_spec:
                continue
            f_meta = (_read_spec(rel_spec).get("flows") or {}).get(f_name)
            if is_flow_meta(f_meta):
                for key, spec, desc in expand_entries(f_name, f_meta):
                    registry.register_flow(key, spec, desc)
        return count

    @staticmethod
    def search_catalog(
        registry: FlowRegistry,
        *,
        query: str = "",
        available_inputs: set[str] | list[str] | None = None,
        required_outputs: set[str] | list[str] | None = None,
        scheme: str | None = None,
        limit: int = 15,
    ) -> list[UnitContract]:
        q_tokens = [t.lower() for t in query.split() if t.strip()]
        avail_set = set(available_inputs) if available_inputs is not None else None
        req_out_set = set(required_outputs) if required_outputs is not None else None

        scored: list[tuple[int, UnitContract]] = []
        for u in registry.units.values():
            if scheme and u.scheme != scheme:
                continue
            if avail_set is not None and not (set(u.in_reqs) - set(u.in_defaults.keys()) <= avail_set):
                continue
            if req_out_set is not None and not (req_out_set <= set(u.out_keys)):
                continue

            in_all = (*u.in_reqs, *u.in_optional, *u.in_defaults)
            haystack = f"{u.name} {u.target} {' '.join(in_all)} {' '.join(u.out_keys)} {u.desc}".lower()
            if q_tokens and not all(tok in haystack for tok in q_tokens):
                continue

            score = sum(3 for tok in q_tokens if tok in u.name.lower()) + sum(
                2 for tok in q_tokens if tok in u.out_keys or tok in in_all
            )
            scored.append((score, u))

        scored.sort(key=lambda item: (-item[0], item[1].name))
        return [u for _, u in scored[:limit]]

    @classmethod
    def format_search_results(
        cls,
        registry: FlowRegistry,
        *,
        query: str = "",
        available_inputs: set[str] | list[str] | None = None,
        required_outputs: set[str] | list[str] | None = None,
        scheme: str | None = None,
        limit: int = 15,
    ) -> str:
        matches = cls.search_catalog(
            registry,
            query=query,
            available_inputs=available_inputs,
            required_outputs=required_outputs,
            scheme=scheme,
            limit=limit,
        )
        if not matches:
            return "[CATALOG SEARCH] No matching units found."

        lines = [f"[CATALOG MATCHES ({len(matches)})]"]
        for u in matches:
            lines.append(f"  - {u.name}: {emit_unit_dsl(u)}  # {u.desc}")
        return "\n".join(lines)


if __name__ == "__main__":
    target_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    sys.stdout.write(
        json.dumps(UnitsIndexer.build_repo_index(target_dir), indent=2, ensure_ascii=False) + "\n"
    )