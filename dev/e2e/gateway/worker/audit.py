# fiber.dev.e2e.gateway.worker.audit
from __future__ import annotations

import asyncio
import sys
import uuid
from typing import List, Optional

from fiber.dev.base.workflow.audit.context import AuditContext, MCPGatewayTestClient
from fiber.dev.base.workflow.config import E2EConfig, Phase, TestResult, get_free_port
from fiber.dev.base.workflow.pipeline import MockPipeline, log
from fiber.plane.mcp.connector import MCPServerConnector
import fiber.plane.mcp.worker.deployer as worker_deployer
import fiber.plane.mcp.worker.reaper as worker_reaper
from xphi.arch.contract.config.env import DEFAULT_GATEWAY_PORT
from xphi.state.phase.reactor import PhaseReactor


class AuditSecurityPipeline(MockPipeline):
    def __init__(self, config: E2EConfig):
        super().__init__(
            config=config,
            name="Stateful Security & Resilience Suite",
            scope_name="MCP_AUDIT_SUITE",
        )

        self.crypto_ctx = AuditContext(spiffe_id="spiffe://self/fiber")
        # 외부 데몬에 떠 있을 수 있는 legacy-01 워커와 Redis 큐 충돌 방지
        self.deploy_id = self.crypto_ctx.scoped_target("legacy-01")
        self.gw_client: Optional[MCPGatewayTestClient] = None

        # Phase 간 공유 상태
        self.prompt_id: Optional[str] = None
        self.idem_key_otp: Optional[str] = None
        self.deploy_payload: Optional[dict] = None

        self.sentinel: Optional[worker_reaper.WorkerReaper] = None
        self._sentinel_task: Optional[asyncio.Task] = None

        self.set_phases([
            Phase("Phase 1: Idempotency Fast-Path Defense (Trigger YIELD)", self.phase_idempotency_defense),
            Phase("Phase 2: MCP 2026-07-28 Stateless Re-issue & Resume", self.phase_stateless_otp_resume),
            Phase("Phase 3: Autonomous Reconciliation (Sentinel)", self.phase_sentinel_reconciliation),
        ])

    async def setup_custom_context(self) -> None:
        print("\n" + "=" * 80)
        print("[Security Context] Zero-Trust Auth Validator Auto-Provisioning")
        validator = self.crypto_ctx.provision()
        print(f"Bound NodeSigner PubKey : {validator.pubkey_hex[:16]}...")
        print(f"Injected Scoped Secret  : {self.crypto_ctx.temp_db_path}")
        print("=" * 80 + "\n")

        self.gw_client = self.crypto_ctx.create_gateway_client(
            base_url=self.local_url,
            target_id=self.deploy_id,
        )
        log.info("[AuditPipeline] Zero-Exposure Audit DB & Validator Context Aligned.")

    async def setup_workers(self) -> None:
        deploy_cmd = f"{sys.executable} -m {worker_deployer.__name__}"
        self.connectors.append(
            MCPServerConnector(target_id=self.deploy_id, execution_target=deploy_cmd, mode="ephemeral")
        )

        self.sentinel = worker_reaper.WorkerReaper(
            anchor_store=self.mock_store, rpc_client=self.rpc, sweep_interval=1.0
        )
        self._sentinel_task = asyncio.create_task(self.sentinel.ignite())
        log.info("[AuditPipeline] Sentinel Autonomous Daemon & Worker Connectors Ignited.")

    async def teardown_custom(self) -> None:
        if self.sentinel:
            self.sentinel.running = False
        if self._sentinel_task and not self._sentinel_task.done():
            self._sentinel_task.cancel()
            await asyncio.gather(self._sentinel_task, return_exceptions=True)

        await self.crypto_ctx.aclose_clients()
        self.crypto_ctx.teardown()
        log.info(
            f"[AuditPipeline] Sentinel Daemon Shutdown & Scoped Record ({self.crypto_ctx.spiffe_id}) Purged."
        )

    # Test Phases
    async def phase_idempotency_defense(self) -> None:
        self.idem_key_otp = uuid.uuid4().hex
        self.deploy_payload = self.gw_client.build_migration_payload(
            req_id=777, service_name="auth", sql_script="DROP TABLE"
        )

        # 1차 호출: 파괴적 쿼리 감지 -> Elicitation (HTTP 202 YIELD) 유도
        res1 = await self.gw_client.invoke(self.deploy_payload, idempotency_key=self.idem_key_otp)
        if res1.status_code != 202:
            raise RuntimeError(f"Expected HTTP 202 (YIELD), got {res1.status_code} - {res1.text}")

        prompt_data = res1.json()
        self.prompt_id = prompt_data.get("id")
        if not self.prompt_id:
            raise RuntimeError(f"Elicitation response missing prompt 'id': {prompt_data}")

        res2 = await self.gw_client.invoke(self.deploy_payload, idempotency_key=self.idem_key_otp)
        if res2.status_code != 202:
            raise RuntimeError(f"Fast-Path failed. Expected 202, got {res2.status_code}")

        log.info("   └─ Idempotency Shield deflected duplicate request without crashing Sandbox.")

    async def phase_stateless_otp_resume(self) -> None:
        valid_totp = self.crypto_ctx.generate_totp()
        resume_payload = self.gw_client.attach_totp_resume(
            base_payload=self.deploy_payload,
            prompt_id=self.prompt_id,
            totp_code=valid_totp,
        )

        res = await self.gw_client.invoke(resume_payload, idempotency_key=self.idem_key_otp, timeout=10.0)
        if res.status_code != 200:
            raise RuntimeError(f"Bridge failed to Resume Sandbox. Expected 200 OK, got {res.status_code} ({res.text})")

        body = res.json()
        content_list = body.get("result", {}).get("content", [])
        final_text = content_list[0].get("text", "") if content_list else ""
        if "successfully" not in final_text:
            raise RuntimeError(f"Resume succeeded, but payload failed: {body}")

        log.info("   └─ Stateless Resume -> Stateful Sentinel Execution -> 200 OK Resolution Verified.")

    async def phase_sentinel_reconciliation(self) -> None:
        # 고아 트랜잭션(Orphaned Stream) 빠른 감지를 위해 TTL 임계치를 1.0초로 단축
        self.mock_store.stale_timeout = 1.0
        if self.sentinel:
            self.sentinel.ttl_thresholds["transition.pending"] = 1.0

        idem_key_sentinel = uuid.uuid4().hex
        payload = self.gw_client.build_migration_payload(
            req_id=888, service_name="billing", sql_script="DROP TABLE"
        )

        # 고의적으로 YIELD(202)를 유발한 뒤 TOTP를 입력하지 않고 방치
        res = await self.gw_client.invoke(payload, idempotency_key=idem_key_sentinel)
        if res.status_code != 202:
            raise RuntimeError(f"Expected 202 on initial sentinel trigger, got {res.status_code} ({res.text})")

        handle_id = self.gw_client.extract_handle_id(res, fallback=self.captured_handle_ids.get("latest"))

        # Sentinel이 백그라운드에서 고아 상태를 감지하고 FAULTED로 강제 롤백할 때까지 최대 10초간 폴링
        final_state = None
        for _ in range(20):
            final_state = await self.mock_store.query_state(handle_id)
            if final_state and final_state.metadata.get("status") == "FAULTED":
                log.info("   └─ Sentinel Autonomous Reconciliation Successful.")
                return
            await asyncio.sleep(0.5)

        current_status = final_state.metadata.get("status") if final_state else "None"
        raise RuntimeError(f"Sentinel failed to rollback within 10.0s. Current status: {current_status}")


class AuditSuiteRunner:
    def __init__(self):
        self.log = log
        self.results: List[TestResult] = []

    async def execute(self) -> None:
        net_config = E2EConfig(host="127.0.0.1", port=get_free_port(DEFAULT_GATEWAY_PORT), protocol="http")
        self.results.extend(await AuditSecurityPipeline(config=net_config).run_pipeline())
        self._print_report()

    def _print_report(self) -> None:
        self.log.info("\n" + "=" * 80)
        self.log.info("[SECURITY & AUDIT BENCHMARK REPORT]")
        self.log.info("=" * 80)
        all_passed = all(r.passed for r in self.results)

        for idx, res in enumerate(self.results, 1):
            status_icon = "[O]" if res.passed else "[X]"
            self.log.info(
                f"{status_icon} {idx:02d}. [{res.target}]".ljust(22)
                + f"{res.scenario.ljust(50)} | {'PASSED' if res.passed else 'FAILED'}"
            )

        self.log.info("-" * 80)
        if all_passed:
            self.log.info("SECURITY AUDIT PIPELINE VALIDATED SUCCESSFULLY.")
        else:
            self.log.critical("SECURITY FRACTURE DETECTED. Check logs for details.")
        self.log.info("=" * 80 + "\n")

def main(args_list: Optional[List[str]] = None) -> None:
    app = AuditSuiteRunner()
    PhaseReactor.ignite(main_coro_func=app.execute)

if __name__ == "__main__":
    main()