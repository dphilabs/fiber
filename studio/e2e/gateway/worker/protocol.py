# fiber.studio.e2e.gateway.worker.protocol
"""
Unified E2E test suite for MCP Transport Protocols & Stateful Resilience:
  - HTTP Multiplexing & Mcp-Session-Id persistence
  - Streamable HTTP (SSE text/event-stream) framing
  - Upstream Auth (401) & JSON-RPC Fault -> Gateway 502 error translation
  - Native Worker Elicitation (202 YIELD), Idempotency Fast-Path & In-Band Stateless TOTP Resume
  - Sentinel (WorkerReaper) autonomous orphan stream rollback
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any, List, Optional

import httpx

from fiber.studio.base.mcp.payload import McpPayloadBuilder
from fiber.studio.base.mcp.server import MockMcpServer
from fiber.studio.base.mcp.worker import E2EWorkerManager
from fiber.studio.base.workflow.audit.context import AuditContext, MCPGatewayTestClient
from fiber.studio.base.workflow.config import E2EConfig, Phase, TestResult, get_free_port
from fiber.studio.base.workflow.pipeline import MockPipeline, log
from xphi.arch.contract.config.env import DEFAULT_GATEWAY_PORT
from xphi.state.phase.reactor import PhaseReactor


class MCPProtocolPipeline(MockPipeline):
    def __init__(self, config: E2EConfig):
        super().__init__(
            config=config,
            name="Unified MCP Transport Protocols & Stateful Resilience Suite",
            scope_name="MCP_PROTOCOL_SUITE",
        )
        self.mcp = McpPayloadBuilder()
        self.crypto_ctx = AuditContext(spiffe_id="spiffe://self/fiber")
        self.gw_client: Optional[MCPGatewayTestClient] = None

        # 외부 데몬의 활성 타겟과 Redis Pub/Sub 큐가 충돌하지 않도록 실행 스코프 격리
        self.http_target_id = self.crypto_ctx.scoped_target("net-sqlite-fs")
        self.sse_target_id = self.crypto_ctx.scoped_target("net-sse-stream")
        self.remote_ctx7_id = self.crypto_ctx.scoped_target("net-context7-auth")
        self.deploy_target_id = self.crypto_ctx.scoped_target("legacy-deployer")

        self.env_key_name = "CONTEXT7_API_KEY"
        user_key = os.environ.get(self.env_key_name, "").strip()
        self._injected_temp_env = not bool(user_key)
        self.active_api_key = user_key or f"ctx7_live_e2e_{uuid.uuid4().hex[:16]}"
        os.environ[self.env_key_name] = self.active_api_key

        self.mock_mcp = MockMcpServer(
            host="127.0.0.1", port=get_free_port(8359), expected_api_key=self.active_api_key
        )
        self.active_context7_url = os.environ.get("CONTEXT7_MCP_URL", "").strip() or self.mock_mcp.context7_url

        self.set_phases([
            Phase("Phase 1: HTTP Multiplex Concurrency & Mcp-Session-Id Persistence", self.phase_http_session_and_multiplex),
            Phase("Phase 2: Streamable HTTP (SSE text/event-stream) Frame Resolution", self.phase_sse_stream_transport),
            Phase("Phase 3: Upstream Auth (401) & RPC Fault -> Gateway 502 Translation", self.phase_error_and_auth_translation),
            Phase("Phase 4: Native Elicitation (202), Idempotency Shield & In-Band TOTP Resume", self.phase_inband_elicitation_and_resume),
            Phase("Phase 5: Sentinel (WorkerReaper) Autonomous Orphan Stream Rollback", self.phase_sentinel_reconciliation),
        ])

    async def setup_custom_context(self) -> None:
        await self.mock_mcp.start()
        validator = self.crypto_ctx.provision()
        self.gw_client = self.crypto_ctx.create_gateway_client(base_url=self.local_url, default_timeout=20.0)
        log.info(
            f"[ProtocolPipeline] MockMcpServer ({self.mock_mcp.base_url}) & "
            f"Validator (PubKey: {validator.pubkey_hex[:12]}...) Aligned."
        )

    async def setup_workers(self) -> None:
        E2EWorkerManager.mount_custom_targets(
            self,
            {
                self.http_target_id: self.mock_mcp.mcp_url,
                self.sse_target_id: self.mock_mcp.sse_url,
                self.remote_ctx7_id: self.active_context7_url,
                self.deploy_target_id: None,
            },
            tag="ProtocolPipeline",
        )
        E2EWorkerManager.start_sentinel(self, sweep_interval=1.0)
        log.info("[ProtocolPipeline] 4 Scoped Connectors & Sentinel Reaper Daemon Ignited.")

    async def teardown_custom(self) -> None:
        await E2EWorkerManager.stop_sentinel(self)

        if self._injected_temp_env:
            os.environ.pop(self.env_key_name, None)

        await self.crypto_ctx.aclose_clients()
        self.crypto_ctx.teardown()
        await self.mock_mcp.stop()

    async def _call_tool(self, target_id: str, tool_name: str, timeout: float = 15.0, **arguments: Any) -> httpx.Response:
        payload = self.mcp.next_tool_call(tool_name, **arguments)
        return await self.gw_client.post_gateway(target_id, payload, timeout=timeout)

    @staticmethod
    def _extract_data(res: httpx.Response, parse_json: bool = True) -> Any:
        if res.status_code != 200:
            raise RuntimeError(f"Gateway HTTP {res.status_code}: {res.text}")
        return McpPayloadBuilder.extract_data(res.content, parse_json=parse_json)

    # =========================================================================
    # Phase 1: HTTP Multiplexing, SQLite Execution & Mcp-Session-Id Persistence
    # =========================================================================
    async def phase_http_session_and_multiplex(self) -> None:
        init_res = await self.gw_client.post_gateway(self.http_target_id, self.mcp.next_initialize())
        conn = self._get_connector(self.http_target_id)
        session_id = getattr(conn.shared_transport, "_mcp_session_id", None) if conn else None
        if init_res.status_code != 200 or not conn or conn.transport_type != "network" or conn.mode != "multiplex" or not session_id:
            raise RuntimeError("Failed to resolve network/multiplex or persist Mcp-Session-Id.")

        sql_res = await self._call_tool(
            self.http_target_id,
            "sqlite_query",
            query="SELECT tool_name, latency_ms FROM mcp_audit_logs WHERE status = 'OK' ORDER BY latency_ms ASC",
        )
        sql_data = self._extract_data(sql_res)
        if sql_data.get("row_count") != 4:
            raise RuntimeError(f"Expected 4 SQLite rows, got {sql_data.get('row_count')}")

        t0 = time.perf_counter()
        burst = await asyncio.gather(
            *[self._call_tool(self.http_target_id, "filesystem_inspect", path=__file__) for _ in range(8)]
        )
        if any(r.status_code != 200 for r in burst):
            raise RuntimeError("Concurrent 8-way HTTP multiplexed file inspection failed.")

        sample = self._extract_data(burst[0])
        log.info(
            f"  └─ Session '{session_id}' Persisted | SQLite 4 Rows OK | "
            f"8-Way Multiplex SHA-256 ({sample['sha256'][:10]}...) in {time.perf_counter() - t0:.2f}s"
        )

    # =========================================================================
    # Phase 2: Streamable HTTP (SSE text/event-stream) Parsing
    # =========================================================================
    async def phase_sse_stream_transport(self) -> None:
        res = await self._call_tool(
            self.sse_target_id, "sqlite_query", query="SELECT COUNT(*) AS total FROM mcp_audit_logs"
        )
        total = self._extract_data(res)["rows"][0]["total"]
        if total != 4:
            raise RuntimeError(f"Expected count 4 over SSE stream, got {total}")
        log.info(f"  └─ SSE (text/event-stream) Frame Parsed & Resolved Successfully (total={total})")

    # =========================================================================
    # Phase 3: Upstream 401 Auth Tamper & RPC Error -> Gateway 502 Translation
    # =========================================================================
    async def phase_error_and_auth_translation(self) -> None:
        # 1) SQLite read-only violation (-32602 JSON-RPC error) -> HTTP 502
        err_res = await self._call_tool(self.http_target_id, "sqlite_query", query="DROP TABLE mcp_audit_logs")
        if err_res.status_code != 502:
            raise RuntimeError(f"Expected HTTP 502 on invalid SQL mutation, got {err_res.status_code}")

        # 2) Compromised remote API key (HTTP 401 Unauthorized) -> HTTP 502
        original_key = os.environ.get(self.env_key_name, "")
        os.environ[self.env_key_name] = "ctx7_invalid_compromised_token_000"
        try:
            auth_res = await self._call_tool(
                self.remote_ctx7_id, "query-docs", context7CompatibleLibraryID="/vercel/next.js"
            )
            if auth_res.status_code != 502:
                raise RuntimeError(f"Expected HTTP 502 on upstream 401 Unauthorized, got {auth_res.status_code}")
        finally:
            os.environ[self.env_key_name] = original_key

        log.info("  └─ Upstream HTTP 401 & JSON-RPC Faults accurately translated to Gateway HTTP 502.")

    # =========================================================================
    # Phase 4: Native Elicitation (202), Idempotency Fast-Path & In-Band Resume
    # =========================================================================
    async def phase_inband_elicitation_and_resume(self) -> None:
        idem_key = uuid.uuid4().hex
        deploy_payload = self.gw_client.build_migration_payload(
            req_id=777, service_name="auth", sql_script="DROP TABLE"
        )

        # 1) 1차 호출: 워커 내부 네이티브 Elicitation (HTTP 202 YIELD) 유도
        res1 = await self.gw_client.post_gateway(self.deploy_target_id, deploy_payload, idem_key=idem_key)
        if res1.status_code != 202:
            raise RuntimeError(f"Expected HTTP 202 (YIELD), got {res1.status_code}: {res1.text}")

        prompt_id = res1.json().get("id")
        if not prompt_id:
            raise RuntimeError(f"Elicitation response missing prompt 'id': {res1.text}")

        # 2) 동일 Idempotency-Key 재전송 시 워커 중복 스폰 없이 Fast-Path 202 즉시 반환 검증
        res2 = await self.gw_client.post_gateway(self.deploy_target_id, deploy_payload, idem_key=idem_key)
        if res2.status_code != 202:
            raise RuntimeError(f"Idempotency Fast-Path failed. Expected 202, got {res2.status_code}")

        # 3) In-Band `_meta.inputResponses` (AttestedResumeResult 규격) TOTP 주입 및 재개 검증
        resume_payload = self.gw_client.attach_totp_resume(
            base_payload=deploy_payload,
            prompt_id=prompt_id,
            totp_code=self.crypto_ctx.generate_totp(),
        )
        res_final = await self.gw_client.post_gateway(
            self.deploy_target_id, resume_payload, idem_key=idem_key, timeout=10.0
        )
        if res_final.status_code != 200:
            raise RuntimeError(f"In-Band Resume failed. Expected 200 OK, got {res_final.status_code}: {res_final.text}")

        final_text = self._extract_data(res_final, parse_json=False)
        if "successfully" not in final_text:
            raise RuntimeError(f"Resume succeeded, but migration output unexpected: {final_text}")

        log.info("  └─ Native 202 YIELD -> Idempotency Fast-Path -> In-Band TOTP Resume (200 OK) Verified.")

    # =========================================================================
    # Phase 5: Sentinel (WorkerReaper) Autonomous Orphan Stream Reconciliation
    # =========================================================================
    async def phase_sentinel_reconciliation(self) -> None:
        idem_key = uuid.uuid4().hex
        payload = self.gw_client.build_migration_payload(
            req_id=888, service_name="billing", sql_script="DROP TABLE"
        )

        res = await self.gw_client.post_gateway(self.deploy_target_id, payload, idem_key=idem_key)
        if res.status_code != 202:
            raise RuntimeError(f"Expected 202 on sentinel trigger, got {res.status_code}")

        handle_id = self.gw_client.extract_handle_id(res, fallback=self.captured_handle_ids.get("latest"))
        await E2EWorkerManager.wait_for_sentinel_rollback(self, handle_id, stale_timeout=1.0)
        log.info(f"  └─ Sentinel Autonomous Reconciliation Verified (Orphan {handle_id} -> FAULTED).")


async def run_suite() -> None:
    pipeline = MCPProtocolPipeline(
        E2EConfig(host="127.0.0.1", port=get_free_port(DEFAULT_GATEWAY_PORT), protocol="http")
    )
    results: List[TestResult] = await pipeline.run_pipeline()
    all_passed = all(r.passed for r in results)

    lines = [
        "\n" + "=" * 90,
        "[UNIFIED MCP TRANSPORT PROTOCOLS & STATEFUL RESILIENCE E2E REPORT]",
        "=" * 90,
    ]
    for idx, r in enumerate(results, 1):
        lines.append(
            f"{'[O]' if r.passed else '[X]'} {idx:02d}. [{r.target}]".ljust(26)
            + f"{r.scenario.ljust(62)} | {'PASSED' if r.passed else 'FAILED'}"
        )
    lines.extend([
        "-" * 90,
        "MCP PROTOCOL & RESILIENCE PIPELINE VALIDATED SUCCESSFULLY." if all_passed else "MCP PROTOCOL PIPELINE FAILURE DETECTED.",
        "=" * 90 + "\n",
    ])
    print("\n".join(lines), flush=True)

def main(args_list: Optional[List[str]] = None) -> None:
    PhaseReactor.ignite(main_coro_func=run_suite)

if __name__ == "__main__":
    main()