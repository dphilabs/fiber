# fiber.gateway.bridge.action.connect
import os
import asyncio
from pathlib import Path
from typing import Dict, Optional, List, Any

import orjson
import typer
from fiber.node.mcp.connector import MCPServerConnector
from fiber.node.mcp.mapper.spec import (
    ACTIVE_TARGETS_TUNNEL_KEY,
    STANDALONE_ATTACHED_TUNNEL_KEY,
    ANCHOR_ROOT,
    ANCHOR_MCP_PATH,
    DEFAULT_WORKER_PACKAGE,
    BUILTIN_ALIASES,
    ResolvedTarget,
    _parse_fiber_tags,
    discover_builtin_workers,
    _resolve_all_builtin_specs,
    resolve_connector_spec,
    _specs_from_mapped_entries,
    _sync_to_anchor_config,
    _collect_specs_and_envs,
)

from xphi.node.space.tunnel.factory import TunnelFactory
from fiber.gateway.bridge.emitter import get_emitter

log = get_emitter("bridge.action.connect")


async def _notify_standalone_attachment(spec: ResolvedTarget, attach: bool):
    """McpWorkerDaemon이 직접 PID 생존 여부를 확인하고 레지스트리에 통합할 수 있도록 Standalone 상태를 기록/해제합니다."""
    try:
        tunnel = await TunnelFactory.get_default()
        raw = await tunnel.get(STANDALONE_ATTACHED_TUNNEL_KEY)
        attached: Dict[str, Dict[str, Any]] = orjson.loads(raw) if raw else {}
        if not isinstance(attached, dict):
            attached = {}

        if attach:
            tags = list(dict.fromkeys([*spec.tags, "STANDALONE"]))
            attached[spec.target_id] = {
                "pid": os.getpid(),
                "tags": tags,
                "mode": spec.mode,
                "transport": spec.transport,
            }
        else:
            existing = attached.get(spec.target_id)
            if isinstance(existing, dict) and existing.get("pid") == os.getpid():
                attached.pop(spec.target_id, None)

        await tunnel.set(STANDALONE_ATTACHED_TUNNEL_KEY, orjson.dumps(attached).decode("utf-8"))
    except Exception as e:
        log.debug(f"[Fiber] Standalone attachment notification skipped for {spec.target_id}: {e}")


async def _filter_unmounted_specs(specs: List[ResolvedTarget]) -> List[ResolvedTarget]:
    """데몬 또는 다른 살아있는 Standalone 프로세스에서 이미 떠 있는 타겟을 제외하여 중복 실행 방지"""
    try:
        tunnel = await TunnelFactory.get_default()
        raw = await tunnel.get(ACTIVE_TARGETS_TUNNEL_KEY)
        if raw:
            loaded = orjson.loads(raw)
            active_ids = set(loaded.keys()) if isinstance(loaded, dict) else set(loaded)
            unmounted = [s for s in specs if s.target_id not in active_ids]
            skipped = [s.target_id for s in specs if s.target_id in active_ids]
            if skipped:
                log.info(f"[Fiber] Skipping {len(skipped)} target(s) already active in McpWorkerDaemon: {skipped}")
            return unmounted
    except Exception:
        pass
    return specs


async def launch_connector_async(spec: ResolvedTarget):
    effective_tags = tuple(dict.fromkeys([*spec.tags, "STANDALONE"]))
    guard_badge = f", Pre-Flight: /{spec.preflight}/i" if spec.preflight else ""
    tags_badge = f", Tags: {list(effective_tags)}"
    log.info(
        f"[Fiber] Sublimating server [{spec.target_id}] into the MCP Server Connector "
        f"(Mode: {spec.mode.upper()}, Transport: {spec.transport.upper()}{guard_badge}{tags_badge})..."
    )
    daemon = MCPServerConnector(
        target_id=spec.target_id,
        execution_target=spec.exec_cmd,
        mode=spec.mode,
        transport_type=spec.transport,
        preflight_pattern=spec.preflight,
    )
    await _notify_standalone_attachment(spec, attach=True)
    try:
        await daemon.run()
    finally:
        await _notify_standalone_attachment(spec, attach=False)
        log.info(f"[Fiber] [{spec.target_id}] Disconnected from A2A Network.")


async def _launch_all_async(specs: List[ResolvedTarget]):
    if len(specs) == 1:
        await launch_connector_async(specs[0])
    else:
        await asyncio.gather(*(launch_connector_async(spec) for spec in specs))


def run_connect_copy(
    agent: Optional[str] = None,
    exec_cmd: Optional[str] = None,
    config: Optional[Path] = None,
    target: Optional[str] = None,
    mode: Optional[str] = None,
    preflight: Optional[str] = None,
) -> Path:
    if agent is None and exec_cmd is None and config is None:
        log.error(
            "[Fiber] Specify an agent to sync (e.g., 'fiber connect claude', 'fiber connect all'), "
            "--config (-c), or --exec (-e)."
        )
        raise typer.Exit(1)

    specs, env_lookup = _collect_specs_and_envs(
        agent=agent,
        exec_cmd=exec_cmd,
        config=config,
        target=target,
        mode=mode,
        preflight=preflight,
    )
    if not specs:
        log.error("[Fiber] No MCP targets found to sync.")
        raise typer.Exit(1)

    out_path = _sync_to_anchor_config(specs, env_lookup=env_lookup)
    for s in specs:
        guard_info = f" (Pre-Flight: {s.preflight})" if s.preflight else ""
        tags_info = f" (Tags: {','.join(s.tags)})" if s.tags else ""
        log.info(f"  ├─ [{s.target_id}] -> {s.exec_cmd} [{s.mode.upper()}]{guard_info}{tags_info}")
    return out_path or ANCHOR_MCP_PATH


def run_connect_standalone(
    exec_cmd: Optional[str] = None,
    config: Optional[Path] = None,
    target: Optional[str] = None,
    mode: Optional[str] = None,
    preflight: Optional[str] = None,
):
    # standalone 실행 시 외부 인자가 없으면 오직 ~/.anchor/mcp.json("fiber")에 등록된 타겟만 실행
    default_agent = "fiber" if (exec_cmd is None and config is None) else None
    specs, _ = _collect_specs_and_envs(
        agent=default_agent,
        exec_cmd=exec_cmd,
        config=config,
        target=target,
        mode=mode,
        preflight=preflight,
    )
    if not specs:
        log.error(
            "[Fiber] No valid MCP target resolved for standalone execution. "
            "Provide --exec (-e), --config (-c), or register targets via 'fiber connect'."
        )
        raise typer.Exit(1)

    async def _orchestrate_launch():
        runnable_specs = await _filter_unmounted_specs(specs)
        if not runnable_specs:
            log.info("[Fiber] All resolved targets are already running under McpWorkerDaemon.")
            return
        await _launch_all_async(runnable_specs)

    try:
        asyncio.run(_orchestrate_launch())
    except KeyboardInterrupt:
        log.info("\n[Fiber] Connector shutting down...")