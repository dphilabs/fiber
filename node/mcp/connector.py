# fiber.node.mcp.connector
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
import sys
from typing import Any, Dict, Optional, Protocol, Set

import orjson

from fiber.gateway.bridge.emitter import get_emitter
from fiber.node.mcp.fsm import IntentLifecycleFSM, IntentState
from fiber.node.mcp.quarantine import AdapterRegistry
from fiber.studio.harness.mcp.payload import McpPayloadBuilder
from xphi.arch.bound.client.rpc import InternalRpcClient
from xphi.node.space.tunnel.factory import TunnelFactory

log = get_emitter("mcp.connector")

_REMOTE_SCHEMES = ("http://", "https://", "sse://", "ws://", "wss://")


class MCPServerTransport(Protocol):
    async def start(self) -> None: ...
    async def send_payload(self, safe_payload: Dict[str, Any]) -> None: ...
    async def receive_raw(self) -> str: ...
    async def read_egress_stream(self) -> bytes: ...
    async def close(self) -> None: ...
    process: Any


def _format_error_detail(err: Any) -> str:
    """JSON-RPC error dict가 파이썬 repr 문자열로 깨지지 않도록 메시지를 정제합니다."""
    if isinstance(err, dict):
        code = err.get("code")
        msg = err.get("message") or str(err)
        data = err.get("data")
        suffix = f" ({data})" if data is not None else ""
        return f"[RPC {code}] {msg}{suffix}" if code is not None else f"{msg}{suffix}"
    return str(err) if err else "Unknown Execution Fault"


class MCPServerConnector:
    def __init__(
        self,
        target_id: str,
        execution_target: str,
        mode: str = "ephemeral",
        transport_type: str = "stdio",
        preflight_pattern: Optional[str] = None,
        *,
        env: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
    ):
        self.target_id = target_id
        self.execution_target = execution_target.strip()
        self.mode = mode.lower().strip()
        self.transport_type = transport_type.lower().strip()

        if self.mode not in ("ephemeral", "linear", "multiplex"):
            raise ValueError(f"Invalid mode: {self.mode}. Must be ephemeral, linear, or multiplex.")

        if self.transport_type not in ("stdio", "network"):
            raise ValueError(f"Invalid transport: {self.transport_type}. Must be stdio or network.")

        self.is_remote_url = (
            self.transport_type == "network"
            and self.execution_target.startswith(_REMOTE_SCHEMES)
        )
        self.target_env = dict(env) if env else None
        self.target_headers = dict(headers) if headers else None

        self.tunnel = None
        self.rpc = InternalRpcClient()
        self.fsm = IntentLifecycleFSM(self.target_id, self.rpc, preflight_pattern=preflight_pattern)
        self.listen_channel = f"mcp.intent.queue.{self.target_id}"
        self.running = False

        self.quarantine = AdapterRegistry.get_adapter(
            self.target_id,
            is_remote=self.is_remote_url,
            env=self.target_env,
            headers=self.target_headers,
        )

        self.active_sandboxes: Dict[str, MCPServerTransport] = {}
        self.shared_transport: Optional[MCPServerTransport] = None
        self.pending_requests: Dict[str, asyncio.Future] = {}

        self.linear_lock = asyncio.Lock()
        self._shared_boot_lock = asyncio.Lock()
        self._shared_write_lock = asyncio.Lock()

        self._listener_task: Optional[asyncio.Task] = None
        self._bg_tasks: Set[asyncio.Task] = set()

    def _spawn_bg_task(self, coro: Any) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def _create_transport(self, handle_id: str) -> MCPServerTransport:
        if self.transport_type == "network":
            from xphi.state.phase.network.transport import NetworkTransport
            return NetworkTransport(execution_target=self.execution_target, handle_id=handle_id)
        else:
            from xphi.state.phase.network.transport import StdioTransport
            return StdioTransport(command=self.execution_target, handle_id=handle_id)

    def _is_shared_transport_alive(self) -> bool:
        if self.shared_transport is None:
            return False
        proc = getattr(self.shared_transport, "process", None)
        if proc is not None and getattr(proc, "returncode", None) is not None:
            return False
        if self._listener_task is None or self._listener_task.done():
            return False
        return True

    async def _ensure_shared_transport(self) -> MCPServerTransport:
        """공유 트랜스포트(linear/multiplex) 생존을 보장하고 비정상 종료 시 자동으로 재기동합니다."""
        if self._is_shared_transport_alive():
            assert self.shared_transport is not None
            return self.shared_transport

        async with self._shared_boot_lock:
            if self._is_shared_transport_alive():
                assert self.shared_transport is not None
                return self.shared_transport

            if self.shared_transport is not None:
                log.warning(f"[Connector:{self.target_id}] Shared worker dead or disconnected. Respawning...")
                try:
                    await self.shared_transport.close()
                except Exception:
                    pass
                self.shared_transport = None

            if self._listener_task and not self._listener_task.done():
                self._listener_task.cancel()
                await asyncio.gather(self._listener_task, return_exceptions=True)

            transport = self._create_transport(f"shared-{self.target_id}")
            await transport.start()
            self.shared_transport = transport
            self._listener_task = asyncio.create_task(self._shared_stdout_listener(transport))
            return transport

    async def _send_shared_payload(self, transport: MCPServerTransport, payload: Dict[str, Any]) -> None:
        async with self._shared_write_lock:
            await transport.send_payload(payload)

    def _prepare_ingress_payload(
        self, handle_id: str, payload: Dict[str, Any], is_resume: bool = False
    ) -> Dict[str, Any]:
        """
        내부 워커(AsyncWorkerProtocol)의 RESUME 언래핑 규격({"action": "RESUME", "payload": {...}})과
        원격 표준 MCP 서버(params._meta.fiber_action) 규격을 정확히 분리하여 원본 prompt_id 유실을 방지합니다.
        """
        if is_resume:
            if not self.is_remote_url:
                rpc_payload = {
                    "id": handle_id,
                    "action": "RESUME",
                    "payload": dict(payload),
                }
            else:
                rpc_payload = dict(payload)
                rpc_payload["id"] = handle_id
                if isinstance(rpc_payload.get("params"), dict):
                    params_copy = dict(rpc_payload["params"])
                    meta_copy = dict(params_copy.get("_meta", {}))
                    meta_copy["fiber_action"] = "RESUME"
                    params_copy["_meta"] = meta_copy
                    rpc_payload["params"] = params_copy
        else:
            rpc_payload = dict(payload)
            rpc_payload["id"] = handle_id

        return self.quarantine.translate_ingress(rpc_payload)

    async def _delegate_to_core(self, rpc_req: Dict[str, Any]) -> Dict[str, Any]:
        call_id = rpc_req.get("id")
        params = rpc_req.get("params") or {}
        target_method = params.get("target_method")
        req_data = params.get("data") or {}

        try:
            core_res = await self.rpc.call(target_method, req_data)
            return McpPayloadBuilder.result(call_id, core_res)
        except Exception as e:
            log.error(f"[Connector] Failed to delegate agent RPC ({target_method}): {e}")
            return McpPayloadBuilder.error(call_id, -32000, f"Core delegation failure: {e}")

    @staticmethod
    def _is_elicitation(response: Dict[str, Any]) -> bool:
        method = response.get("method")
        return bool(method and "elicitation" in str(method))

    def _clear_pending_future(self, handle_id: str) -> None:
        future = self.pending_requests.pop(handle_id, None)
        if future and not future.done():
            future.cancel()

    def _fail_all_pending_futures(self, reason: str) -> None:
        if not self.pending_requests:
            return
        err = RuntimeError(reason)
        for handle_id in list(self.pending_requests.keys()):
            fut = self.pending_requests.pop(handle_id, None)
            if fut and not fut.done():
                fut.set_exception(err)

    async def run(self) -> None:
        self.tunnel = await TunnelFactory.get_default()
        pubsub = self.tunnel.pubsub()
        await pubsub.subscribe(self.listen_channel)

        self.running = True
        log.info(
            f"[Connector:{self.target_id}] Listening for Intents on DPHI Bus "
            f"(Mode: {self.mode.upper()}, Transport: {self.transport_type.upper()})"
        )

        try:
            if self.mode in ("linear", "multiplex"):
                await self._ensure_shared_transport()

            async for msg in pubsub.listen():
                if not self.running:
                    break
                if msg and msg["type"] == "message":
                    try:
                        intent_data = orjson.loads(msg["data"])
                    except Exception as parse_err:
                        log.error(f"[Connector:{self.target_id}] Malformed intent JSON ignored: {parse_err}")
                        continue
                    self._spawn_bg_task(self.process_intent(intent_data))

        except asyncio.CancelledError:
            log.info("[Connector] Shutdown signal received.")
        except Exception as e:
            log.error(f"[Connector] Fatal Bus Error: {e}", exc_info=True)
        finally:
            self.running = False
            await pubsub.unsubscribe(self.listen_channel)
            await pubsub.close()

            for handle_id in list(self.pending_requests.keys()):
                self._clear_pending_future(handle_id)
            self.pending_requests.clear()

            if self._bg_tasks:
                for t in list(self._bg_tasks):
                    t.cancel()
                await asyncio.gather(*self._bg_tasks, return_exceptions=True)
                self._bg_tasks.clear()

            if self.shared_transport:
                await self.shared_transport.close()
                self.shared_transport = None
            if self._listener_task and not self._listener_task.done():
                self._listener_task.cancel()
                await asyncio.gather(self._listener_task, return_exceptions=True)

            for transport in list(self.active_sandboxes.values()):
                await transport.close()
            self.active_sandboxes.clear()

    async def _shared_stdout_listener(self, transport: MCPServerTransport) -> None:
        eof_unexpected = False
        while self.running and self.shared_transport is transport:
            try:
                raw_output = await transport.read_egress_stream()
                if not raw_output:
                    eof_unexpected = self.running
                    break

                raw_str = raw_output.decode("utf-8", errors="replace").strip()
                if not raw_str:
                    continue

                response = self.quarantine.translate_egress(raw_str)
                if not response or not isinstance(response, dict):
                    continue

                req_id = response.get("id")

                if response.get("method") == "rpc_delegate":
                    self._spawn_bg_task(self._handle_agent_rpc_delegation(response, transport))
                    continue

                if self._is_elicitation(response):
                    canonical_id = str(req_id).removeprefix("prompt_")
                    log.warning(f"[Connector] TRAP: Elicitation (YIELD) detected for {canonical_id}")
                    if self.mode == "linear":
                        log.error("FATAL: Linear mode worker yielded! This blocks the entire queue.")

                    await self.fsm.transition(canonical_id, IntentState.YIELDED, executable_payload=response)
                    self._clear_pending_future(canonical_id)
                    continue

                future = self.pending_requests.pop(req_id, None)
                if future is None and req_id is not None:
                    future = self.pending_requests.pop(str(req_id), None)

                if future and not future.done():
                    future.set_result(response)
                else:
                    log.debug(f"[Connector] Unmatched or already resolved response ID: {req_id}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"[Connector] Shared Listener Fracture: {e}")
                await asyncio.sleep(0.1)

        if eof_unexpected:
            log.warning(f"[Connector:{self.target_id}] Shared worker stdout reached unexpected EOF.")
            self._fail_all_pending_futures(f"Shared worker '{self.target_id}' terminated unexpectedly (EOF).")

    async def _handle_agent_rpc_delegation(
        self, rpc_req: Dict[str, Any], transport: Optional[MCPServerTransport] = None
    ) -> None:
        active_transport = transport or self.shared_transport
        if not active_transport:
            log.error(
                f"[Connector] Cannot send RPC delegation feedback for {rpc_req.get('id')}: shared_transport is None."
            )
            return
        feedback_payload = await self._delegate_to_core(rpc_req)
        await self._send_shared_payload(active_transport, feedback_payload)

    # =========================================================================
    # Intent Routing & Mode-Unified Execution / Resume Handlers
    # =========================================================================
    async def process_intent(self, intent_data: Dict[str, Any]) -> None:
        handle_id = intent_data.get("handle_id")
        if not handle_id:
            return

        action = intent_data.get("action", "EXECUTE")
        payload = intent_data.get("payload", {})

        try:
            if action == "EXECUTE":
                await self._handle_execute(handle_id, payload)
            elif action == "RESUME":
                await self._handle_resume(handle_id, payload)
            elif action in ("RESUME_OR_KILL", "FORCE_ROLLBACK"):
                await self._handle_rollback(handle_id)

        except Exception as e:
            await self._handle_intent_crash(handle_id, e)

    async def _dispatch_fresh_execution(self, handle_id: str, payload: Dict[str, Any]) -> None:
        """최초 EXECUTE 및 Pre-Flight 승인 후 방출된 원본 페이로드를 현재 모드에 맞춰 실행합니다."""
        if self.mode == "ephemeral":
            await self._execute_ephemeral(handle_id, payload)
        elif self.mode == "linear":
            await self._execute_shared(handle_id, payload, use_lock=True, is_resume=False)
        elif self.mode == "multiplex":
            await self._execute_shared(handle_id, payload, use_lock=False, is_resume=False)

    async def _handle_execute(self, handle_id: str, payload: Dict[str, Any]) -> None:
        if await self.fsm.check_preflight_trap(handle_id, payload):
            return
        if await self.fsm.transition(handle_id, IntentState.EXECUTING):
            await self._dispatch_fresh_execution(handle_id, payload)

    async def _handle_resume(self, handle_id: str, payload: Dict[str, Any]) -> None:
        # 1. Pre-Flight Guard에 의해 파킹된 요청인지 먼저 검증 및 원본 추출
        verified_preflight_payload = await self.fsm.verify_preflight_resume(handle_id, payload)

        # Case A: Pre-Flight Release (워커는 아직 실행된 적 없으므로 모든 모드에서 신규 실행과 동일하게 디스패치)
        if verified_preflight_payload is not None:
            if await self.fsm.transition(handle_id, IntentState.RESUMING):
                log.info(f"[Connector] Releasing Pre-Flight approved intent ({self.mode.upper()}): {handle_id}")
                await self._dispatch_fresh_execution(handle_id, verified_preflight_payload)
            return

        # Case B: Native Worker Resume (워커 내부에서 직접 elicitation YIELD를 발생시킨 경우)
        await self._resume_parked_worker(handle_id, payload)

    async def _resume_parked_worker(self, handle_id: str, resume_payload: Dict[str, Any]) -> None:
        if self.mode == "linear":
            log.warning(f"Native worker RESUME not supported in Linear Mode. Ignoring {handle_id}.")
            return

        if not await self.fsm.transition(handle_id, IntentState.RESUMING):
            return

        if self.mode == "multiplex":
            if not self._is_shared_transport_alive():
                log.error(f"Cannot RESUME {handle_id}: Shared multiplex worker died while parked.")
                await self.fsm.transition(
                    handle_id, IntentState.FAULTED, error_detail="Shared worker terminated before RESUME"
                )
                self.fsm.cleanup(handle_id)
                return
            log.info(f"[Connector] Multiplexing native RESUME to Shared Transport: {handle_id}")
            await self._execute_shared(handle_id, resume_payload, use_lock=False, is_resume=True)
        elif self.mode == "ephemeral":
            transport = self.active_sandboxes.get(handle_id)
            if not transport:
                log.error(f"Cannot RESUME {handle_id}: Parked sandbox not found.")
                await self.fsm.transition(handle_id, IntentState.FAULTED, error_detail="Sandbox not found for RESUME")
                self.fsm.cleanup(handle_id)
                return
            log.info(f"[Connector] Resuming Parked Ephemeral Intent: {handle_id}")
            await self._cycle_io(handle_id, resume_payload, transport, is_resume=True)

    async def _handle_rollback(self, handle_id: str) -> None:
        if await self.fsm.transition(
            handle_id, IntentState.ROLLED_BACK, error_detail="SYSTEM_SENTINEL_TIMEOUT: Request aborted."
        ):
            self._clear_pending_future(handle_id)
            if self.mode == "ephemeral":
                log.warning(f"[Connector] Sentinel enforced ROLLBACK on {handle_id}. Terminating parked sandbox.")
                await self._destroy_sandbox(handle_id)
            self.fsm.cleanup(handle_id)

    async def _handle_intent_crash(self, handle_id: str, exc: Exception) -> None:
        if not self.running:
            log.debug(f"[Connector] Intent {handle_id} aborted cleanly due to graceful shutdown (Expected EOF).")
            return

        if self.fsm.get_state(handle_id) == IntentState.ROLLED_BACK:
            log.debug(f"[Connector] Suppressed expected EOF for rolled-back intent: {handle_id}")
            return

        log.error(f"[Connector] Lifecycle Crash for {handle_id}: {exc}", exc_info=True)
        await self.fsm.transition(handle_id, IntentState.FAULTED, error_detail=str(exc))
        self._clear_pending_future(handle_id)
        if self.mode == "ephemeral":
            await self._destroy_sandbox(handle_id)
        self.fsm.cleanup(handle_id)

    # =========================================================================
    # Low-Level Transport Execution Primitives
    # =========================================================================
    async def _execute_shared(
        self, handle_id: str, payload: Dict[str, Any], use_lock: bool, is_resume: bool = False
    ) -> None:
        safe_payload = self._prepare_ingress_payload(handle_id, payload, is_resume=is_resume)

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending_requests[handle_id] = future

        try:
            if use_lock:
                async with self.linear_lock:
                    transport = await self._ensure_shared_transport()
                    await self._send_shared_payload(transport, safe_payload)
                    await self._wait_and_resolve_shared(handle_id, future)
            else:
                transport = await self._ensure_shared_transport()
                await self._send_shared_payload(transport, safe_payload)
                await self._wait_and_resolve_shared(handle_id, future)
        except Exception:
            self._clear_pending_future(handle_id)
            raise

    async def _wait_and_resolve_shared(self, handle_id: str, future: asyncio.Future) -> None:
        try:
            response = await future
            if "error" in response:
                await self.fsm.transition(
                    handle_id, IntentState.FAULTED, error_detail=_format_error_detail(response["error"])
                )
            else:
                await self.fsm.transition(handle_id, IntentState.RESOLVED, executable_payload=response)
        except asyncio.CancelledError:
            log.warning(f"Shared request {handle_id} was cancelled or yielded.")
        finally:
            if self.fsm.get_state(handle_id) in self.fsm.TERMINAL_STATES:
                self.fsm.cleanup(handle_id)

    async def _execute_ephemeral(self, handle_id: str, payload: Dict[str, Any]) -> None:
        if handle_id in self.active_sandboxes:
            log.warning(f"[Connector] Duplicate EXECUTE ignored for {handle_id}")
            return

        transport = self._create_transport(handle_id)
        await transport.start()
        self.active_sandboxes[handle_id] = transport

        await self._cycle_io(handle_id, payload, transport, is_resume=False)

    async def _cycle_io(
        self, handle_id: str, payload: Dict[str, Any], transport: MCPServerTransport, is_resume: bool = False
    ) -> None:
        try:
            safe_payload = self._prepare_ingress_payload(handle_id, payload, is_resume=is_resume)
            await transport.send_payload(safe_payload)

            raw_output = await transport.receive_raw()
            if not raw_output:
                raise RuntimeError("Ephemeral worker exited without output (EOF).")
            response = self.quarantine.translate_egress(raw_output)
            if not isinstance(response, dict):
                raise RuntimeError(f"Malformed worker egress payload: {raw_output!r}")

            while response.get("method") == "rpc_delegate":
                feedback_payload = await self._delegate_to_core(response)
                await transport.send_payload(feedback_payload)
                raw_output = await transport.receive_raw()
                if not raw_output:
                    raise RuntimeError("Ephemeral worker exited mid-delegation (EOF).")
                response = self.quarantine.translate_egress(raw_output)
                if not isinstance(response, dict):
                    raise RuntimeError(f"Malformed worker egress payload: {raw_output!r}")

            if self._is_elicitation(response):
                log.warning(f"[Connector] TRAP: Elicitation detected. Parking {handle_id} (YIELD).")
                await self.fsm.transition(handle_id, IntentState.YIELDED, executable_payload=response)
            else:
                if "error" in response:
                    error_detail = _format_error_detail(response["error"])
                    log.error(
                        f"[Connector:FAULT] Ephemeral Sandbox execution failed: {error_detail}",
                        extra={"handle_id": handle_id},
                    )
                    await self.fsm.transition(handle_id, IntentState.FAULTED, error_detail=error_detail)
                else:
                    log.info(f"[Connector] Intent {handle_id} RESOLVED.")
                    await self.fsm.transition(handle_id, IntentState.RESOLVED, executable_payload=response)

                await self._destroy_sandbox(handle_id)
                self.fsm.cleanup(handle_id)

        except Exception as e:
            raise RuntimeError(f"IO Cycle Failed: {e}") from e

    async def _destroy_sandbox(self, handle_id: str) -> None:
        transport = self.active_sandboxes.pop(handle_id, None)
        if transport:
            await transport.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Fiber MCP Worker Egress Connector")
    parser.add_argument("--target", required=True, help="Target ID")
    parser.add_argument("--exec", required=True, help="Command, Binary root path, or HTTP(S) URL")
    parser.add_argument("--mode", default=None, choices=["ephemeral", "linear", "multiplex"])
    parser.add_argument("--transport", default=None, choices=["stdio", "network"])
    parser.add_argument("--preflight", default=None, help="Regex pattern to trigger YIELD before worker execution")

    args = parser.parse_args()

    exec_target = args.exec.strip()
    is_remote = exec_target.startswith(_REMOTE_SCHEMES)
    is_managed_bin_dir = not is_remote and Path(exec_target).is_dir()

    resolved_transport = args.transport or ("network" if (is_remote or is_managed_bin_dir) else "stdio")
    resolved_mode = args.mode or ("multiplex" if resolved_transport == "network" else "ephemeral")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    connector = MCPServerConnector(
        target_id=args.target,
        execution_target=exec_target,
        mode=resolved_mode,
        transport_type=resolved_transport,
        preflight_pattern=args.preflight,
    )

    try:
        asyncio.run(connector.run())
    except KeyboardInterrupt:
        log.info("[Connector] Exiting gracefully...")
        sys.exit(0)

if __name__ == "__main__":
    main()