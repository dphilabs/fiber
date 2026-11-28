# fiber.studio.harness.mcp.worker
from __future__ import annotations

import asyncio
from importlib.metadata import entry_points
from importlib.util import find_spec
import os
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union

import orjson

from fiber.node.bound.guard.preflight import PreflightGuard
from fiber.gateway.mcp.connector import MCPServerConnector
from fiber.gateway.mcp.mapper.builtin import (
    BUILTIN_ALIASES,
    DEFAULT_WORKER_PACKAGE,
    discover_builtin_workers,
)
from fiber.gateway.mcp.mapper.spec import (
    ACTIVE_TARGETS_TUNNEL_KEY,
    ANCHOR_MCP_PATH,
    ResolvedTarget,
    load_anchor_specs as load_canonical_anchor_specs,
    sync_target_specs,
)
import fiber.node.worker.internal.reaper as worker_reaper
from fiber.studio.cli.e2e.pipeline import log
from xphi.node.space.tunnel.factory import TunnelFactory

ANCHOR_MCP_JSON: Path = ANCHOR_MCP_PATH
WORKER_BASE_PKG: str = DEFAULT_WORKER_PACKAGE

_UNSET: Any = object()

_WORKER_OVERRIDES: Dict[str, Tuple[str, str, Optional[str]]] = {
    "worker.eco.finlib": ("eco.finlib", "linear", r"normalize_order|pow\(|exp\("),
    "worker.deployer": ("deployer", "ephemeral", None),
    "legacy-01": ("deployer", "ephemeral", None),
    "legacy-deployer": ("deployer", "ephemeral", None),
}


def _module_exists(module_name: str) -> bool:
    if not module_name:
        return False
    try:
        return find_spec(module_name) is not None
    except (ModuleNotFoundError, ValueError):
        return False


def _is_stale_module_cmd(cmd: Optional[str]) -> bool:
    """Detect `-m <pkg.mod>` commands in cached mcp.json whose module was moved/deleted."""
    if not cmd or cmd.startswith(("http://", "https://", "sse://", "ws://", "wss://")):
        return False
    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = cmd.split()
    if "-m" in parts:
        idx = parts.index("-m")
        if idx + 1 < len(parts):
            return not _module_exists(parts[idx + 1])
    return False


class E2EWorkerManager:
    @staticmethod
    def get_worker_packages(extra_packages: Optional[Sequence[str]] = None) -> Tuple[str, ...]:
        candidates: List[str] = []
        if extra_packages:
            candidates.extend(p.strip() for p in extra_packages if p and p.strip())

        env_raw = os.environ.get("MCP_WORKER_PACKAGE", "")
        if env_raw.strip():
            for chunk in env_raw.replace(":", ",").split(","):
                if pkg := chunk.strip():
                    candidates.append(pkg)

        candidates.append(WORKER_BASE_PKG)

        try:
            for ep in entry_points(group="fiber.worker_packages"):
                if ep.value and ep.value.strip():
                    candidates.append(ep.value.strip())
        except Exception:
            pass

        return tuple(dict.fromkeys(candidates))

    @classmethod
    def _resolve_worker_cmd(
        cls,
        sub_mod: str,
        worker_packages: Optional[Sequence[str]] = None,
    ) -> Optional[str]:
        if not sub_mod:
            return None
        pkgs = cls.get_worker_packages(worker_packages)
        for base_pkg in pkgs:
            full_mod = f"{base_pkg}.{sub_mod}"
            if _module_exists(full_mod):
                return f"{sys.executable} -m {full_mod}"
        return None

    @classmethod
    def _resolve_from_builtin_discovery(
        cls,
        candidate_id: str,
        worker_packages: Optional[Sequence[str]] = None,
    ) -> Optional[Tuple[str, str, Optional[str]]]:
        normalized_id = BUILTIN_ALIASES.get(candidate_id, candidate_id)
        for pkg in cls.get_worker_packages(worker_packages):
            try:
                builtin_map = discover_builtin_workers(pkg, include_internal=True)
            except Exception:
                continue

            if normalized_id in builtin_map:
                tid, cmd, default_mode = builtin_map[normalized_id]
                return cmd, default_mode, PreflightGuard.resolve_pattern(tid, None)

            for tid, cmd, default_mode in builtin_map.values():
                if tid in (candidate_id, normalized_id) or candidate_id == tid.split(".")[-1]:
                    return cmd, default_mode, PreflightGuard.resolve_pattern(tid, None)
        return None

    @classmethod
    def resolve_worker_defaults(
        cls,
        lookup_id: str,
        *,
        worker_packages: Optional[Sequence[str]] = None,
    ) -> Tuple[Optional[str], str, Optional[str]]:
        # 1. Explicit test/legacy overrides
        base_key = lookup_id
        if base_key not in _WORKER_OVERRIDES:
            for k in _WORKER_OVERRIDES:
                if lookup_id.startswith(f"{k}_") or lookup_id.startswith(f"{k}-"):
                    base_key = k
                    break

        if base_key in _WORKER_OVERRIDES:
            sub_mod, mode, guard = _WORKER_OVERRIDES[base_key]
            cmd = cls._resolve_worker_cmd(sub_mod, worker_packages)
            return cmd, mode, guard

        # Build candidate IDs: exact ID first, then stripped ephemeral suffix ('worker.foo.bar_xxxx' -> 'worker.foo.bar')
        candidates: List[str] = [lookup_id]
        if lookup_id.startswith("worker.") and "_" in lookup_id:
            stripped_last = lookup_id.rsplit("_", 1)[0]
            stripped_first = lookup_id.split("_", 1)[0]
            for cand in (stripped_last, stripped_first):
                if cand not in candidates:
                    candidates.append(cand)

        # 2. Builtin worker discovery (handles nested subpackages like fiber.node.worker.dev.ast)
        for cand in candidates:
            discovered = cls._resolve_from_builtin_discovery(cand, worker_packages)
            if discovered is not None:
                return discovered

        # 3. Direct '<pkg>.<sub_mod>' module lookup (e.g. reflexor.units.worker.price.model)
        for cand in candidates:
            if cand.startswith("worker."):
                sub_mod = cand[len("worker."):]
                cmd = cls._resolve_worker_cmd(sub_mod, worker_packages)
                if cmd is not None:
                    return cmd, "multiplex", PreflightGuard.resolve_pattern(cand, None)

        return None, "multiplex", None

    @staticmethod
    def load_anchor_specs(auto_bootstrap: bool = False) -> Dict[str, ResolvedTarget]:
        if auto_bootstrap and not ANCHOR_MCP_JSON.is_file():
            log.info(f"[E2EWorker] {ANCHOR_MCP_JSON} not found. Bootstrapping builtins into anchor config...")
            try:
                sync_target_specs(include_builtins=True)
            except BaseException as exc:
                log.warning(f"[E2EWorker] Auto-bootstrap warning: {exc}")

        if ANCHOR_MCP_JSON.is_file():
            try:
                return {s.target_id: s for s in load_canonical_anchor_specs()}
            except Exception as exc:
                log.warning(f"[E2EWorker] Failed to parse {ANCHOR_MCP_JSON}: {exc}")
        return {}

    @staticmethod
    async def get_active_daemon_ids() -> Set[str]:
        try:
            tunnel = await TunnelFactory.get_default()
            if not tunnel:
                return set()
            raw = await tunnel.get(ACTIVE_TARGETS_TUNNEL_KEY)
            if raw:
                parsed = orjson.loads(raw)
                if isinstance(parsed, (dict, list)):
                    return {str(k).strip() for k in parsed if str(k).strip()}
        except Exception:
            pass
        return set()

    @staticmethod
    def _is_network_url(target_str: Optional[str]) -> bool:
        return bool(target_str and target_str.startswith(("http://", "https://", "sse://", "ws://", "wss://")))

    @classmethod
    def build_connector(
        cls,
        target_id: str,
        *,
        base_worker_id: Optional[str] = None,
        anchor_specs: Optional[Dict[str, ResolvedTarget]] = None,
        execution_target: Optional[str] = None,
        mode: Optional[str] = None,
        transport_type: Optional[str] = None,
        preflight_pattern: Any = _UNSET,
        env: Optional[Mapping[str, str]] = None,
        headers: Optional[Mapping[str, str]] = None,
        worker_packages: Optional[Sequence[str]] = None,
    ) -> MCPServerConnector:
        if execution_target and not cls._is_network_url(execution_target) and " " not in execution_target:
            base_worker_id = base_worker_id or execution_target
            execution_target = None

        lookup_id = base_worker_id or target_id
        spec = (anchor_specs or {}).get(lookup_id)
        default_cmd, default_mode, default_guard = cls.resolve_worker_defaults(
            lookup_id, worker_packages=worker_packages
        )

        spec_cmd = spec.exec_cmd if (spec and not _is_stale_module_cmd(spec.exec_cmd)) else None
        resolved_cmd = execution_target or spec_cmd or default_cmd
        if not resolved_cmd:
            searched = cls.get_worker_packages(worker_packages)
            raise ValueError(
                f"[E2EWorker] Cannot resolve execution_target for '{target_id}' "
                f"(lookup='{lookup_id}', searched_packages={searched})."
            )

        is_net = cls._is_network_url(resolved_cmd)
        resolved_mode = mode or (spec.mode if spec else None) or ("multiplex" if is_net else default_mode)
        resolved_transport = (
            transport_type
            or ("network" if is_net else None)
            or (spec.transport if spec else None)
            or "stdio"
        )

        if preflight_pattern is not _UNSET:
            resolved_preflight = preflight_pattern
        else:
            resolved_preflight = (spec.preflight if spec else None) or default_guard

        merged_env: Dict[str, str] = {}
        if spec is not None and spec.env:
            merged_env.update(spec.env_dict)
        if env:
            merged_env.update({str(k): str(v) for k, v in env.items() if v is not None})

        merged_headers: Dict[str, str] = {}
        if spec is not None and spec.headers:
            merged_headers.update(spec.headers_dict)
        if headers:
            merged_headers.update({str(k): str(v) for k, v in headers.items() if v is not None})

        extra_kwargs: Dict[str, Any] = {}
        if merged_env:
            extra_kwargs["env"] = merged_env
        if merged_headers:
            extra_kwargs["headers"] = merged_headers

        return MCPServerConnector(
            target_id=target_id,
            execution_target=resolved_cmd,
            mode=resolved_mode,
            transport_type=resolved_transport,
            preflight_pattern=resolved_preflight,
            **extra_kwargs,
        )

    @classmethod
    async def mount_workers(
        cls,
        pipeline_or_connectors: Any,
        targets: Union[Sequence[str], Mapping[str, Union[str, Tuple[str, str], None]]],
        *,
        attach_live: bool = True,
        auto_bootstrap: bool = False,
        preflight_overrides: Optional[Dict[str, Optional[str]]] = None,
        mode_overrides: Optional[Dict[str, str]] = None,
        env_overrides: Optional[Dict[str, Mapping[str, str]]] = None,
        worker_packages: Optional[Sequence[str]] = None,
        tag: str = "E2EWorker",
    ) -> List[MCPServerConnector]:
        active_ids = await cls.get_active_daemon_ids() if attach_live else set()
        anchor_specs = cls.load_anchor_specs(auto_bootstrap=auto_bootstrap) if attach_live or auto_bootstrap else {}
        return cls._mount_sync(
            pipeline_or_connectors,
            targets,
            active_daemon_ids=active_ids,
            anchor_specs=anchor_specs,
            attach_live=attach_live,
            preflight_overrides=preflight_overrides,
            mode_overrides=mode_overrides,
            env_overrides=env_overrides,
            worker_packages=worker_packages,
            tag=tag,
        )

    @classmethod
    def mount_custom_targets(
        cls,
        pipeline_or_connectors: Any,
        targets: Mapping[str, Union[str, Tuple[str, str], None]],
        *,
        preflight_overrides: Optional[Dict[str, Optional[str]]] = None,
        mode_overrides: Optional[Dict[str, str]] = None,
        env_overrides: Optional[Dict[str, Mapping[str, str]]] = None,
        worker_packages: Optional[Sequence[str]] = None,
        tag: str = "E2EWorker",
    ) -> List[MCPServerConnector]:
        return cls._mount_sync(
            pipeline_or_connectors,
            targets,
            active_daemon_ids=set(),
            anchor_specs={},
            attach_live=False,
            preflight_overrides=preflight_overrides,
            mode_overrides=mode_overrides,
            env_overrides=env_overrides,
            worker_packages=worker_packages,
            tag=tag,
        )

    @classmethod
    def _mount_sync(
        cls,
        pipeline_or_connectors: Any,
        targets: Union[Sequence[str], Mapping[str, Union[str, Tuple[str, str], None]]],
        *,
        active_daemon_ids: Set[str],
        anchor_specs: Dict[str, ResolvedTarget],
        attach_live: bool,
        preflight_overrides: Optional[Dict[str, Optional[str]]] = None,
        mode_overrides: Optional[Dict[str, str]] = None,
        env_overrides: Optional[Dict[str, Mapping[str, str]]] = None,
        worker_packages: Optional[Sequence[str]] = None,
        tag: str = "E2EWorker",
    ) -> List[MCPServerConnector]:
        connectors_list: List[MCPServerConnector] = (
            pipeline_or_connectors.connectors
            if hasattr(pipeline_or_connectors, "connectors")
            else pipeline_or_connectors
        )
        preflight_map = preflight_overrides or {}
        mode_map = mode_overrides or {}
        env_map = env_overrides or {}
        target_map: Mapping[str, Any] = targets if isinstance(targets, Mapping) else {t: None for t in targets}

        spawned: List[MCPServerConnector] = []
        for tid, spec_val in target_map.items():
            if attach_live and tid in active_daemon_ids:
                log.info(f"[{tag}] Attached to live McpWorkerDaemon target: [{tid}] (Zero duplicate spawn)")
                continue

            exec_target = spec_val[0] if isinstance(spec_val, tuple) else spec_val
            mode_val = spec_val[1] if isinstance(spec_val, tuple) else mode_map.get(tid)
            pf_val = preflight_map[tid] if tid in preflight_map else _UNSET

            conn = cls.build_connector(
                target_id=tid,
                anchor_specs=anchor_specs,
                execution_target=exec_target,
                mode=mode_val,
                preflight_pattern=pf_val,
                env=env_map.get(tid),
                worker_packages=worker_packages,
            )
            log.info(
                f"[{tag}] Mounted connector [{tid}] (Transport: {conn.transport_type.upper()}, Mode: {conn.mode.upper()})"
            )
            connectors_list.append(conn)
            spawned.append(conn)

        return spawned

    @staticmethod
    def start_sentinel(
        pipeline: Any,
        *,
        sweep_interval: float = 1.0,
        anchor_store: Any = None,
        rpc_client: Any = None,
    ) -> worker_reaper.WorkerReaper:
        sentinel = worker_reaper.WorkerReaper(
            anchor_store=anchor_store or getattr(pipeline, "mock_store", None),
            rpc_client=rpc_client or getattr(pipeline, "rpc", None),
            sweep_interval=sweep_interval,
        )
        pipeline.sentinel = sentinel
        pipeline._sentinel_task = asyncio.create_task(sentinel.ignite())
        return sentinel

    @staticmethod
    async def stop_sentinel(pipeline: Any) -> None:
        sentinel = getattr(pipeline, "sentinel", None)
        if sentinel is not None:
            sentinel.running = False
        task: Optional[asyncio.Task] = getattr(pipeline, "_sentinel_task", None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @staticmethod
    async def wait_for_sentinel_rollback(
        pipeline: Any,
        handle_id: str,
        *,
        stale_timeout: float = 1.0,
        max_wait_sec: float = 10.0,
        poll_interval: float = 0.5,
    ) -> Any:
        store = getattr(pipeline, "mock_store", None)
        sentinel = getattr(pipeline, "sentinel", None)

        if store is not None:
            store.stale_timeout = stale_timeout
        if sentinel is not None and hasattr(sentinel, "ttl_thresholds"):
            sentinel.ttl_thresholds["transition.pending"] = stale_timeout

        final_state = None
        for _ in range(max(1, int(max_wait_sec / poll_interval))):
            final_state = await store.query_state(handle_id)
            if final_state and final_state.metadata.get("status") == "FAULTED":
                return final_state
            await asyncio.sleep(poll_interval)

        current_status = final_state.metadata.get("status") if final_state else "None"
        raise RuntimeError(
            f"Sentinel failed to rollback '{handle_id}' within {max_wait_sec:.1f}s. Current status: {current_status}"
        )