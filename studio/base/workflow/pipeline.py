# fiber.studio.base.workflow.pipeline
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Dict, List, Optional

import uvicorn

from fiber.studio.base.workflow.config import (
    E2EConfig,
    ManagedTestServer,
    PipelineRunner,
    TestResult,
)
from fiber.gateway.bridge.action.connect import resolve_connector_spec
from fiber.gateway.payload import Config, create_app
from fiber.node.mcp.connector import MCPServerConnector

from xphi.arch.bound.client.rpc import InternalRpcClient
import xphi.node.ops.daemon.rpc as daemon_module
from xphi.node.ops.daemon.rpc import RpcWorkerDaemon
import xphi.node.rpc.registry as rpc_registry
from xphi.node.space.tunnel.factory import TunnelFactory
from xphi.state.anchor.store import AnchorRole, AnchorStore
from xphi.watcher.plane.emitter import get_emitter

log = get_emitter("infra.e2e.bridge")


class MockStream:
    def __init__(self, handle_id: str, target: str, fuel: int):
        self.id = handle_id
        self.metadata = {"target": target, "fuel_locked": fuel, "risk_score": 0}


class _MockStateRecord:
    """Unified in-memory state record replacing repetitive inner DummyState classes."""

    def __init__(self, action: str, metadata: Dict[str, Any], payload: Any = None):
        self.action = action
        self.metadata = metadata
        self.payload = payload

class MockAnchorStore:
    def __init__(self):
        self.states: Dict[str, _MockStateRecord] = {}
        self.yield_timestamps: Dict[str, float] = {}
        self.stale_timeout = 60.0

    def _resolve_target(self, handle_id: str, explicit_target: Optional[str] = None) -> str:
        if explicit_target:
            return explicit_target
        prev = self.states.get(handle_id)
        if prev and isinstance(prev.metadata, dict):
            return prev.metadata.get("target", "legacy-01")
        return "legacy-01"

    async def query_state(self, handle_id: str) -> Optional[_MockStateRecord]:
        return self.states.get(handle_id)

    def set_pending(self, handle_id: str, payload: dict, target_server_id: str = "legacy-01") -> None:
        self.states[handle_id] = _MockStateRecord(
            action="transition.pending",
            metadata={"status": "PENDING", "target": target_server_id},
            payload=payload,
        )

    def set_resolved(self, handle_id: str, payload: dict, target_server_id: Optional[str] = None) -> None:
        target = self._resolve_target(handle_id, target_server_id)
        self.states[handle_id] = _MockStateRecord(
            action="transition.resolve",
            metadata={"status": "RESOLVED", "target": target},
            payload=payload,
        )
        self.yield_timestamps.pop(handle_id, None)

    def set_faulted(self, handle_id: str, error_detail: str, target_server_id: Optional[str] = None) -> None:
        target = self._resolve_target(handle_id, target_server_id)
        self.states[handle_id] = _MockStateRecord(
            action="transition.resolve",
            metadata={"status": "FAULTED", "error_detail": error_detail, "target": target},
            payload=None,
        )
        self.yield_timestamps.pop(handle_id, None)

    def set_yielded(self, handle_id: str, payload: dict, target_server_id: Optional[str] = None) -> None:
        target = self._resolve_target(handle_id, target_server_id)
        self.states[handle_id] = _MockStateRecord(
            action="transition.yield",
            metadata={"status": "YIELD", "target": target},
            payload=payload,
        )
        self.yield_timestamps[handle_id] = time.time()

    async def query_stale_streams(self, current_time: float, thresholds: dict) -> List[MockStream]:
        effective_ttl = min(
            self.stale_timeout,
            thresholds.get("transition.pending", self.stale_timeout),
        )
        stale: List[MockStream] = []
        for hid, ts in list(self.yield_timestamps.items()):
            if current_time - ts > effective_ttl:
                stale.append(MockStream(hid, self._resolve_target(hid), 100))
        return stale

    async def force_transition(self, handle_id: str, new_action: str, metadata: dict) -> None:
        merged_meta = {"target": self._resolve_target(handle_id), **(metadata or {})}
        self.states[handle_id] = _MockStateRecord(action=new_action, metadata=merged_meta, payload={})
        self.yield_timestamps.pop(handle_id, None)

class BasePipeline(PipelineRunner):
    """
    Manages the full lifecycle (Custom Context -> RPC Bus -> REST Edge -> Connectors -> Phases -> Teardown)
    and provides built-in connector & Pub/Sub synchronization primitives.
    """

    def __init__(self, config: E2EConfig, name: str, scope_name: str):
        super().__init__(name=name, scope_name=scope_name)
        self.config = config
        self.local_url = f"{self.config.protocol}://127.0.0.1:{self.config.port}"

        self.anchor_store: Optional[AnchorStore] = None
        self.rpc: Optional[InternalRpcClient] = None
        self.external_node_active: bool = False

        self.connectors: List[MCPServerConnector] = []
        self._connector_tasks: List[asyncio.Task] = []
        self.server: Optional[ManagedTestServer] = None
        self._server_task: Optional[asyncio.Task] = None
        self.worker_daemon: Optional[RpcWorkerDaemon] = None
        self._worker_task: Optional[asyncio.Task] = None

        self.captured_handle_ids: Dict[str, str] = {}

    @property
    def _active_store(self) -> Any:
        return self.anchor_store

    # --- Lifecycle Hooks (Overridden by E2E Subclasses) ---
    async def setup_custom_context(self) -> None:
        pass

    async def setup_workers(self) -> None:
        pass

    async def teardown_custom(self) -> None:
        pass

    # --- Built-in E2E Primitives (Available to all Pipelines) ---
    def add_connector(
        self,
        target_id: str,
        exec_cmd: str,
        *,
        mode: Optional[str] = None,
        preflight: Optional[str] = None,
    ) -> MCPServerConnector:
        """Resolves connector spec and registers an MCPServerConnector instance."""
        spec = resolve_connector_spec(target=target_id, exec_cmd=exec_cmd, mode=mode, preflight=preflight)
        conn = MCPServerConnector(
            target_id=spec.target_id,
            execution_target=spec.exec_cmd,
            mode=spec.mode,
            transport_type=spec.transport,
            preflight_pattern=spec.preflight,
        )
        self.connectors.append(conn)
        return conn

    def _get_connector(self, target_id: str) -> Optional[MCPServerConnector]:
        return next((c for c in self.connectors if c.target_id == target_id), None)

    async def wait_for_intent_reply(
        self,
        handle_id: str,
        timeout: float = 15.0,
        trigger_coro: Optional[Callable[[], Any]] = None,
    ) -> Dict[str, Any]:
        """
        Subscribes to `mcp.intent.reply.<handle_id>` BEFORE running optional `trigger_coro`
        to eliminate race conditions, then awaits ledger state resolution.
        """
        tunnel = await TunnelFactory.get_default()
        pubsub_channel = f"mcp.intent.reply.{handle_id}"
        pubsub = tunnel.pubsub()
        await pubsub.subscribe(pubsub_channel)

        async def _listen() -> Dict[str, Any]:
            while True:
                try:
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if msg and msg.get("type") == "message":
                        raw = msg["data"]
                        return json.loads(raw) if isinstance(raw, (str, bytes)) else raw
                except TimeoutError:
                    continue
                await asyncio.sleep(0.05)

        try:
            if trigger_coro is not None:
                res = trigger_coro()
                if asyncio.iscoroutine(res):
                    await res
            return await asyncio.wait_for(_listen(), timeout=timeout)
        finally:
            try:
                await pubsub.unsubscribe(pubsub_channel)
                await pubsub.close()
            except Exception:
                pass

    # --- Internal Infrastructure Setup & Teardown ---
    async def _is_live_node_running(self) -> bool:
        if self.anchor_store and getattr(self.anchor_store, "role", None) == AnchorRole.PROPOSER:
            return True
        try:
            tunnel = await TunnelFactory.get_default()
            raw = await tunnel.get("mcp:registry:active_targets")
            if raw:
                return True
        except Exception:
            pass
        return False

    async def _setup_rpc_bus(self) -> None:
        self.rpc = InternalRpcClient()
        self.anchor_store = AnchorStore()
        self.external_node_active = await self._is_live_node_running()

        if self.external_node_active:
            log.info(
                "[BasePipeline] Live Master Node detected (Operating as PROPOSER). "
                "Delegating RPC & Real AnchorStore transitions directly to live node."
            )
        else:
            store_ref = self.anchor_store

            class RealDaemonCtx:
                broker = None
                store = store_ref

            self.worker_daemon = RpcWorkerDaemon(ctx=RealDaemonCtx())
            self.worker_daemon.running = True
            self._worker_task = asyncio.create_task(self.worker_daemon.run())
            log.info("[BasePipeline] Standalone Mode: Real AnchorStore (SEALER) & local RpcWorkerDaemon ignited.")

    async def _setup_rest_edge(self) -> None:
        tunnel = await TunnelFactory.get_default()
        rest_app = create_app(
            config=Config(wasm_timeout=5.0),
            tunnel=tunnel,
            anchor_store=self._active_store,
        )
        u_config = uvicorn.Config(
            app=rest_app,
            host="127.0.0.1",
            port=self.config.port,
            log_level="error",
            access_log=False,
        )
        self.server = ManagedTestServer(u_config)
        self._server_task = asyncio.create_task(self.server.serve())
        pipeline_label = "MockPipeline" if isinstance(self, MockPipeline) else "BasePipeline"
        log.info(f"[{pipeline_label}] REST Edge Bootstrapped.")

    async def _teardown_rpc_bus(self) -> None:
        if self.anchor_store and not self.external_node_active:
            try:
                self.anchor_store.close()
            except Exception:
                pass

    async def run_pipeline(self) -> List[TestResult]:
        log.info(f"\n=== Starting Suite: {self.name} ===")

        await self.setup_custom_context()
        await self._setup_rpc_bus()
        await self._setup_rest_edge()
        await self.setup_workers()
        await asyncio.sleep(1.0)

        log.info(f"[BasePipeline] Igniting {len(self.connectors)} Connectors...")
        for conn in self.connectors:
            self._connector_tasks.append(asyncio.create_task(conn.run()))
        if self.connectors:
            await asyncio.sleep(2.0)

        results: List[TestResult] = []
        try:
            for idx, phase in enumerate(self.phases, 1):
                log.info(f"\n[PHASE {idx}/{len(self.phases)}] {phase.name}")
                try:
                    await phase.action()
                    results.append(TestResult(self.scope_name, phase.name, True, True))
                    log.info("  └─ Status: PASSED")
                except Exception as e:
                    log.error(f"  └─ Halted: {str(e)} X", exc_info=True)
                    results.append(TestResult(self.scope_name, phase.name, False, True))
        finally:
            await self._teardown_all()

        return results

    async def _teardown_all(self) -> None:
        log.info("\n[BasePipeline] Teardown sequence initiated...")

        await self.teardown_custom()
        for conn in self.connectors:
            conn.running = False
        for task in self._connector_tasks:
            if not task.done():
                task.cancel()
        if self._connector_tasks:
            await asyncio.gather(*self._connector_tasks, return_exceptions=True)

        if self.worker_daemon:
            self.worker_daemon.running = False
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            await asyncio.gather(self._worker_task, return_exceptions=True)

        if self.server:
            self.server.should_exit = True
        if self._server_task:
            await asyncio.gather(self._server_task, return_exceptions=True)

        await self._teardown_rpc_bus()
        log.info("[BasePipeline] Teardown complete.")


class MockPipeline(BasePipeline):
    """
    In-Memory AnchorStore & Hybrid RPC Interception Pipeline.
    Inherits the unified lifecycle from BasePipeline while isolating ledger state in `self.mock_store`.
    """

    def __init__(self, config: E2EConfig, name: str, scope_name: str):
        super().__init__(config=config, name=name, scope_name=scope_name)
        self.mock_store = MockAnchorStore()
        self._original_rpc_call: Optional[Callable] = None
        self._original_registry_builder: Optional[Callable] = None

    @property
    def _active_store(self) -> Any:
        return self.mock_store

    async def _setup_rpc_bus(self) -> None:
        self.rpc = InternalRpcClient()
        tunnel = await TunnelFactory.get_default()

        async def mock_mcp_state_query(params: dict, ctx: Any = None):
            handle_id = params.get("handle_id")
            state = await self.mock_store.query_state(handle_id)
            if not state:
                return {"exists": False}

            meta = getattr(state, "metadata", {})
            return {
                "exists": True,
                "status": meta.get("status"),
                "target_server_id": meta.get("target", "legacy-01"),
                "error_detail": meta.get("error_detail", ""),
                "executable_payload": getattr(state, "payload", {}),
                "action": getattr(state, "action", meta.get("action", "")),
            }

        async def mock_mcp_state_pending_seal(params: dict, ctx: Any = None):
            handle_id = params.get("handle_id")
            payload = params.get("payload", {})
            target_server_id = params.get("target_server_id") or params.get("target") or "legacy-01"
            if handle_id:
                self.captured_handle_ids["latest"] = handle_id
                self.mock_store.set_pending(handle_id, payload, target_server_id)
            return {"success": True, "handle_id": handle_id}

        async def mock_resolve_state(params: dict, ctx: Any = None):
            handle_id = params.get("handle_id")
            status = params.get("status")
            payload = params.get("executable_payload", {})
            error_detail = params.get("error_detail", "Unknown Fault")
            target_server_id = params.get("target_server_id") or params.get("target")

            self.captured_handle_ids["latest"] = handle_id

            if status == "RESOLVED":
                self.mock_store.set_resolved(handle_id, payload, target_server_id)
            elif status == "YIELD":
                self.mock_store.set_yielded(handle_id, payload, target_server_id)
            else:
                self.mock_store.set_faulted(handle_id, error_detail, target_server_id)

            await tunnel.publish(
                f"mcp.intent.reply.{handle_id}",
                json.dumps({
                    "status": status,
                    "executable_payload": payload,
                    "error_detail": error_detail,
                }),
            )
            return {"success": True, "status": status}

        ephemeral_state_handlers = {
            "mcp.state.query": mock_mcp_state_query,
            "mcp.state.pending.seal": mock_mcp_state_pending_seal,
            "mcp.bridge.resolve_state": mock_resolve_state,
        }

        self._original_rpc_call = InternalRpcClient.call
        orig_call_ref = self._original_rpc_call

        async def _hybrid_live_node_rpc_call(client_self, method: Any, params: dict = None, *args, **kwargs):
            method_key = getattr(method, "value", str(method))
            if method_key in ephemeral_state_handlers:
                return await ephemeral_state_handlers[method_key](params or {})
            return await orig_call_ref(client_self, method, params, *args, **kwargs)

        InternalRpcClient.call = _hybrid_live_node_rpc_call

        self._original_registry_builder = rpc_registry.build_internal_rpc_registry
        orig_builder_ref = self._original_registry_builder

        def bridge_mock_registry_builder(*args, **kwargs):
            registry = orig_builder_ref(*args, **kwargs)
            registry.update(ephemeral_state_handlers)
            return registry

        rpc_registry.build_internal_rpc_registry = bridge_mock_registry_builder
        daemon_module.build_internal_rpc_registry = bridge_mock_registry_builder

        class MockDaemonCtx:
            broker = None
            store = None

        self.worker_daemon = RpcWorkerDaemon(ctx=MockDaemonCtx())
        self.worker_daemon.running = True
        self._worker_task = asyncio.create_task(self.worker_daemon.run())

        log.info("[MockPipeline] RPC Bus & Core Daemons Ignited (Mock AnchorStore & Real Validators Aligned).")

    async def _teardown_rpc_bus(self) -> None:
        if self._original_rpc_call:
            InternalRpcClient.call = self._original_rpc_call
            self._original_rpc_call = None

        if self._original_registry_builder:
            rpc_registry.build_internal_rpc_registry = self._original_registry_builder
            daemon_module.build_internal_rpc_registry = self._original_registry_builder
            self._original_registry_builder = None