# fiber.node.mcp.mapper.builtin
from __future__ import annotations

import ast
from dataclasses import dataclass
import importlib.util
from importlib.metadata import entry_points
import os
from pathlib import Path
import sys
from typing import Dict, List, Optional, Tuple

DEFAULT_WORKER_PACKAGE = "fiber.node.worker"
EXCLUDED_DIRS = frozenset({"e2e", "tests"})
INTERNAL_SUBPACKAGE = "internal"

BUILTIN_ALIASES: Dict[str, str] = {
    "deployer": "worker.internal.deployer",
    "finops": "worker.internal.finops",
    "reaper": "worker.internal.reaper",
}

@dataclass(frozen=True)
class BuiltinWorkerEntry:
    canonical_id: str
    module_path: str
    exec_cmd: str
    default_mode: str
    is_internal: bool = False

_BUILTIN_CACHE: Dict[Tuple[str, ...], Dict[str, BuiltinWorkerEntry]] = {}

def _resolve_worker_packages(base_package: Optional[str] = None) -> Tuple[str, ...]:
    raw = (
        base_package
        if base_package and base_package != DEFAULT_WORKER_PACKAGE
        else os.environ.get("MCP_WORKER_PACKAGE", "")
    )
    pkgs: List[str] = [
        p.strip() for chunk in raw.replace(":", ",").split(",") if (p := chunk.strip())
    ]
    pkgs.append(DEFAULT_WORKER_PACKAGE)

    if not base_package or base_package == DEFAULT_WORKER_PACKAGE:
        try:
            for ep in entry_points(group="fiber.worker_packages"):
                if ep.value and ep.value.strip():
                    pkgs.append(ep.value.strip())
        except Exception:
            pass

    return tuple(dict.fromkeys(pkgs))


def _extract_base_name(b: ast.expr) -> str:
    if isinstance(b, ast.Name):
        return b.id
    if isinstance(b, ast.Attribute):
        return b.attr
    if isinstance(b, ast.Subscript):
        return _extract_base_name(b.value)
    return ""


def _has_builtin_opt_out(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if (
                any(isinstance(t, ast.Name) and t.id == "__mcp_builtin__" for t in node.targets)
                and isinstance(node.value, ast.Constant)
                and node.value.value is False
            ):
                return True
    return False


def _scan_all_workers(base_package: str = DEFAULT_WORKER_PACKAGE) -> Dict[str, BuiltinWorkerEntry]:
    packages = _resolve_worker_packages(base_package)
    if packages in _BUILTIN_CACHE:
        return _BUILTIN_CACHE[packages]

    discovered: Dict[str, BuiltinWorkerEntry] = {}
    target_bases = {"WorkerProtocol", "AsyncWorkerProtocol", "BaseWorker", "McpWorker"}

    for pkg in packages:
        try:
            spec = importlib.util.find_spec(pkg)
            if not spec or not spec.submodule_search_locations:
                continue
            search_roots = [Path(loc).resolve() for loc in spec.submodule_search_locations]
        except Exception:
            continue

        for root_dir in search_roots:
            if not root_dir.is_dir():
                continue
            for py_file in sorted(root_dir.rglob("*.py")):
                if py_file.name.startswith("_"):
                    continue

                rel_parts = py_file.relative_to(root_dir).with_suffix("").parts
                if any(part in EXCLUDED_DIRS for part in rel_parts):
                    continue

                try:
                    source_text = py_file.read_text(encoding="utf-8")
                    tree = ast.parse(source_text)
                except Exception:
                    continue

                is_internal = INTERNAL_SUBPACKAGE in rel_parts or _has_builtin_opt_out(tree)
                rel_key = ".".join(rel_parts)
                module_path = f"{pkg}.{rel_key}"
                leaf = rel_parts[-1]

                matched_mode: Optional[str] = None
                for node in tree.body:
                    if not isinstance(node, ast.ClassDef):
                        continue
                    bases = {_extract_base_name(b) for b in node.bases}
                    matched = bases & target_bases
                    if matched or any("Worker" in b for b in bases):
                        matched_mode = "multiplex" if "AsyncWorkerProtocol" in matched else "linear"
                        break

                if matched_mode is None and (
                    rel_key in BUILTIN_ALIASES or leaf in BUILTIN_ALIASES or "__main__" in source_text
                ):
                    matched_mode = "multiplex" if "Async" in source_text else "linear"

                if matched_mode is not None:
                    canonical_id = BUILTIN_ALIASES.get(rel_key, f"worker.{rel_key}")
                    if canonical_id in discovered:
                        continue

                    exec_cmd = f"{sys.executable} -m {module_path}"
                    entry = BuiltinWorkerEntry(
                        canonical_id=canonical_id,
                        module_path=module_path,
                        exec_cmd=exec_cmd,
                        default_mode=matched_mode,
                        is_internal=is_internal,
                    )

                    discovered[canonical_id] = entry
                    discovered.setdefault(rel_key, entry)
                    discovered.setdefault(f"worker.{rel_key}", entry)
                    discovered.setdefault(module_path, entry)
                    discovered.setdefault(exec_cmd, entry)
                    if leaf not in discovered or BUILTIN_ALIASES.get(leaf) == canonical_id:
                        discovered[leaf] = entry

                    for alias_key, target_cid in BUILTIN_ALIASES.items():
                        if target_cid == canonical_id:
                            discovered.setdefault(alias_key, entry)

    _BUILTIN_CACHE[packages] = discovered
    return discovered


def list_builtin_entries(
    base_package: str = DEFAULT_WORKER_PACKAGE,
    *,
    include_internal: bool = False,
) -> List[BuiltinWorkerEntry]:
    """Return deduplicated BuiltinWorkerEntry list (excludes internal workers by default)."""
    seen: set[str] = set()
    result: List[BuiltinWorkerEntry] = []
    for entry in _scan_all_workers(base_package).values():
        if entry.canonical_id in seen:
            continue
        if not include_internal and entry.is_internal:
            continue
        seen.add(entry.canonical_id)
        result.append(entry)
    return result


def get_builtin_entry(
    target_or_cmd: Optional[str],
    *,
    include_internal: bool = True,
    base_package: str = DEFAULT_WORKER_PACKAGE,
) -> Optional[BuiltinWorkerEntry]:
    """Lookup a BuiltinWorkerEntry by canonical_id, alias, module path, or '-m <module>' command."""
    if not target_or_cmd:
        return None
    key = target_or_cmd.strip()
    all_map = _scan_all_workers(base_package)
    entry = all_map.get(key)

    if entry is None and " -m " in key:
        mod_part = key.split(" -m ", 1)[1].split()[0].strip()
        entry = all_map.get(mod_part)

    if entry is not None and not include_internal and entry.is_internal:
        return None
    return entry


def classify_builtin_source(
    target_id: str,
    exec_cmd: str = "",
    fallback: str = "fiber",
) -> str:
    """Classify worker source label ('builtin', 'internal', or fallback) for console/diagnostics."""
    entry = get_builtin_entry(target_id, include_internal=True) or get_builtin_entry(
        exec_cmd, include_internal=True
    )
    if entry is None:
        return fallback
    return "internal" if entry.is_internal else "builtin"


def discover_builtin_workers(
    base_package: str = DEFAULT_WORKER_PACKAGE,
    *,
    include_internal: bool = False,
) -> Dict[str, Tuple[str, str, str]]:
    """Return {lookup_key: (canonical_id, exec_cmd, default_mode)} map."""
    all_entries = _scan_all_workers(base_package)
    return {
        k: (e.canonical_id, e.exec_cmd, e.default_mode)
        for k, e in all_entries.items()
        if include_internal or not e.is_internal
    }

def is_explicit_internal_target(target: Optional[str]) -> bool:
    """Return True if target explicitly addresses an internal worker or alias."""
    if not target:
        return False
    norm = target.strip()
    resolved = BUILTIN_ALIASES.get(norm, norm)
    return resolved.startswith("worker.internal.") or resolved.startswith("internal.")