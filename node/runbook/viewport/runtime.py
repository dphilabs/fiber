# fiber.node.runbook.viewport.runtime
from __future__ import annotations

from dataclasses import replace
import time
from typing import Any

from xphi.arch.contract.registry.flow import FlowDSL, FlowRegistry, load_sample_registry
from xphi.arch.contract.reflex.auth import missing_credentials, unit_auth
from xphi.node.runtime.reflex.engine import DispatcherFn, FlowEngine
from xphi.arch.contract.reflex.flow import UnitContract
from xphi.arch.xor.fsm.parser import verify_asl

from fiber.node.runbook.unit.indexer import UnitsIndexer
from fiber.node.runbook.unit.loader import UnitsLoader
from fiber.node.runbook.trigger.base import DEFAULT_TRIGGERS, TriggerSpec
from fiber.node.runbook.stepper import (
    emit_flow_dsl,
    emit_unit_dsl,
    format_llm_catalog,
    format_llm_diagnostic,
    inspect_flow,
)
from fiber.node.runbook.viewport.projector import (
    SynthesisReport,
    TelemetryEvent,
    ViewportProjector,
    ViewportSnapshot,
)

class ViewportRuntime:
    format_llm_diagnostic = staticmethod(format_llm_diagnostic)

    def __init__(
        self,
        registry: FlowRegistry | None = None,
        loader: UnitsLoader | None = None,
        *,
        available_credentials: set[str] | None = None,
        connected_accounts: dict[str, list[str]] | None = None,
        auto_sync_catalog: bool = False,
    ) -> None:
        self.registry = registry or load_sample_registry()
        self.loader = loader or UnitsLoader()
        self.indexer = UnitsIndexer(self.loader)
        self.available_credentials = set(available_credentials) if available_credentials is not None else None
        self.connected_accounts = dict(connected_accounts) if connected_accounts is not None else None

        self.flow_triggers: dict[str, tuple[TriggerSpec, ...]] = {}
        self.published_flows: set[str] = set()

        if auto_sync_catalog:
            self.sync_catalog()

    def set_credentials(
        self,
        available_credentials: set[str] | list[str] | None = None,
        connected_accounts: dict[str, list[str]] | None = None,
    ) -> None:
        if available_credentials is not None:
            self.available_credentials = set(available_credentials)
        if connected_accounts is not None:
            self.connected_accounts = {k: list(v) for k, v in connected_accounts.items()}
            if self.available_credentials is None:
                self.available_credentials = set(connected_accounts.keys())

    def _clone_registry(self) -> FlowRegistry:
        shadow = FlowRegistry()
        shadow.units = dict(self.registry.units)
        shadow.flows = dict(self.registry.flows)
        return shadow

    def project(self, flow_name: str, *, inspection: Any | None = None) -> ViewportSnapshot:
        return ViewportProjector.project(
            self.registry,
            flow_name,
            available_credentials=self.available_credentials,
            connected_accounts=self.connected_accounts,
            inspection=inspection,
            triggers=self.flow_triggers.get(flow_name, DEFAULT_TRIGGERS),
            published=(flow_name in self.published_flows),
        )

    # Catalog Sync & Search
    def sync_catalog(self, ref: str | None = None, *, force_refresh: bool = False) -> int:
        return self.indexer.sync_into_registry(self.registry, ref=ref, force_refresh=force_refresh)

    def search_catalog(
        self,
        query: str = "",
        *,
        available_inputs: set[str] | list[str] | None = None,
        required_outputs: set[str] | list[str] | None = None,
        available_credentials: set[str] | list[str] | None = None,
        scheme: str | None = None,
        limit: int = 15,
    ) -> str:
        matches = self.indexer.search_catalog(
            self.registry,
            query=query,
            available_inputs=available_inputs,
            required_outputs=required_outputs,
            scheme=scheme,
            limit=limit * 2,
        )
        creds = set(available_credentials) if available_credentials is not None else self.available_credentials
        if creds is not None:
            matches = [u for u in matches if not missing_credentials({str(a) for a in unit_auth(u)}, creds)]
        matches = matches[:limit]
        if not matches:
            return "[CATALOG SEARCH] No matching units found."

        lines = [f"[CATALOG MATCHES ({len(matches)})]"]
        lines.extend(f"  - {u.name}: {emit_unit_dsl(u)}  # {u.desc}" for u in matches)
        return "\n".join(lines)

    def export_llm_catalog(self, *, max_units: int = 30) -> str:
        return format_llm_catalog(self.registry, max_units=max_units)

    # LLM Synthesis & Constrained Human Slot Patching
    def synthesize_unit(
        self,
        unit_name: str,
        proposed_unit_dsl: str,
        *,
        desc: str = "",
        commit: bool = False,
    ) -> UnitContract:
        unit = FlowDSL.parse_unit(unit_name, proposed_unit_dsl, desc)
        if unit.scheme == "llm" and unit.auth:
            raise ValueError(f"LLM unit '{unit_name}' must not declare user-app auth (§5).")
        if commit:
            self.registry.units[unit_name] = unit
        return unit

    def _synthesize_on_registry(
        self,
        shadow_reg: FlowRegistry,
        flow_name: str,
        proposed_dsl: str,
        *,
        desc: str = "",
        commit: bool = False,
    ) -> SynthesisReport:
        old_steps = [s.name for s in self.registry.flows[flow_name].steps] if flow_name in self.registry.flows else []

        try:
            flow_contract = shadow_reg.register_flow(flow_name, proposed_dsl, desc)
            inspection = inspect_flow(shadow_reg, flow_name)
            if inspection.errors:
                return SynthesisReport(flow_name=flow_name, dsl=proposed_dsl, valid=False, errors=tuple(inspection.errors))
            asl = FlowDSL.compile_to_asl(flow_name, shadow_reg)
            if asl_errors := verify_asl(asl):
                return SynthesisReport(flow_name=flow_name, dsl=proposed_dsl, valid=False, errors=tuple(asl_errors))
            FlowEngine.compile_asl_nodes(asl, validate=False)
            snap = ViewportProjector.project(
                shadow_reg,
                flow_name,
                available_credentials=self.available_credentials,
                connected_accounts=self.connected_accounts,
                inspection=inspection,
                triggers=self.flow_triggers.get(flow_name, DEFAULT_TRIGGERS),
                published=False if commit else (flow_name in self.published_flows),
            )
        except Exception as exc:
            return SynthesisReport(flow_name=flow_name, dsl=proposed_dsl, valid=False, errors=(str(exc),))

        if commit:
            self.registry.units = shadow_reg.units
            self.registry.flows = shadow_reg.flows
            self.published_flows.discard(flow_name)
            self.flow_triggers.setdefault(flow_name, DEFAULT_TRIGGERS)

        new_steps = [s.name for s in flow_contract.steps]
        return SynthesisReport(
            flow_name=flow_name,
            dsl=snap.formula_dsl,
            valid=True,
            warnings=tuple(inspection.warnings),
            snapshot=snap,
            added_steps=tuple(s for s in new_steps if s not in old_steps),
            removed_steps=tuple(s for s in old_steps if s not in new_steps),
        )

    def synthesize(
        self,
        flow_name: str,
        proposed_dsl: str,
        *,
        desc: str = "",
        commit: bool = False,
    ) -> SynthesisReport:
        return self._synthesize_on_registry(
            self._clone_registry(), flow_name, proposed_dsl, desc=desc, commit=commit
        )

    def patch_slot(
        self,
        flow_name: str,
        step_name: str,
        *,
        new_defaults: dict[str, Any] | None = None,
        new_cfg: dict[str, Any] | None = None,
        new_gate_expr: str | None = None,
        swap_with: str | None = None,
    ) -> SynthesisReport:
        if flow_name not in self.registry.flows:
            raise KeyError(f"Flow '{flow_name}' not found in registry.")
        flow = self.registry.flows[flow_name]
        shadow_reg = self._clone_registry()
        target_step = swap_with or step_name

        if new_defaults is not None and target_step in shadow_reg.units:
            u = shadow_reg.units[target_step]
            shadow_reg.units[target_step] = replace(u, in_defaults={**u.in_defaults, **new_defaults})

        merged_cfg = dict(flow.cfg_values)
        if new_cfg is not None:
            merged_cfg.update({f"{target_step}.{k}": v for k, v in new_cfg.items()})

        updated_steps = tuple(
            replace(s, gate_expr=new_gate_expr)
            if (s.name == step_name and new_gate_expr is not None and s.gate_expr is not None)
            else replace(s, name=swap_with)
            if (s.name == step_name and swap_with is not None)
            else s
            for s in flow.steps
        )

        candidate_dsl = emit_flow_dsl(replace(flow, steps=updated_steps, cfg_values=merged_cfg))
        return self._synthesize_on_registry(shadow_reg, flow_name, candidate_dsl, desc=flow.desc, commit=True)

    def _default_dispatch(self, scheme: str, target: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if scheme == "flow":
            _, sub_ctx = self.execute_with_trace(target, initial_context=payload)
            return sub_ctx
        if scheme in ("pass", "py", "exec"):
            return self.loader.dispatch(scheme, target, payload)
        if not hasattr(self, "_polyglot_bridge") or self._polyglot_bridge is None:
            from fiber.gateway.bridge.reflex.polyglot import PolyglotExecutionBridge
            self._polyglot_bridge = PolyglotExecutionBridge(loader=self.loader)
        return self._polyglot_bridge.dispatch_sync(scheme, target, payload)

    def execute_with_trace(
        self,
        flow_name: str,
        dispatcher: DispatcherFn | None = None,
        initial_context: dict[str, Any] | None = None,
        *,
        allow_incomplete_auth: bool = False,
    ) -> tuple[ViewportSnapshot, dict[str, Any]]:
        base_dispatch = dispatcher or self._default_dispatch
        snap = self.project(flow_name)

        if not allow_incomplete_auth:
            if snap.missing_auth:
                raise PermissionError(f"Cannot execute '{flow_name}': missing required credentials {list(snap.missing_auth)}.")
            if snap.open_account_slots:
                raise PermissionError(f"Cannot execute '{flow_name}': ambiguous accounts require selection for {list(snap.open_account_slots)}.")

        snap.status = "running"
        seq = 0

        def _traced_dispatcher(scheme: str, target: str, payload: dict[str, Any]) -> dict[str, Any] | None:
            nonlocal seq
            current_node = engine.state
            try:
                out = base_dispatch(scheme, target, payload)
            except BaseException as exc:
                seq += 1
                snap.trace.append(TelemetryEvent(seq=seq, timestamp=time.time(), state=current_node, event="fail_caught", error=str(exc)))
                raise

            seq += 1
            is_dry_skip = isinstance(out, dict) and out.pop("__dry_run_skipped__", False)
            is_hitl = isinstance(out, dict) and out.get("_hitl_yield") is True
            ev_kind = "dry_run_skip" if is_dry_skip else ("hitl_parked" if is_hitl else "step_ok")
            score = out.get("_score") if isinstance(out, dict) else None
            emitted = tuple(out.keys()) if isinstance(out, dict) else ()
            snap.trace.append(TelemetryEvent(seq=seq, timestamp=time.time(), state=current_node, event=ev_kind, score=score, emitted_keys=emitted))
            return out

        engine = FlowEngine.from_contract(flow_name, dispatcher=_traced_dispatcher, registry=self.registry)
        try:
            final_ctx = engine.invoke(initial_context)
            snap.active_state = engine.state
            snap.status = "parked" if final_ctx.get("_hitl_yield") is True else "succeeded"
            return snap, final_ctx
        except BaseException as exc:
            seq += 1
            snap.active_state = engine.state
            snap.status = "failed"
            snap.trace.append(TelemetryEvent(seq=seq, timestamp=time.time(), state=engine.state, event="halted", error=str(exc)))
            raise