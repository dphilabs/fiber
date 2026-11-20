# fiber.node.reflex.workspace.projection
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from xphi.arch.contract.registry.flow import FlowRegistry
from xphi.arch.contract.reflex.auth import missing_credentials, resolve_accounts
from xphi.arch.contract.reflex.flow import FlowContract, StepSpec, UnitContract

from fiber.node.reflex.trigger.base import DEFAULT_TRIGGERS, TriggerSpec
from fiber.node.reflex.workspace.inspector import (
    block_auth,
    emit_flow_dsl,
    emit_step_dsl,
    emit_unit_dsl,
    format_synthesis_feedback,
    inspect_flow,
)

@dataclass(frozen=True, slots=True)
class EdgeWiringView:
    source: str
    target: str
    kind: str  # 'step' | 'heal_catch' | 'heal_return' | 'gate_pass' | 'gate_fail'
    bound_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NodeSlotView:
    name: str
    kind: str  # 'unit' | 'subflow' | 'gate' | 'heal'
    resource_uri: str
    in_reqs: tuple[str, ...]
    in_defaults: dict[str, Any]
    out_keys: tuple[str, ...]
    in_optional: tuple[str, ...] = ()
    cfg_reqs: tuple[str, ...] = ()
    cfg_defaults: dict[str, Any] = field(default_factory=dict)
    cfg_overrides: dict[str, Any] = field(default_factory=dict)
    auth_reqs: tuple[str, ...] = ()
    bindings: dict[str, str] = field(default_factory=dict)
    each_item: str | None = None
    each_over: str | None = None
    heal_target: str | None = None
    heal_mode: str = "retry"
    gate_expr: str | None = None
    swap_candidates: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TelemetryEvent:
    seq: int
    timestamp: float
    state: str
    event: str  # 'step_ok' | 'dry_run_skip' | 'fail_caught' | 'gate_eval' | 'halted'
    next_state: str | None = None
    score: float | None = None
    error: str | None = None
    emitted_keys: tuple[str, ...] = ()


@dataclass(slots=True)
class ViewportSnapshot:
    flow_name: str
    formula_dsl: str
    entry_reqs: tuple[str, ...]
    entry_defaults: dict[str, Any]
    exit_keys: tuple[str, ...]
    nodes: tuple[NodeSlotView, ...]
    edges: tuple[EdgeWiringView, ...]
    required_auth: tuple[str, ...] = ()
    missing_auth: tuple[str, ...] = ()
    selected_accounts: dict[str, str] = field(default_factory=dict)
    open_account_slots: tuple[str, ...] = ()
    open_cfg_slots: tuple[str, ...] = ()
    triggers: tuple[TriggerSpec, ...] = DEFAULT_TRIGGERS
    published: bool = False
    trace: list[TelemetryEvent] = field(default_factory=list)
    active_state: str | None = None
    status: str = "idle"

    @property
    def is_ready_to_publish(self) -> bool:
        return not (self.missing_auth or self.open_account_slots or self.open_cfg_slots)


@dataclass(frozen=True, slots=True)
class SynthesisReport:
    flow_name: str
    dsl: str
    valid: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    snapshot: ViewportSnapshot | None = None
    added_steps: tuple[str, ...] = ()
    removed_steps: tuple[str, ...] = ()

    def to_llm_feedback(self) -> str:
        return format_synthesis_feedback(self)


class TopologyProjector:
    """Stateless coordinate-free graph projector."""

    emit_unit_dsl = staticmethod(emit_unit_dsl)
    emit_step_dsl = staticmethod(emit_step_dsl)
    emit_flow_dsl = staticmethod(emit_flow_dsl)
    BlockAuthRequirements = staticmethod(block_auth)

    @classmethod
    def find_swap_candidates(
        cls,
        registry: FlowRegistry,
        current_name: str,
        available_ctx: set[str],
        required_outs: tuple[str, ...],
        available_credentials: set[str] | None = None,
    ) -> tuple[str, ...]:
        req_out_set = set(required_outs)
        candidates: list[str] = []

        for name in (*registry.units, *registry.flows):
            if name == current_name:
                continue
            c = registry.get(name)
            if not (set(c.in_reqs) - set(c.in_defaults) <= available_ctx and req_out_set <= set(c.out_keys)):
                continue
            if available_credentials is not None and missing_credentials(block_auth(registry, name), available_credentials):
                continue
            candidates.append(name)

        return tuple(sorted(candidates))

    @staticmethod
    def compute_consumed_keys(registry: FlowRegistry, step: StepSpec, ctx_pool: set[str]) -> tuple[str, ...]:
        if step.gate_expr is not None:
            return tuple(
                sorted(k for k in ctx_pool if re.search(rf"\b{re.escape(k)}\b", step.gate_expr))
            )
        c = registry.get(step.name)
        bound: list[str] = [step.each_over] if step.is_each and step.each_over in ctx_pool else []
        for k in (*c.in_reqs, *c.in_defaults, *c.in_optional):
            src = step.source_of(k)
            if src in ctx_pool:
                bound.append(src)
        return tuple(dict.fromkeys(bound))

    @classmethod
    def _build_node_view(
        cls,
        registry: FlowRegistry,
        flow: FlowContract,
        step: StepSpec,
        *,
        kind: str | None = None,
        swaps: tuple[str, ...] = (),
    ) -> NodeSlotView:
        c = registry.get(step.name)
        is_unit = isinstance(c, UnitContract)
        resolved_kind = kind or ("unit" if is_unit else "subflow")
        return NodeSlotView(
            name=step.name,
            kind=resolved_kind,
            resource_uri=c.resource_uri,
            in_reqs=c.in_reqs,
            in_defaults=dict(c.in_defaults),
            out_keys=c.out_keys,
            in_optional=c.in_optional,
            cfg_reqs=c.cfg_reqs if is_unit else (),
            cfg_defaults=dict(c.cfg_defaults) if is_unit else {},
            cfg_overrides=flow.cfg_for(step.name),
            auth_reqs=tuple(sorted(block_auth(registry, step.name))),
            bindings=dict(step.bindings),
            each_item=step.each_item,
            each_over=step.each_over,
            heal_target=step.heal_target,
            heal_mode=step.heal_mode,
            swap_candidates=swaps,
        )

    @classmethod
    def project(
        cls,
        registry: FlowRegistry,
        flow_name: str,
        *,
        available_credentials: set[str] | None = None,
        connected_accounts: dict[str, list[str]] | None = None,
        inspection: Any | None = None,
        triggers: tuple[TriggerSpec, ...] = DEFAULT_TRIGGERS,
        published: bool = False,
    ) -> ViewportSnapshot:
        if flow_name not in registry.flows:
            raise KeyError(f"Flow '{flow_name}' not found in registry.")

        flow = registry.flows[flow_name]
        ctx_pool: set[str] = set(flow.in_reqs) | set(flow.in_defaults) | set(flow.in_optional)
        nodes: list[NodeSlotView] = []
        edges: list[EdgeWiringView] = []

        for i, step in enumerate(flow.steps):
            next_step = flow.steps[i + 1] if i + 1 < len(flow.steps) else None
            next_id = next_step.name if next_step is not None else "success_end"

            if step.gate_expr is not None:
                gate_inputs = cls.compute_consumed_keys(registry, step, ctx_pool)
                nodes.append(
                    NodeSlotView(
                        name=step.name,
                        kind="gate",
                        resource_uri="choice://gate",
                        in_reqs=gate_inputs,
                        in_defaults={},
                        out_keys=(),
                        gate_expr=step.gate_expr,
                    )
                )
                next_bound = cls.compute_consumed_keys(registry, next_step, ctx_pool) if next_step else flow.out_keys
                edges.append(EdgeWiringView(source=step.name, target=next_id, kind="gate_pass", bound_keys=next_bound))
                edges.append(EdgeWiringView(source=step.name, target="fail_end", kind="gate_fail"))
                continue

            c = registry.get(step.name)
            step_ctx = set(ctx_pool) | ({step.each_item} if step.is_each and step.each_item else set())
            swaps = cls.find_swap_candidates(registry, step.name, step_ctx, c.out_keys, available_credentials)
            nodes.append(cls._build_node_view(registry, flow, step, swaps=swaps))

            if step.heal_target:
                h_c = registry.get(step.heal_target)
                heal_step = StepSpec(name=step.heal_target)
                heal_inputs = cls.compute_consumed_keys(registry, heal_step, ctx_pool)
                nodes.append(cls._build_node_view(registry, flow, heal_step, kind="heal"))
                edges.append(EdgeWiringView(source=step.name, target=step.heal_target, kind="heal_catch", bound_keys=heal_inputs))
                ret_target = step.name if step.heal_mode == "retry" else next_id
                edges.append(EdgeWiringView(source=step.heal_target, target=ret_target, kind="heal_return", bound_keys=h_c.out_keys))
                if step.heal_mode == "fallback":
                    ctx_pool.update(h_c.out_keys)

            ctx_pool.update(c.out_keys)
            wired_to_next = cls.compute_consumed_keys(registry, next_step, ctx_pool) if next_step else flow.out_keys
            edges.append(EdgeWiringView(source=step.name, target=next_id, kind="step", bound_keys=wired_to_next or c.out_keys))

        inspection = inspection or inspect_flow(registry, flow_name)
        miss_auth = (
            tuple(sorted(missing_credentials(inspection.auth, available_credentials)))
            if available_credentials is not None
            else ()
        )
        selected_accs, open_acc_slots = (
            resolve_accounts(registry, flow_name, connected_accounts)
            if connected_accounts is not None
            else ({}, [])
        )

        return ViewportSnapshot(
            flow_name=flow.name,
            formula_dsl=emit_flow_dsl(flow),
            entry_reqs=flow.in_reqs,
            entry_defaults=dict(flow.in_defaults),
            exit_keys=flow.out_keys,
            nodes=tuple(nodes),
            edges=tuple(edges),
            required_auth=tuple(sorted(inspection.auth)),
            missing_auth=miss_auth,
            selected_accounts=selected_accs,
            open_account_slots=tuple(open_acc_slots),
            open_cfg_slots=tuple(inspection.open_slots),
            triggers=triggers or DEFAULT_TRIGGERS,
            published=published,
        )