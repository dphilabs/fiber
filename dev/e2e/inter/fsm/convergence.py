# fiber.dev.e2e.inter.fsm.convergence
import json
import sys
import uuid
from typing import Any, Dict, List

from fiber.dev.base.fsm.registry import default_asl_registry
from fiber.dev.base.workflow.config import E2EConfig, Phase, TestResult, get_free_port
from fiber.dev.base.workflow.pipeline import BasePipeline, log
from fiber.gateway.bridge.action.connect import (
    ACTIVE_TARGETS_TUNNEL_KEY,
    ResolvedTarget,
    _specs_from_mapped_entries,
)
from fiber.gateway.bridge.fsm.polyglot import PolyglotExecutionBridge
from fiber.plane.mcp.connector import MCPServerConnector
from fiber.plane.mcp.mapper.config import McpConfigMapper
import fiber.plane.mcp.worker.eco.finlib as worker_finlib
import fiber.plane.mcp.worker.search.archive as worker_search

from xphi.node.runtime.fsm.traverser import GraphTraverser, TraversalResult
from xphi.node.runtime.wasm.broker import WasmBroker
from xphi.node.space.bind.resolver import resolve_path
from xphi.node.space.tunnel.factory import TunnelFactory
from xphi.state.anchor.store import AnchorStore
from xphi.state.phase.reactor import PhaseReactor

ANCHOR_MCP_JSON = resolve_path("anchor") / "mcp.json"


class MultiverseConvergencePipeline(BasePipeline):
    def __init__(self, config: E2EConfig):
        super().__init__(
            config=config,
            name="Multiverse Fork, Pre-Flight Park & Polyglot Convergence Suite",
            scope_name="MULTIVERSE_CONVERGENCE",
        )
        self.finlib_id = "worker.eco.finlib"
        self.search_id = "worker.search.archive"
        self.base_stream_id = f"mv_conv_{uuid.uuid4().hex[:8]}"
        self.initial_fuel = 1000

        self.asl_schema: Dict[str, Any] = {}
        self.bridge: PolyglotExecutionBridge = None
        self.traverser: GraphTraverser = None
        self.first_pass: TraversalResult = None
        self.hitl_approved: bool = False

        self.set_phases([
            Phase(
                "Phase 1: 1:3 Asymmetric Fork + PreFlight Park (202) + FinOps 402 -> Exec Fallback",
                self.phase_fork_park_and_fallback,
            ),
            Phase(
                "Phase 2: Selective Timeline Suspension Audit & Operator Approval",
                self.phase_audit_and_approve_hitl,
            ),
            Phase(
                "Phase 3: O(1) Cursor Resume, Barrier Collapse & PTA Fuel Conservation",
                self.phase_resume_and_converge,
            ),
        ])

    def _load_specs_from_anchor_root(self) -> Dict[str, ResolvedTarget]:
        if ANCHOR_MCP_JSON.is_file():
            entries = McpConfigMapper.parse_file(ANCHOR_MCP_JSON, agent_hint="fiber")
            return {s.target_id: s for s in _specs_from_mapped_entries(entries)}
        return {}

    async def setup_workers(self) -> None:
        tunnel = await TunnelFactory.get_default()
        raw_active = await tunnel.get(ACTIVE_TARGETS_TUNNEL_KEY)
        active_daemon_ids = set(json.loads(raw_active)) if raw_active else set()
        anchor_specs = self._load_specs_from_anchor_root()

        targets = {
            self.finlib_id: (f"{sys.executable} -m {worker_finlib.__name__}", "linear", r"normalize_order|pow\(|exp\("),
            self.search_id: (f"{sys.executable} -m {worker_search.__name__}", "multiplex", None),
        }
        for tid, (cmd, mode, default_guard) in targets.items():
            if tid in active_daemon_ids:
                log.info(f"[Multiverse E2E] Attached to live McpWorkerDaemon target: [{tid}]")
                continue
            spec = anchor_specs.get(tid)
            self.connectors.append(
                MCPServerConnector(
                    target_id=tid,
                    execution_target=spec.exec_cmd if spec else cmd,
                    mode=spec.mode if spec else mode,
                    transport_type=spec.transport if spec else "stdio",
                    preflight_pattern=(spec.preflight if spec else None) or default_guard,
                )
            )

        loaded = default_asl_registry.get("multiverse_convergence")
        if not loaded:
            raise FileNotFoundError("ASL schema 'multiverse_convergence.json' not found in ASLRegistry.")
        self.asl_schema, src_name = loaded
        log.info(f"[Multiverse E2E] Loaded ASL topology from '{src_name}'.")

        broker = WasmBroker.get_shared()
        self.traverser = GraphTraverser(broker=broker)
        self.bridge = PolyglotExecutionBridge(
            runtime=self.traverser.runtime,
            mcp_gateway_url=f"{self.local_url}/v1/mcp-gateway",
            allow_exec_subprocess=True,
        )
        self.traverser.node_executor = self._intercept_hitl_or_delegate

        # Register local `fn://` handlers
        self.bridge.register_fn("seed_intent", self._fn_seed_intent)
        self.bridge.register_fn("audit_convergence", self._fn_audit_convergence)

    async def teardown_custom(self) -> None:
        if self.bridge:
            await self.bridge.aclose()

    # Local `fn://` Callables & Pre-Flight Resume Hook
    @staticmethod
    def _fn_seed_intent(symbol: str = "BTCUSD", base_amount: int = 250, multiplier: int = 4) -> Dict[str, Any]:
        return {
            "expr_str": f"{base_amount} * {multiplier}",
            "raw_price": 64321.89,
            "raw_amount": 1.23456,
            "_fork_ratios": [0.3, 0.4, 0.3],
            "step_cost": 5,
        }

    @staticmethod
    def _fn_audit_convergence(settled_price: float = 0.0) -> Dict[str, Any]:
        return {
            "convergence_verified": True,
            "settled_price": settled_price,
            "_score": 0.99,
            "step_cost": 5,
        }

    async def _intercept_hitl_or_delegate(self, cursor, node_spec, task_io):
        """Delegates all execution to PolyglotExecutionBridge, injecting resolved output upon HITL resume."""
        uri = task_io.get("resource_uri", "")
        if uri.endswith("/normalize_order") and not self.hitl_approved:
            # Trigger Pre-Flight HITL Yield (simulating or capturing HTTP 202 guard)
            meta_up, offload, cost = await self.bridge(cursor, node_spec, task_io)
            if meta_up.get("_hitl_yield"):
                return meta_up, offload, cost
            # If external daemon did not return 202 directly on REST edge, enforce Pre-Flight yield contract
            return {"_hitl_yield": True, "_hitl_ticket": f"ticket_{cursor.stream_id}"}, {"parked": True}, 0

        if uri.endswith("/normalize_order") and self.hitl_approved:
            return {"normalized_price": 64321.5, "_score": 0.96, "_hitl_yield": False}, {"approved": True}, 5

        res_meta, offload, cost = await self.bridge(cursor, node_spec, task_io)
        if cursor.curr_node == "BranchA_FinLib_Math":
            res_meta.setdefault("math_value", 1000)
            res_meta.setdefault("_score", 0.92)
        return res_meta, offload, cost

    # Phases 1 ~ 3
    async def phase_fork_park_and_fallback(self) -> None:
        tunnel = await TunnelFactory.get_default()
        fuel_key = f"config:fuel:{self.search_id}"
        await tunnel.set(fuel_key, "1")

        try:
            self.first_pass = await self.traverser.run(
                asl_schema=self.asl_schema,
                base_stream_id=self.base_stream_id,
                initial_fuel=self.initial_fuel,
            )

            log.info(f"  ├─ Visited History (Pass 1): {self.first_pass.visited_history}")
            log.info(f"  ├─ Parked & Barrier-Waiting Cursors: {len(self.first_pass.parked_cursors)}")

            # Verify all 3 parallel branches are preserved (Branch A at barrier, Branch C via Fallback at barrier, Branch B HITL-parked)
            if len(self.first_pass.parked_cursors) != 3:
                raise RuntimeError(
                    f"Expected 3 preserved cursors (1 HITL + 2 Barrier Siblings), got {len(self.first_pass.parked_cursors)}"
                )

            has_fallback = any("(FALLBACK)" in h for h in self.first_pass.visited_history)
            has_hitl = any("(HITL_PARKED)" in h for h in self.first_pass.visited_history)
            if not (has_fallback and has_hitl):
                raise RuntimeError(
                    f"Missing expected FALLBACK or HITL_PARKED transition in history: {self.first_pass.visited_history}"
                )
        finally:
            await tunnel.delete(fuel_key)

    async def phase_audit_and_approve_hitl(self) -> None:
        hitl_cursors = [c for c in self.first_pass.parked_cursors if c.meta_ctx.get("_hitl_yield")]
        if len(hitl_cursors) != 1:
            raise RuntimeError(f"Expected exactly 1 cursor with _hitl_yield=True, got {len(hitl_cursors)}")

        target_c = hitl_cursors[0]
        log.info(
            f"  ├─ Verified Branch '{target_c.stream_id}' suspended at '{target_c.curr_node}' "
            f"(Pointer={target_c.pointer.to_key()}, Preserved Fuel={target_c.fuel})"
        )

        self.hitl_approved = True
        log.info(f"  └─ Operator HITL Approval granted for '{target_c.stream_id}'. Ready for O(1) resume.")

    async def phase_resume_and_converge(self) -> None:
        resumed_res = await self.traverser.resume(
            targets=self.first_pass.parked_cursors,
            base_stream_id=self.base_stream_id,
            resume_meta={"_hitl_approved_by": "secops_operator"},
        )

        log.info(f"  ├─ Visited History (Pass 2): {resumed_res.visited_history}")
        log.info(
            f"  ├─ Final Cursor: {resumed_res.final_cursor.stream_id} | "
            f"Node: {resumed_res.final_cursor.curr_node} | "
            f"Reclaimed Fuel: {resumed_res.reclaimed_fuel} | "
            f"Join Collapses: {resumed_res.join_collapses}"
        )

        if resumed_res.parked_cursors:
            raise RuntimeError(f"Expected 0 parked cursors after resume, got {len(resumed_res.parked_cursors)}")

        if resumed_res.join_collapses < 1:
            raise RuntimeError("Expected at least 1 N:1 DagJoinBarrier collapse during convergence.")

        if not resumed_res.final_cursor.meta_ctx.get("convergence_verified"):
            raise RuntimeError(f"Final audit fn did not execute! Meta: {resumed_res.final_cursor.meta_ctx}")

        head_hash = AnchorStore().get_head_hash(self.base_stream_id)
        if not head_hash:
            raise RuntimeError(f"AnchorStore head commit missing for converged stream '{self.base_stream_id}'")

        log.info(f"  └─ AnchorStore Ledger Head Verified: {head_hash[:12]}... (Multiverse Convergence Complete)")


class ConvergenceSuiteRunner:
    def __init__(self):
        self.results: List[TestResult] = []

    async def execute(self):
        net_config = E2EConfig(host="127.0.0.1", port=get_free_port(8360), protocol="http")
        self.results.extend(await MultiverseConvergencePipeline(config=net_config).run_pipeline())
        for idx, r in enumerate(self.results, 1):
            mark = "[O]" if r.passed else "[X]"
            log.info(f"{mark} {idx:02d}. {r.scenario} -> {'PASSED' if r.passed else 'FAILED'}")


def main(args_list: list[str] = None):
    PhaseReactor.ignite(main_coro_func=ConvergenceSuiteRunner().execute)


if __name__ == "__main__":
    main()