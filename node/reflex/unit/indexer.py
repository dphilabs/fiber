# fiber.node.reflex.unit.indexer
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from fiber.node.reflex.unit.loader import UnitsLoader
from xphi.arch.contract.reflex.flow import UnitContract
from xphi.arch.contract.registry.flow import FlowRegistry

_UNIT_TAG_RE = re.compile(r"@unit:\s*([A-Za-z0-9_]+)")
_FLOW_TAG_RE = re.compile(r"@flow:\s*(.+)")
_DESC_TAG_RE = re.compile(r"@desc:\s*(.+)")
_SIDE_EFFECT_TAG_RE = re.compile(r"@side_effect:\s*([A-Za-z0-9_]+)")


class UnitsIndexer:
    INDEX_FILENAME = "index.json"

    def __init__(self, loader: UnitsLoader) -> None:
        self.loader = loader

    @staticmethod
    def build_repo_index(repo_root: str | Path, ref_prefix: str = "") -> dict[str, Any]:
        """Statically scan `.py`/`.sh` script headers and `flow/*.json` specs under `repo_root`."""
        root = Path(repo_root)
        units_manifest: dict[str, dict[str, Any]] = {}
        flows_manifest: dict[str, dict[str, Any]] = {}
        prefix = f"{ref_prefix}@" if ref_prefix else ""

        # 1. Load declarative units & flows from `flow/*.json` (e.g. compliance.json, presets.json)
        flow_dir = root / "flow"
        if flow_dir.is_dir():
            for spec_path in sorted(flow_dir.glob("*.json")):
                try:
                    spec_data = json.loads(spec_path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                for u_name, u_meta in (spec_data.get("units") or {}).items():
                    if isinstance(u_meta, dict) and "flow" in u_meta:
                        units_manifest[u_name] = {
                            "desc": u_meta.get("desc", ""),
                            "flow": u_meta["flow"],
                        }
                for f_name, f_meta in (spec_data.get("flows") or {}).items():
                    if isinstance(f_meta, dict) and "flow" in f_meta:
                        flows_manifest[f_name] = {
                            "desc": f_meta.get("desc", ""),
                            "flow": f_meta["flow"],
                        }

        # 2. Scan executable `.py` and `.sh` unit scripts via unified @unit / @flow / @desc tags
        for path in sorted(root.rglob("*")):
            if not path.is_file() or any(p.startswith(".") for p in path.relative_to(root).parts):
                continue
            if path.suffix not in (".py", ".sh", ".bash") or path.name == "__init__.py":
                continue
            rel_path = path.relative_to(root).as_posix()
            if rel_path == UnitsIndexer.INDEX_FILENAME:
                continue

            raw_bytes = path.read_bytes()
            file_sha = hashlib.sha256(raw_bytes).hexdigest()
            head = raw_bytes[:2048].decode("utf-8", errors="replace")

            u_match = _UNIT_TAG_RE.search(head)
            f_match = _FLOW_TAG_RE.search(head)
            if not (u_match and f_match):
                continue

            u_name = u_match.group(1).strip()
            flow_dsl = f_match.group(1).strip()
            d_match = _DESC_TAG_RE.search(head)
            s_match = _SIDE_EFFECT_TAG_RE.search(head)
            desc = d_match.group(1).strip() if d_match else f"Unit {u_name} ({rel_path})"

            if prefix:
                flow_dsl = re.sub(r"\b(py|exec)\((?!@)", rf"\1({prefix}", flow_dsl)

            entry: dict[str, Any] = {
                "desc": desc,
                "flow": flow_dsl,
                "path": rel_path,
                "sha256": file_sha,
            }
            if path.suffix == ".py":
                entry["isolated"] = "# /// script" in head[:512]
            if s_match:
                entry["side_effect"] = s_match.group(1).strip()

            units_manifest[u_name] = entry

        result: dict[str, Any] = {"version": "1.0", "units": units_manifest}
        if flows_manifest:
            result["flows"] = flows_manifest
        return result

    @classmethod
    def write_repo_index(cls, repo_root: str | Path, ref_prefix: str = "") -> Path:
        """Build and write `index.json` directly into the `repo_root` directory."""
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

        # Push SHA256 locks into the loader for lazy verification upon execution
        hash_locks = {
            meta["path"]: meta["sha256"]
            for meta in index_data.get("units", {}).values()
            if "path" in meta and "sha256" in meta
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
        """Register all units (and optional flows) from `index.json` into `FlowRegistry`."""
        idx = self.load_catalog_index(ref=ref, force_refresh=force_refresh)
        units_dict = idx.get("units", {})
        for u_name, u_meta in units_dict.items():
            registry.register_unit(u_name, u_meta["flow"], u_meta.get("desc", ""))
        for f_name, f_meta in idx.get("flows", {}).items():
            if hasattr(registry, "register_flow"):
                registry.register_flow(f_name, f_meta["flow"], f_meta.get("desc", ""))
        return len(units_dict)

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
        """Filter and rank units by keyword, scheme, input satisfiability, and output contract."""
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

            haystack = f"{u.name} {u.target} {' '.join(u.in_reqs)} {' '.join(u.out_keys)} {u.desc}".lower()
            if q_tokens and not all(tok in haystack for tok in q_tokens):
                continue

            score = sum(3 for tok in q_tokens if tok in u.name.lower()) + sum(
                2 for tok in q_tokens if tok in u.out_keys or tok in u.in_reqs
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
        """Format matched units into a compact LLM-ready catalog snippet."""
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
            in_items = list(u.in_reqs) + [f"{k}={v!r}" for k, v in u.in_defaults.items()]
            in_str = f"in({', '.join(in_items)}) -> " if in_items else ""
            out_str = f" -> out({', '.join(u.out_keys)})" if u.out_keys else ""
            lines.append(f"  - {u.name}: {in_str}{u.scheme}({u.target}){out_str}  # {u.desc}")
        return "\n".join(lines)


if __name__ == "__main__":
    target_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    sys.stdout.write(
        json.dumps(UnitsIndexer.build_repo_index(target_dir), indent=2, ensure_ascii=False) + "\n"
    )