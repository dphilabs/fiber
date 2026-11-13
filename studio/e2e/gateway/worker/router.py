# fiber.studio.e2e.gateway.worker.router
"""
@requirements: fiber daemon 
$ XPHI_WORKERS=1 python -m xphi.node.ops.boot
"""
import asyncio
import json
import time
import uuid
from typing import Any, Dict, List

import httpx

from fiber.studio.base.mcp.worker import E2EWorkerManager
from fiber.studio.base.workflow.config import E2EConfig, Phase, TestResult, get_free_port
from fiber.studio.base.workflow.pipeline import BasePipeline, log
from xphi.arch.bound.adapter.gateway import DPoPClientGenerator
from xphi.arch.bound.client.rpc import InternalRpcClient
from xphi.node.ops.daemon.eco.pricing import DynamicPricingDaemon
from xphi.node.space.tunnel.factory import TunnelFactory
from xphi.state.phase.reactor import PhaseReactor

class MCPRouterPipeline(BasePipeline):
    def __init__(self, config: E2EConfig):
        super().__init__(
            config=config,
            name="Core Routing & Performance Suite",
            scope_name="MCP_ROUTER_SUITE",
        )
        self.oracle_id = "worker.price.model"
        self.finlib_id = "worker.eco.finlib"
        self.margin_id = "worker.eco.margin"
        self.search_id = "worker.search.archive"

        self.dpop_client = DPoPClientGenerator(key_size=2048)
        self.set_phases([
            Phase("Phase 1: Event-Driven Zero-Latency Proof", self.phase_zero_latency),
            Phase("Phase 2: High-Throughput Linear Queue Stress (FinLib)", self.phase_finlib_linear_queue),
            Phase("Phase 3: Native Async Multiplexing Concurrency (Model Price)", self.phase_oracle_multiplexing),
            Phase("Phase 4: Unit Economics Vectorization (Margin BI)", self.phase_margin_simulation),
            Phase("Phase 5: Billing Rejection (Free-Rider Defense)", self.phase_billing_rejection),
            Phase("Phase 6: Precise Error Routing (Invalid Params)", self.phase_error_routing),
            Phase("Phase 7: Data-Intensive Async Multiplexing (DuckDB Search)", self.phase_duckdb_search_worker),
            Phase("Phase 8: Autonomic Dynamic Pricing Alignment", self.phase_dynamic_pricing_alignment),
        ])

    async def _invoke_gateway(
        self,
        target_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        req_id: int = 1,
        receipt: str = "valid_receipt",
        timeout: float = 10.0,
    ) -> httpx.Response:
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        nonce = uuid.uuid4().hex
        url = f"{self.local_url}/v1/mcp-gateway/{target_id}/invoke"
        headers = {
            "x-idempotency-key": uuid.uuid4().hex,
            "x-nonce": nonce,
            "X-Fiber-Fuel-Receipt": receipt,
            "DPoP": self.dpop_client.generate_proof(url=url, method="POST", nonce=nonce),
        }
        async with httpx.AsyncClient(base_url=self.local_url, timeout=timeout) as client:
            return await client.post(url, json=payload, headers=headers)

    # Lifecycle Overrides
    async def setup_custom_context(self) -> None:
        class MockPricingCtx:
            pass

        ctx = MockPricingCtx()
        ctx.tunnel = await TunnelFactory.get_default()
        ctx.rpc = InternalRpcClient()

        self.pricing_daemon = DynamicPricingDaemon(ctx=ctx)
        self.pricing_daemon.running = True
        self._pricing_task = asyncio.create_task(self.pricing_daemon.run())
        log.info("[E2E Pipeline] DynamicPricingDaemon successfully injected into test lifecycle.")

    async def teardown_custom(self) -> None:
        if hasattr(self, "pricing_daemon"):
            self.pricing_daemon.running = False
        if hasattr(self, "_pricing_task"):
            self._pricing_task.cancel()
            await asyncio.gather(self._pricing_task, return_exceptions=True)

    async def setup_workers(self) -> None:
        await E2EWorkerManager.mount_workers(
            self,
            [self.oracle_id, self.finlib_id, self.margin_id, self.search_id],
            auto_bootstrap=True,
            tag="E2E Router",
        )

    # Test Phases (1 ~ 8)
    async def phase_zero_latency(self) -> None:
        res = await self._invoke_gateway(self.finlib_id, "eval_math", {"expression": "100 * 50"}, req_id=1)
        if res.status_code != 200:
            raise RuntimeError(f"Expected 200, got {res.status_code}")

    async def phase_finlib_linear_queue(self) -> None:
        req_count = 10
        results = await asyncio.gather(*[
            self._invoke_gateway(
                self.finlib_id,
                "resolve_dates",
                {"base_date": "2026-09-04", "offset_business_days": i},
                req_id=i,
            )
            for i in range(1, req_count + 1)
        ])
        if any(res.status_code != 200 for res in results):
            raise RuntimeError("Linear Queueing failed")

    async def phase_oracle_multiplexing(self) -> None:
        models = ["gpt-4o", "claude", "gemini"]
        start_time = time.time()

        results = await asyncio.gather(*[
            self._invoke_gateway(
                self.oracle_id,
                "lookup_model_cost",
                {"model_name": m},
                req_id=100 + i,
                timeout=15.0,
            )
            for i, m in enumerate(models)
        ])
        elapsed = time.time() - start_time
        log.info(f"  └─ 3 Concurrent Model Price Fetches Completed in {elapsed:.2f}s")

        if any(res.status_code != 200 for res in results):
            raise RuntimeError("Model Price Multiplexing failed. One or more API calls returned error.")

    async def phase_margin_simulation(self) -> None:
        now_ts = int(time.time())
        args = {
            "symbol": "BTC-USDT",
            "observations": {
                "arn:binance": {"rate": 0.0001, "time": now_ts},
                "arn:bybit": {"rate": 0.00012, "time": now_ts},
            },
            "trade_size_usd": 10000.0,
            "pricing": {"base_fee_usd": 0.002, "profit_share_ratio": 0.05},
            "infra": {
                "monthly_fixed_cost_usd": 30.0,
                "compute_cost_per_sec_usd": 0.00001,
                "avg_latency_sec": 0.05,
            },
            "tps_range": [1.0, 10.0, 50.0],
        }
        res = await self._invoke_gateway(self.margin_id, "calculate_trajectory_margin", args, req_id=200)
        if res.status_code != 200:
            raise RuntimeError(f"Margin Sim Failed: Expected 200, got {res.status_code} ({res.text})")

    async def phase_billing_rejection(self) -> None:
        tunnel = await TunnelFactory.get_default()
        tunnel_key = f"config:fuel:{self.finlib_id}"

        await tunnel.set(tunnel_key, "1")
        try:
            res = await self._invoke_gateway(
                self.finlib_id, "eval_math", {"expression": "1 + 1"}, req_id=300, receipt="invalid_receipt"
            )
            if res.status_code != 402:
                raise RuntimeError(f"Expected HTTP 402, got {res.status_code}")
        finally:
            await tunnel.delete(tunnel_key)

    async def phase_error_routing(self) -> None:
        res = await self._invoke_gateway(
            self.finlib_id, "calc_indicators_batch", {"prices_matrix": "BAD_DATA"}, req_id=99
        )
        if res.status_code != 502:
            raise RuntimeError("Expected HTTP 502")

    async def phase_duckdb_search_worker(self) -> None:
        start_time = time.time()
        res = await self._invoke_gateway(
            self.search_id,
            "search_market_evidence",
            {"domain": "Domain_1_Control_Failure", "limit": 2},
            req_id=400,
            timeout=60.0,
        )
        elapsed = time.time() - start_time
        if res.status_code != 200:
            raise RuntimeError(f"Search Worker Failed: Expected 200, got {res.status_code}")

        data = res.json()
        if "error" in data:
            raise RuntimeError(f"Search Worker returned JSON-RPC error: {data['error']}")

        try:
            result_content = data.get("result", {}).get("content", [])[0].get("text", "{}")
            telemetry = json.loads(result_content).get("telemetry", {})
            if telemetry:
                log.info(f"  └─ Total E2E Latency: {elapsed:.2f}s")
                log.info(f"  ├─ [DuckDB] Processed {telemetry.get('scanned_file_mb', 0)} MB")
                log.info(f"  ├─ [DuckDB] Extracted {telemetry.get('total_events_parsed', 0)} events")
                log.info(f"  ├─ [DuckDB] Pure SQL Time: {telemetry.get('duckdb_sql_time_sec', 0)}s")
                log.info(f"  └─ [DuckDB] Python Time: {telemetry.get('python_regex_time_sec', 0)}s")
        except Exception as e:
            raise RuntimeError(f"Search response parsing failed: {e}")

    async def phase_dynamic_pricing_alignment(self) -> None:
        tunnel = await TunnelFactory.get_default()
        tunnel_key = f"config:fuel:{self.search_id}"

        await tunnel.set(tunnel_key, "1")
        try:
            await asyncio.sleep(1.0)
            price_tag_key = f"eco:price_tag:{self.search_id}"
            dynamic_price_str = await tunnel.get(price_tag_key)

            if not dynamic_price_str:
                raise RuntimeError(f"Pricing Daemon failed to write to Redis key: {price_tag_key}")

            dynamic_price = float(dynamic_price_str)
            genesis_floor = 0.002

            log.info(f"  ├─ Genesis Floor Price: ${genesis_floor:.4f}")
            log.info(f"  ├─ New Dynamic Price  : ${dynamic_price:.4f}")

            if dynamic_price <= genesis_floor:
                raise RuntimeError(
                    f"Dynamic price (${dynamic_price}) did not increase from Genesis floor despite compute usage."
                )

            res = await self._invoke_gateway(
                self.search_id,
                "search_market_evidence",
                {"domain": "Domain_2_Security_Audit", "limit": 1},
                req_id=500,
                receipt="invalid_receipt",
            )
            if res.status_code != 402:
                raise RuntimeError(f"Expected 402 Payment Required, got {res.status_code}")

            error_msg = res.json().get("detail", "")
            expected_fee_string = f"${dynamic_price:.4f}"
            if expected_fee_string not in error_msg:
                raise RuntimeError(f"Rejection message does not contain dynamic price! Msg: {error_msg}")

            log.info(
                f"  └─ Gateway precisely rejected underfunded request based on Dynamic Price ({expected_fee_string})."
            )
        finally:
            await tunnel.delete(tunnel_key)


class CoreSuiteRunner:
    def __init__(self):
        self.log = log
        self.results: List[TestResult] = []

    async def execute(self) -> None:
        net_config = E2EConfig(host="127.0.0.1", port=get_free_port(8355), protocol="http")
        self.results.extend(await MCPRouterPipeline(config=net_config).run_pipeline())
        self._print_report()

    def _print_report(self) -> None:
        all_passed = all(r.passed for r in self.results)
        report_buffer = [
            "\n" + "=" * 80,
            "[CORE ROUTING BENCHMARK REPORT]",
            "=" * 80,
        ]

        for idx, res in enumerate(self.results, 1):
            status_icon = "[O]" if res.passed else "[X]"
            line = (
                f"{status_icon} {idx:02d}. [{res.target}]".ljust(28)
                + f"{res.scenario.ljust(50)} | {'PASSED' if res.passed else 'FAILED'}"
            )
            report_buffer.append(line)

        report_buffer.append("-" * 80)
        report_buffer.append(
            "CORE ROUTING PIPELINE VALIDATED SUCCESSFULLY."
            if all_passed
            else "CORE ROUTING FAILURE DETECTED. Check logs for details."
        )
        report_buffer.append("=" * 80 + "\n")

        aggregated_report = "\n".join(report_buffer)
        if all_passed:
            self.log.info(aggregated_report)
        else:
            self.log.critical(aggregated_report)


def main(args_list: list[str] = None) -> None:
    app = CoreSuiteRunner()
    PhaseReactor.ignite(main_coro_func=app.execute)


if __name__ == "__main__":
    main()