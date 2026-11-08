# fiber.dev.base.mcp.worker
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union

from fiber.dev.base.workflow.pipeline import log
from fiber.gateway.bridge.action.connect import (
    ACTIVE_TARGETS_TUNNEL_KEY,
    ResolvedTarget,
    _specs_from_mapped_entries,
    run_connect_copy,
)
from fiber.plane.mcp.connector import MCPServerConnector
from fiber.plane.mcp.mapper.config import McpConfigMapper
import fiber.plane.mcp.worker as worker_pkg
import fiber.plane.mcp.worker.reaper as worker_reaper
from xphi.node.space.bind.resolver import resolve_path
from xphi.node.space.tunnel.factory import TunnelFactory

ANCHOR_MCP_JSON: Path = resolve_path("anchor") / "mcp.json"
WORKER_BASE_PKG: str = worker_pkg.__name__
_WORKER_OVERRIDES: Dict[str, Tuple[str, str, Optional[str]]] = {
    "worker.eco.finlib": ("eco.finlib", "linear", r"normalize_order|pow\(|exp\("),
    "worker.deployer": ("deployer", "ephemeral", None),
    "legacy-01": ("deployer", "ephemeral", None),
    "legacy-deployer": ("deployer", "ephemeral", None),
}


class E2EWorkerManager:
    @staticmethod
    def resolve_worker_defaults(lookup_id: str) -> Tuple[Optional[str], str, Optional[str]]:
        base_key = lookup_id
        if base_key not in _WORKER_OVERRIDES:
            for k in _WORKER_OVERRIDES:
                if lookup_id.startswith(f"{k}_") or lookup_id.startswith(f"{k}-"):
                    base_key = k
                    break

        if base_key in _WORKER_OVERRIDES:
            sub_mod, mode, guard = _WORKER_OVERRIDES[base_key]
            return f"{sys.executable} -m {WORKER_BASE_PKG}.{sub_mod}", mode, guard

        # 'worker.price.model_xxxx' -> 'worker.price.model' -> '{WORKER_BASE_PKG}.price.model'
        clean_id = lookup_id.split("_", 1)[0] if lookup_id.startswith("worker.") else lookup_id
        if clean_id.startswith("worker."):
            sub_mod = clean_id[len("worker."):]
            return f"{sys.executable} -m {WORKER_BASE_PKG}.{sub_mod}", "multiplex", None

        return None, "multiplex", None

    @staticmethod
    def load_anchor_specs(auto_bootstrap: bool = False) -> Dict[str, ResolvedTarget]:
        if auto_bootstrap and not ANCHOR_MCP_JSON.is_file():
            log.info(f"[E2EWorker] {ANCHOR_MCP_JSON} not found. Bootstrapping via connect copy...")
            try:
                run_connect_copy(exec_cmd="builtin")
            except BaseException as exc:
                log.warning(f"[E2EWorker] Auto-bootstrap warning: {exc}")

        if ANCHOR_MCP_JSON.is_file():
            entries = McpConfigMapper.parse_file(ANCHOR_MCP_JSON, agent_hint="fiber")
            return {s.target_id: s for s in _specs_from_mapped_entries(entries)}
        return {}

    @staticmethod
    async def get_active_daemon_ids() -> Set[str]:
        try:
            tunnel = await TunnelFactory.get_default()
            raw = await tunnel.get(ACTIVE_TARGETS_TUNNEL_KEY)
            if raw:
                return set(json.loads(raw))
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
        preflight_pattern: Optional[str] = None,
    ) -> MCPServerConnector:
        if execution_target and not cls._is_network_url(execution_target) and " " not in execution_target:
            base_worker_id = base_worker_id or execution_target
            execution_target = None

        lookup_id = base_worker_id or target_id
        spec = (anchor_specs or {}).get(lookup_id)
        default_cmd, default_mode, default_guard = cls.resolve_worker_defaults(lookup_id)

        resolved_cmd = execution_target or (spec.exec_cmd if spec else None) or default_cmd
        if not resolved_cmd:
            raise ValueError(f"[E2EWorker] Cannot resolve execution_target for '{target_id}' (lookup='{lookup_id}').")

        is_net = cls._is_network_url(resolved_cmd)
        resolved_mode = mode or (spec.mode if spec else None) or ("multiplex" if is_net else default_mode)
        resolved_transport = (
            transport_type
            or ("network" if is_net else None)
            or (spec.transport if spec else None)
            or "stdio"
        )
        resolved_preflight = (
            preflight_pattern
            if preflight_pattern is not None
            else ((spec.preflight if spec else None) or default_guard)
        )

        return MCPServerConnector(
            target_id=target_id,
            execution_target=resolved_cmd,
            mode=resolved_mode,
            transport_type=resolved_transport,
            preflight_pattern=resolved_preflight,
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
            tag=tag,
        )

    @classmethod
    def mount_custom_targets(
        cls,
        pipeline_or_connectors: Any,
        targets: Mapping[str, Union[str, Tuple[str, str], None]],
        *,
        preflight_overrides: Optional[Dict[str, Optional[str]]] = None,
        tag: str = "E2EWorker",
    ) -> List[MCPServerConnector]:
        return cls._mount_sync(
            pipeline_or_connectors,
            targets,
            active_daemon_ids=set(),
            anchor_specs={},
            attach_live=False,
            preflight_overrides=preflight_overrides,
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
        tag: str = "E2EWorker",
    ) -> List[MCPServerConnector]:
        connectors_list: List[MCPServerConnector] = (
            pipeline_or_connectors.connectors
            if hasattr(pipeline_or_connectors, "connectors")
            else pipeline_or_connectors
        )
        preflight_map = preflight_overrides or {}
        mode_map = mode_overrides or {}
        target_map: Mapping[str, Any] = targets if isinstance(targets, Mapping) else {t: None for t in targets}

        spawned: List[MCPServerConnector] = []
        for tid, spec_val in target_map.items():
            if attach_live and tid in active_daemon_ids:
                log.info(f"[{tag}] Attached to live McpWorkerDaemon target: [{tid}] (Zero duplicate spawn)")
                continue

            exec_target = spec_val[0] if isinstance(spec_val, tuple) else spec_val
            mode_val = spec_val[1] if isinstance(spec_val, tuple) else mode_map.get(tid)

            conn = cls.build_connector(
                target_id=tid,
                anchor_specs=anchor_specs,
                execution_target=exec_target,
                mode=mode_val,
                preflight_pattern=preflight_map.get(tid),
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