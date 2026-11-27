# fiber.gateway.mcp.mapper.spec
from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

import orjson
import typer

from fiber.gateway.bridge.emitter import get_emitter
from fiber.node.bound.guard.preflight import PreflightGuard
from fiber.gateway.mcp.mapper.builtin import (
    BUILTIN_ALIASES,
    DEFAULT_WORKER_PACKAGE,
    discover_builtin_workers,
    is_explicit_internal_target,
)
from fiber.gateway.mcp.mapper.config import MappedMcpEntry, McpConfigMapper
from xphi.node.space.bind.resolver import resolve_path

log = get_emitter("mcp.mapper.spec")

ACTIVE_TARGETS_TUNNEL_KEY = "mcp:registry:active_targets"
STANDALONE_ATTACHED_TUNNEL_KEY = "mcp:registry:standalone_attached"

ANCHOR_ROOT = resolve_path("anchor")
ANCHOR_MCP_PATH = ANCHOR_ROOT / "mcp.json"

@dataclass(frozen=True)
class ResolvedTarget:
    target_id: str
    exec_cmd: str
    mode: str
    transport: str
    preflight: Optional[str] = None
    tags: Tuple[str, ...] = ()
    env: Tuple[Tuple[str, str], ...] = ()
    headers: Tuple[Tuple[str, str], ...] = ()

    @property
    def env_dict(self) -> Dict[str, str]:
        return dict(self.env)

    @property
    def headers_dict(self) -> Dict[str, str]:
        return dict(self.headers)


def parse_fiber_tags(raw: Any) -> Tuple[str, ...]:
    """Normalize FIBER_TAGS inputs ('', '[]', ['STANDALONE'], 'a,b') into uppercase tuples."""
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


def _freeze_str_map(raw: Optional[Mapping[str, Any]]) -> Tuple[Tuple[str, str], ...]:
    if not raw or not isinstance(raw, Mapping):
        return ()
    return tuple(
        sorted(
            (str(k).strip(), str(v))
            for k, v in raw.items()
            if str(k).strip() and v is not None
        )
    )


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
    *,
    env: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, Any]] = None,
) -> ResolvedTarget:
    """Resolve raw execution input or builtin alias into an immutable ResolvedTarget."""
    raw_cmd = exec_cmd.strip()
    if not raw_cmd:
        log.error("[Fiber] Empty execution command provided.")
        raise typer.Exit(1)

    builtin_map = discover_builtin_workers(include_internal=True)
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
        tags=parse_fiber_tags(tags),
        env=_freeze_str_map(env),
        headers=_freeze_str_map(headers),
    )


def _resolve_builtin_specs(
    target_filter: Optional[str] = None,
    mode_override: Optional[str] = None,
    preflight_override: Optional[str] = None,
    base_package: str = DEFAULT_WORKER_PACKAGE,
) -> List[ResolvedTarget]:
    allow_internal = is_explicit_internal_target(target_filter)
    builtin_map = discover_builtin_workers(base_package, include_internal=allow_internal)

    seen_ids: set[str] = set()
    specs: List[ResolvedTarget] = []
    clean_filter = target_filter.strip() if target_filter else None
    normalized_filter = BUILTIN_ALIASES.get(clean_filter, clean_filter) if clean_filter else None

    for tid, cmd, default_mode in builtin_map.values():
        if tid in seen_ids:
            continue
        if normalized_filter and tid != normalized_filter and clean_filter != tid.split(".")[-1]:
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
        clean_env = dict(clean_env) if clean_env else {}
        env_mode = clean_env.pop("FIBER_MODE", None)
        env_tags = clean_env.pop("FIBER_TAGS", None)
        entry_headers = getattr(entry, "headers", None)

        specs.append(
            resolve_connector_spec(
                target=entry.target_id,
                exec_cmd=entry.exec_cmd,
                mode=mode_override or env_mode,
                preflight=entry_preflight,
                tags=env_tags,
                env=clean_env,
                headers=entry_headers,
            )
        )
    return specs


def _collect_specs_with_envs(
    agent: Optional[str] = None,
    exec_cmd: Optional[str] = None,
    config: Optional[Path] = None,
    target: Optional[str] = None,
    mode: Optional[str] = None,
    preflight: Optional[str] = None,
    include_builtins: bool = False,
) -> Tuple[List[ResolvedTarget], Dict[str, Dict[str, str]]]:
    specs_by_id: Dict[str, ResolvedTarget] = {}
    env_lookup: Dict[str, Dict[str, str]] = {}

    def _ingest_mapped(entries: List[MappedMcpEntry]):
        for e in entries:
            env_lookup[e.target_id] = dict(e.env)
        for s in _specs_from_mapped_entries(
            entries, target_filter=target, mode_override=mode, preflight_override=preflight
        ):
            specs_by_id[s.target_id] = s

    if agent is not None:
        norm_agent = agent.strip().lower()
        if norm_agent in ("builtin", "builtins"):
            include_builtins = True
        else:
            scan_agent = None if norm_agent == "all" else norm_agent
            _ingest_mapped(McpConfigMapper.scan_and_parse(agent=scan_agent))
            if norm_agent == "all":
                include_builtins = True

    if config is not None:
        _ingest_mapped(McpConfigMapper.parse_file(config))

    if exec_cmd is not None:
        if exec_cmd.strip().lower() in ("builtin", "builtins"):
            include_builtins = True
        else:
            s = resolve_connector_spec(
                target=target,
                exec_cmd=exec_cmd,
                mode=mode,
                preflight=preflight,
                tags=os.environ.get("FIBER_TAGS"),
            )
            specs_by_id[s.target_id] = s

    if include_builtins:
        for s in _resolve_builtin_specs(target, mode, preflight):
            specs_by_id.setdefault(s.target_id, s)

    return list(specs_by_id.values()), env_lookup


def _upsert_anchor_config(
    specs: List[ResolvedTarget],
    env_lookup: Optional[Dict[str, Dict[str, str]]] = None,
) -> Path:
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
        entry_dict: Dict[str, Any] = dict(existing_entry) if isinstance(existing_entry, dict) else {}

        merged_env: Dict[str, str] = {}
        if isinstance(entry_dict.get("env"), dict):
            merged_env.update({str(k): str(v) for k, v in entry_dict["env"].items() if v is not None})
        if spec.env:
            merged_env.update(spec.env_dict)
        if spec.target_id in env_lookup:
            merged_env.update(env_lookup[spec.target_id])

        merged_env["FIBER_MODE"] = spec.mode
        if spec.preflight:
            merged_env["FIBER_PREFLIGHT_PATTERN"] = spec.preflight
        if spec.tags:
            merged_env["FIBER_TAGS"] = ",".join(spec.tags)
        elif "FIBER_TAGS" in merged_env and not parse_fiber_tags(merged_env["FIBER_TAGS"]):
            merged_env.pop("FIBER_TAGS", None)

        merged_headers: Dict[str, str] = {}
        if isinstance(entry_dict.get("headers"), dict):
            merged_headers.update({str(k): str(v) for k, v in entry_dict["headers"].items() if v is not None})
        if spec.headers:
            merged_headers.update(spec.headers_dict)

        if spec.exec_cmd.startswith(("http://", "https://")):
            entry_dict.pop("command", None)
            entry_dict.pop("args", None)
            entry_dict["url"] = spec.exec_cmd
        else:
            entry_dict.pop("url", None)
            try:
                parts = shlex.split(spec.exec_cmd)
            except ValueError:
                parts = spec.exec_cmd.split()
            entry_dict["command"] = parts[0] if parts else spec.exec_cmd
            entry_dict["args"] = parts[1:] if len(parts) > 1 else []

        if merged_env:
            entry_dict["env"] = merged_env
        else:
            entry_dict.pop("env", None)

        if merged_headers:
            entry_dict["headers"] = merged_headers
        else:
            entry_dict.pop("headers", None)

        servers[spec.target_id] = entry_dict

    ANCHOR_MCP_PATH.write_bytes(orjson.dumps(data, option=orjson.OPT_INDENT_2))
    log.info(f"[Fiber] Copied & locked {len(specs)} target(s) into canonical config: {ANCHOR_MCP_PATH}")
    return ANCHOR_MCP_PATH


def collect_target_specs(
    agent: Optional[str] = None,
    exec_cmd: Optional[str] = None,
    config: Optional[Path] = None,
    target: Optional[str] = None,
    mode: Optional[str] = None,
    preflight: Optional[str] = None,
    include_builtins: bool = False,
) -> List[ResolvedTarget]:
    """Collect and resolve MCP target specifications from agent configs, files, commands, or builtins."""
    specs, _ = _collect_specs_with_envs(
        agent=agent,
        exec_cmd=exec_cmd,
        config=config,
        target=target,
        mode=mode,
        preflight=preflight,
        include_builtins=include_builtins,
    )
    return specs


def sync_target_specs(
    agent: Optional[str] = None,
    exec_cmd: Optional[str] = None,
    config: Optional[Path] = None,
    target: Optional[str] = None,
    mode: Optional[str] = None,
    preflight: Optional[str] = None,
    include_builtins: bool = False,
) -> Tuple[List[ResolvedTarget], Path]:
    """Collect target specifications and upsert them into ~/.anchor/mcp.json."""
    specs, env_lookup = _collect_specs_with_envs(
        agent=agent,
        exec_cmd=exec_cmd,
        config=config,
        target=target,
        mode=mode,
        preflight=preflight,
        include_builtins=include_builtins,
    )
    if not specs:
        return [], ANCHOR_MCP_PATH
    out_path = _upsert_anchor_config(specs, env_lookup=env_lookup)
    return specs, out_path

def load_anchor_specs() -> List[ResolvedTarget]:
    """Load and resolve canonical MCP target specifications from ~/.anchor/mcp.json."""
    anchor_entries = McpConfigMapper.scan_and_parse(agent="fiber")
    return _specs_from_mapped_entries(anchor_entries)