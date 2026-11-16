# fiber.node.reflex.unit.indexer
## @lineage: fiber.reflex.unit.indexer
from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fiber.node.reflex.unit.loader import UnitsLoader
from xphi.arch.contract.registry.flow import FlowRegistry
from xphi.arch.contract.reflex.flow import UnitContract

_CONTRACT_TAG_RE = re.compile(r"@contract:\s*(.+)")
_DESC_TAG_RE = re.compile(r"@desc:\s*(.+)")
_UNIT_TAG_RE = re.compile(r"@unit:\s*([A-Za-z0-9_]+)")

class UnitsIndexer:
    INDEX_FILENAME = "index.json"

    def __init__(self, loader: FiberUnitsLoader) -> None:
        self.loader = loader

    @staticmethod
    def build_repo_index(repo_root: str | Path, ref_prefix: str = "") -> dict[str, Any]:
        """Statically scan `.py` and `.sh` files under `repo_root` without importing or executing them."""
        root = Path(repo_root)
        units_manifest: dict[str, dict[str, Any]] = {}
        prefix = f"{ref_prefix}@" if ref_prefix else ""

        for path in sorted(root.rglob("*")):
            if not path.is_file() or any(p.startswith(".") for p in path.relative_to(root).parts):
                continue
            rel_path = path.relative_to(root).as_posix()
            if rel_path == UnitsIndexer.INDEX_FILENAME:
                continue

            raw_bytes = path.read_bytes()
            file_sha = hashlib.sha256(raw_bytes).hexdigest()

            if path.suffix == ".py" and path.name != "__init__.py":
                text = raw_bytes.decode("utf-8", errors="replace")
                has_pep723 = "# /// script" in text[:512]
                try:
                    tree = ast.parse(text, filename=rel_path)
                except SyntaxError:
                    continue

                for node in tree.body:
                    if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
                        doc = ast.get_docstring(node) or ""
                        c_match = _CONTRACT_TAG_RE.search(doc)
                        if not c_match:
                            continue
                        d_match = _DESC_TAG_RE.search(doc)
                        io_clause = c_match.group(1).strip()
                        desc = d_match.group(1).strip() if d_match else doc.splitlines()[0].strip()

                        target_uri = f"py({prefix}{rel_path}:{node.name})"
                        flow_dsl = UnitsIndexer._compose_flow_dsl(io_clause, target_uri)

                        units_manifest[node.name] = {
                            "desc": desc,
                            "flow": flow_dsl,
                            "path": rel_path,
                            "sha256": file_sha,
                            "isolated": has_pep723,
                        }

            elif path.suffix in (".sh", ".bash"):
                head = raw_bytes[:1024].decode("utf-8", errors="replace")
                u_match = _UNIT_TAG_RE.search(head)
                if not u_match:
                    continue
                u_name = u_match.group(1).strip()
                c_match = _CONTRACT_TAG_RE.search(head)
                d_match = _DESC_TAG_RE.search(head)
                desc = d_match.group(1).strip() if d_match else f"Exec script {rel_path}"
                io_clause = c_match.group(1).strip() if c_match else ""

                target_uri = f"exec({prefix}{rel_path})"
                flow_dsl = UnitsIndexer._compose_flow_dsl(io_clause, target_uri)

                units_manifest[u_name] = {
                    "desc": desc,
                    "flow": flow_dsl,
                    "path": rel_path,
                    "sha256": file_sha,
                }

        return {"version": "1.0", "units": units_manifest}

    @staticmethod
    def _compose_flow_dsl(io_clause: str, target_uri: str) -> str:
        if not io_clause:
            return target_uri
        if "->" in io_clause:
            left, _, right = io_clause.partition("->")
            return f"{left.strip()} -> {target_uri} -> {right.strip()}"
        if io_clause.startswith("in("):
            return f"{io_clause} -> {target_uri}"
        if io_clause.startswith("out("):
            return f"{target_uri} -> {io_clause}"
        return target_uri

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
        """Register all units from `index.json` into `ContractRegistry` without downloading script bodies."""
        idx = self.load_catalog_index(ref=ref, force_refresh=force_refresh)
        units_dict = idx.get("units", {})
        for u_name, u_meta in units_dict.items():
            registry.register_unit(u_name, u_meta["flow"], u_meta.get("desc", ""))
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