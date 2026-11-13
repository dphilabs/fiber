# fiber.node.mcp.mapper.spec
import os
import sys
import ast
import shlex
import importlib.util
from pathlib import Path
from urllib.parse import urlparse
from dataclasses import dataclass
from typing import Dict, Tuple, Optional, List, Any

import orjson
import typer
from fiber.node.mcp.guard.preflight import PreflightGuard
from fiber.node.mcp.mapper.config import McpConfigMapper, MappedMcpEntry

from xphi.node.space.bind.resolver import resolve_path
from fiber.gateway.bridge.emitter import get_emitter

log = get_emitter("mcp.mapper.spec")

ACTIVE_TARGETS_TUNNEL_KEY = "mcp:registry:active_targets"
STANDALONE_ATTACHED_TUNNEL_KEY = "mcp:registry:standalone_attached"

ANCHOR_ROOT = resolve_path("anchor")
ANCHOR_MCP_PATH = ANCHOR_ROOT / "mcp.json"
DEFAULT_WORKER_PACKAGE = "fiber.node.mcp.worker"


@dataclass(frozen=True)
class ResolvedTarget:
    target_id: str
    exec_cmd: str
    mode: str
    transport: str
    preflight: Optional[str] = None
    tags: Tuple[str, ...] = ()


BUILTIN_ALIASES: Dict[str, str] = {
    "deployer": "worker.deployer",
    "finops": "worker.finops",
    "reaper": "worker.reaper",
    "eco.margin": "worker.eco.margin",
    "eco.finlib": "worker.eco.finlib",
    "price.model": "worker.price.model",
    "search.archive": "worker.search.archive",
}

_BUILTIN_CACHE: Dict[str, Dict[str, Tuple[str, str, str]]] = {}


def _parse_fiber_tags(raw: Any) -> Tuple[str, ...]:
    """FIBER_TAGS="", "[]", ["STANDALONE"], "a,b" 등 모든 입력 포맷을 대문자 튜플로 정규화합니다."""
    if not raw:
        return ()
    if isinstance(raw, (list, tuple, set)):
        items = raw
    elif isinstance(raw, str):
        s = raw.strip()
        if not s or s == "[]":
            return ()
        if s.startswith("[") and s.endswith("]"):
            try:
                parsed = orjson.loads(s)
                items = parsed if isinstance(parsed, list) else [s]
            except Exception:
                items = s[1:-1].replace("'", "").replace('"', "").split(",")
        else:
            items = s.split(",")
    else:
        return ()

    return tuple(dict.fromkeys(str(x).strip().upper() for x in items if str(x).strip()))


def _extract_base_name(b: ast.expr) -> str:
    if isinstance(b, ast.Name):
        return b.id
    if isinstance(b, ast.Attribute):
        return b.attr
    if isinstance(b, ast.Subscript):
        return _extract_base_name(b.value)
    return ""


def discover_builtin_workers(
    base_package: str = DEFAULT_WORKER_PACKAGE,
) -> Dict[str, Tuple[str, str, str]]:
    if base_package in _BUILTIN_CACHE:
        return _BUILTIN_CACHE[base_package]

    discovered: Dict[str, Tuple[str, str, str]] = {}
    try:
        spec = importlib.util.find_spec(base_package)
        if not spec or not spec.submodule_search_locations:
            return discovered
        root_dir = Path(list(spec.submodule_search_locations)[0]).resolve()
    except Exception:
        return discovered

    target_bases = {"WorkerProtocol", "AsyncWorkerProtocol", "BaseWorker", "McpWorker"}

    for py_file in sorted(root_dir.rglob("*.py")):
        if py_file.name.startswith("_") or "e2e" in py_file.parts or "tests" in py_file.parts:
            continue
        try:
            source_text = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source_text)
        except Exception:
            continue

        rel_parts = py_file.relative_to(root_dir).with_suffix("").parts
        rel_key = ".".join(rel_parts)
        module_path = f"{base_package}.{rel_key}"
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

        if matched_mode is None and (rel_key in BUILTIN_ALIASES or leaf in BUILTIN_ALIASES or "__main__" in source_text):
            matched_mode = "multiplex" if "Async" in source_text else "linear"

        if matched_mode is not None:
            canonical_id = BUILTIN_ALIASES.get(rel_key, f"worker.{rel_key}")
            entry = (canonical_id, f"{sys.executable} -m {module_path}", matched_mode)

            discovered[canonical_id] = entry
            discovered[rel_key] = entry
            discovered[f"worker.{rel_key}"] = entry
            if leaf not in discovered or BUILTIN_ALIASES.get(leaf) == canonical_id:
                discovered[leaf] = entry

            for alias_key, target_cid in BUILTIN_ALIASES.items():
                if target_cid == canonical_id:
                    discovered[alias_key] = entry

    _BUILTIN_CACHE[base_package] = discovered
    return discovered


def _resolve_all_builtin_specs(
    target_filter: Optional[str] = None,
    mode_override: Optional[str] = None,
    preflight_override: Optional[str] = None,
    base_package: str = DEFAULT_WORKER_PACKAGE,
) -> List[ResolvedTarget]:
    builtin_map = discover_builtin_workers(base_package)
    seen_ids: set[str] = set()
    specs: List[ResolvedTarget] = []
    normalized_filter = BUILTIN_ALIASES.get(target_filter, target_filter) if target_filter else None

    for tid, cmd, default_mode in builtin_map.values():
        if tid in seen_ids:
            continue
        if normalized_filter and tid != normalized_filter and target_filter != tid.split(".")[-1]:
            continue
        seen_ids.add(tid)
        specs.append(
            resolve_connector_spec(
                target=tid,
                exec_cmd=cmd,
                mode=mode_override or default_mode,
                transport="stdio",
                preflight=preflight_override,
            )
        )
    return specs


def _infer_fallback_target_id(raw_cmd: str, is_http_url: bool) -> str:
    if is_http_url:
        return urlparse(raw_cmd).hostname or "remote-mcp"
    try:
        parts = shlex.split(raw_cmd)
    except ValueError:
        parts = raw_cmd.split()
    return Path(parts[0]).stem if parts else "local-mcp"


def resolve_connector_spec(
    target: Optional[str] = None,
    exec_cmd: str = "",
    mode: Optional[str] = None,
    transport: Optional[str] = None,
    preflight: Optional[str] = None,
    tags: Any = None,
) -> ResolvedTarget:
    raw_cmd = exec_cmd.strip()
    if not raw_cmd:
        log.error("[Fiber] Empty execution command provided.")
        raise typer.Exit(1)

    builtin_map = discover_builtin_workers()
    if raw_cmd in builtin_map:
        inferred_target, resolved_cmd, inferred_default_mode = builtin_map[raw_cmd]
    else:
        inferred_target, resolved_cmd, inferred_default_mode = None, raw_cmd, None

    is_http_url = resolved_cmd.startswith(("http://", "https://"))
    is_managed_bin_dir = not is_http_url and Path(resolved_cmd).is_dir()

    resolved_transport = (
        transport.lower().strip()
        if transport is not None
        else ("network" if (is_http_url or is_managed_bin_dir) else "stdio")
    )
    if resolved_transport not in ("stdio", "network"):
        log.error(f"[Fiber] Invalid transport '{resolved_transport}'. Allowed values: 'stdio', 'network'.")
        raise typer.Exit(1)

    resolved_target = (target or inferred_target or _infer_fallback_target_id(resolved_cmd, is_http_url)).strip()

    if mode is not None:
        resolved_mode = mode.lower().strip()
    elif inferred_default_mode is not None:
        resolved_mode = inferred_default_mode
    else:
        resolved_mode = "multiplex" if resolved_transport == "network" else "ephemeral"

    if resolved_mode not in ("ephemeral", "linear", "multiplex"):
        log.error(f"[Fiber] Invalid mode '{resolved_mode}'. Allowed values: 'ephemeral', 'linear', 'multiplex'.")
        raise typer.Exit(1)

    return ResolvedTarget(
        target_id=resolved_target,
        exec_cmd=resolved_cmd,
        mode=resolved_mode,
        transport=resolved_transport,
        preflight=PreflightGuard.resolve_pattern(resolved_target, preflight),
        tags=_parse_fiber_tags(tags),
    )


def _specs_from_mapped_entries(
    entries: List[MappedMcpEntry],
    target_filter: Optional[str] = None,
    mode_override: Optional[str] = None,
    preflight_override: Optional[str] = None,
) -> List[ResolvedTarget]:
    specs: List[ResolvedTarget] = []
    for entry in entries:
        if target_filter and entry.target_id != target_filter:
            continue

        entry_preflight, clean_env = PreflightGuard.extract_from_env_dict(entry.env, preflight_override)
        env_mode = clean_env.pop("FIBER_MODE", None) if clean_env else None
        env_tags = clean_env.pop("FIBER_TAGS", None) if clean_env else None
        if clean_env:
            os.environ.update(clean_env)

        specs.append(
            resolve_connector_spec(
                target=entry.target_id,
                exec_cmd=entry.exec_cmd,
                mode=mode_override or env_mode,
                preflight=entry_preflight,
                tags=env_tags,
            )
        )
    return specs


def _sync_to_anchor_config(
    specs: List[ResolvedTarget],
    env_lookup: Optional[Dict[str, Dict[str, str]]] = None,
) -> Optional[Path]:
    """해석된 ResolvedTarget 목록을 ~/.anchor/mcp.json의 표준 mcpServers 포맷으로 병합(Upsert)합니다."""
    if not specs:
        return None

    ANCHOR_MCP_PATH.parent.mkdir(parents=True, exist_ok=True)
    data: Dict[str, Any] = {"mcpServers": {}}

    if ANCHOR_MCP_PATH.is_file():
        try:
            loaded = orjson.loads(ANCHOR_MCP_PATH.read_bytes())
            if isinstance(loaded, dict):
                data = loaded
                if not isinstance(data.get("mcpServers"), dict):
                    data["mcpServers"] = {}
        except Exception as e:
            log.warning(f"[Fiber] Failed to read existing {ANCHOR_MCP_PATH}, resetting: {e}")

    servers = data["mcpServers"]
    env_lookup = env_lookup or {}

    for spec in specs:
        existing_entry = servers.get(spec.target_id, {})
        merged_env: Dict[str, str] = {}
        if isinstance(existing_entry, dict) and isinstance(existing_entry.get("env"), dict):
            merged_env.update({str(k): str(v) for k, v in existing_entry["env"].items()})
        if spec.target_id in env_lookup:
            merged_env.update(env_lookup[spec.target_id])

        merged_env["FIBER_MODE"] = spec.mode
        if spec.preflight:
            merged_env["FIBER_PREFLIGHT_PATTERN"] = spec.preflight
        if spec.tags:
            merged_env["FIBER_TAGS"] = ",".join(spec.tags)
        elif "FIBER_TAGS" in merged_env and not _parse_fiber_tags(merged_env["FIBER_TAGS"]):
            merged_env.pop("FIBER_TAGS", None)

        if spec.exec_cmd.startswith(("http://", "https://")):
            entry_dict: Dict[str, Any] = {"url": spec.exec_cmd}
        else:
            try:
                parts = shlex.split(spec.exec_cmd)
            except ValueError:
                parts = spec.exec_cmd.split()
            entry_dict = {
                "command": parts[0] if parts else spec.exec_cmd,
                "args": parts[1:] if len(parts) > 1 else [],
            }

        if merged_env:
            entry_dict["env"] = merged_env

        servers[spec.target_id] = entry_dict

    ANCHOR_MCP_PATH.write_bytes(orjson.dumps(data, option=orjson.OPT_INDENT_2))
    log.info(f"[Fiber] Copied & locked {len(specs)} target(s) into canonical config: {ANCHOR_MCP_PATH}")
    return ANCHOR_MCP_PATH


def _collect_specs_and_envs(
    agent: Optional[str] = None,
    exec_cmd: Optional[str] = None,
    config: Optional[Path] = None,
    target: Optional[str] = None,
    mode: Optional[str] = None,
    preflight: Optional[str] = None,
) -> Tuple[List[ResolvedTarget], Dict[str, Dict[str, str]]]:
    specs_by_id: Dict[str, ResolvedTarget] = {}
    env_lookup: Dict[str, Dict[str, str]] = {}

    def _ingest_mapped(entries: List[MappedMcpEntry]):
        for e in entries:
            env_lookup[e.target_id] = dict(e.env)
        for s in _specs_from_mapped_entries(entries, target_filter=target, mode_override=mode, preflight_override=preflight):
            specs_by_id[s.target_id] = s

    if agent is not None:
        norm_agent = agent.strip().lower()
        if norm_agent in ("builtin", "builtins"):
            for s in _resolve_all_builtin_specs(target, mode, preflight):
                specs_by_id[s.target_id] = s
        else:
            scan_agent = None if norm_agent == "all" else norm_agent
            _ingest_mapped(McpConfigMapper.scan_and_parse(agent=scan_agent))

    if config is not None:
        _ingest_mapped(McpConfigMapper.parse_file(config))

    if exec_cmd is not None:
        if exec_cmd.strip().lower() in ("builtin", "builtins"):
            for s in _resolve_all_builtin_specs(target, mode, preflight):
                specs_by_id[s.target_id] = s
        else:
            s = resolve_connector_spec(
                target=target,
                exec_cmd=exec_cmd,
                mode=mode,
                preflight=preflight,
                tags=os.environ.get("FIBER_TAGS"),
            )
            specs_by_id[s.target_id] = s

    return list(specs_by_id.values()), env_lookup