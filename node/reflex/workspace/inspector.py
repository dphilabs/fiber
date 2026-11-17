# fiber.node.reflex.runtime.inspector
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import TYPE_CHECKING, Any

from xphi.arch.contract.registry.flow import FlowRegistry
from xphi.arch.contract.reflex.auth import (
    allowed_account_slots,
    flow_auth,
    unit_auth,
)
from xphi.arch.contract.reflex.flow import FlowContract, Shape, StepSpec, UnitContract
from xphi.arch.xor.fsm.parser import parse_if_ast

if TYPE_CHECKING:
    from fiber.node.reflex.trigger.base import TriggerSpec
    from fiber.node.reflex.workspace.projection import SynthesisReport, ViewportSnapshot

# Canonical DSL Emitter (Contract Object -> DSL String)
def _format_arg_items(
    reqs: tuple[str, ...],
    optional: tuple[str, ...],
    defaults: dict[str, Any],
    shapes: dict[str, Shape],
) -> list[str]:
    def _sh(k: str) -> str:
        return f": {shapes[k]}" if k in shapes and str(shapes[k]) != "val" else ""

    return [
        *(f"{k}{_sh(k)}" for k in reqs),
        *(f"{k}?{_sh(k)}" for k in optional),
        *(f"{k}{_sh(k)}={v!r}" for k, v in defaults.items()),
    ]


def _format_out_items(
    out_keys: tuple[str, ...],
    out_optional: tuple[str, ...],
    out_shapes: dict[str, Shape],
) -> list[str]:
    out_reqs = tuple(k for k in out_keys if k not in out_optional)
    return _format_arg_items(out_reqs, out_optional, {}, out_shapes)


def emit_unit_dsl(unit: UnitContract) -> str:
    parts: list[str] = []
    if in_items := _format_arg_items(unit.in_reqs, unit.in_optional, unit.in_defaults, unit.in_shapes):
        parts.append(f"in({', '.join(in_items)})")
    if cfg_items := _format_arg_items(unit.cfg_reqs, (), unit.cfg_defaults, unit.cfg_shapes):
        parts.append(f"cfg({', '.join(cfg_items)})")
    if unit.auth:
        parts.append(f"auth({', '.join(str(a) for a in unit.auth)})")
    parts.append(f"{unit.scheme}({unit.target})")
    if out_items := _format_out_items(unit.out_keys, unit.out_optional, unit.out_shapes):
        parts.append(f"out({', '.join(out_items)})")
    return " -> ".join(parts)


def emit_step_dsl(step: StepSpec) -> str:
    if step.gate_expr is not None:
        return f"gate({step.name}: {step.gate_expr})"
    core = step.name
    if step.bindings:
        bind_str = ", ".join(f"{dst}={src}" for dst, src in step.bindings.items())
        core = f"{core}[{bind_str}]"
    if step.heal_target:
        op = "!>" if step.heal_mode == "fallback" else "!"
        core = f"{core}({op}{step.heal_target})"
    return f"each({step.each_item} in {step.each_over}: {core})" if step.is_each else core


def emit_flow_dsl(flow: FlowContract) -> str:
    parts: list[str] = []
    if in_items := _format_arg_items(flow.in_reqs, flow.in_optional, flow.in_defaults, flow.in_shapes):
        parts.append(f"in({', '.join(in_items)})")
    if flow.cfg_values:
        parts.append(f"cfg({', '.join(f'{k}={v!r}' for k, v in flow.cfg_values.items())})")
    parts.extend(emit_step_dsl(s) for s in flow.steps)
    if out_items := _format_out_items(flow.out_keys, flow.out_optional, flow.out_shapes):
        parts.append(f"out({', '.join(out_items)})")
    return " -> ".join(parts)

# Unified Static Flow Inspector (Shared by Viewport & ContractTester)
@dataclass(slots=True)
class FlowInspection:
    flow: str
    auth: set[str] = field(default_factory=set)
    open_slots: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    asl: dict[str, Any] = field(default_factory=dict)

    def to_summary(self) -> dict[str, Any]:
        return {
            "auth": sorted(self.auth),
            "open_slots": self.open_slots,
            "warnings": self.warnings,
            "asl": self.asl,
        }


def block_auth(reg: FlowRegistry, name: str) -> set[str]:
    if name in reg.units:
        return {str(a) for a in unit_auth(reg.units[name])}
    if name in reg.flows:
        return flow_auth(reg, name)
    return set()


def _wire_step(
    reg: FlowRegistry,
    owner: FlowContract,
    step: StepSpec,
    name: str,
    pool: dict[str, Shape],
    weak: set[str],
    rep: FlowInspection,
    visited_flows: set[str],
) -> tuple[dict[str, Shape], set[str]]:
    if name not in reg.units and name not in reg.flows:
        rep.errors.append(f"{owner.name}: unregistered step or sub-flow '{name}'.")
        return {}, set()

    c = reg.get(name)
    in_keys = set(c.in_reqs) | set(c.in_defaults) | set(c.in_optional)

    for dst in step.bindings:
        if dst not in in_keys:
            rep.errors.append(f"{owner.name}:{name}: binding target '{dst}' is not an in-key of '{name}'.")

    for k in sorted(in_keys):
        src = step.source_of(k)
        required = k in c.in_reqs and k not in c.in_defaults
        if src not in pool:
            if required:
                rep.errors.append(f"{owner.name}:{name}: required input '{k}' <- '{src}' not in context {sorted(pool)}.")
            continue
        if required and src in weak:
            rep.errors.append(f"{owner.name}:{name}: required input '{k}' wired from optional output '{src}'.")

        target_shape = c.in_shapes.get(k, Shape())
        verdict = pool[src].compat(target_shape)
        msg = f"{owner.name}:{name}: '{src}' ({pool[src]}) -> '{k}' ({target_shape})"
        if verdict == "error":
            rep.errors.append(msg)
        elif verdict == "warn":
            rep.warnings.append(msg)

    if isinstance(c, UnitContract):
        if c.scheme == "llm" and c.auth:
            rep.errors.append(f"{owner.name}: llm unit '{name}' must not declare user-app auth.")
        filled = owner.cfg_for(name)
        allowed = set(c.cfg_reqs) | set(c.cfg_defaults) | allowed_account_slots(c)
        for k in sorted(set(filled) - allowed):
            rep.errors.append(f"{owner.name}: cfg '{name}.{k}' is not a valid cfg/account slot of '{name}'.")
        rep.open_slots.extend(f"{owner.name}:{name}.{k}" for k in c.cfg_reqs if k not in filled)
    else:
        if name in visited_flows:
            rep.errors.append(f"{owner.name}: cyclic sub-flow reference detected at '{name}'.")
            return {}, set()
        sub = inspect_flow(reg, name, _visited=visited_flows | {name})
        rep.errors.extend(sub.errors)
        rep.warnings.extend(sub.warnings)
        rep.open_slots.extend(sub.open_slots)

    return {k: c.out_shapes.get(k, Shape()) for k in c.out_keys}, set(c.out_optional)


def inspect_flow(
    reg: FlowRegistry,
    flow_name: str,
    *,
    _visited: set[str] | None = None,
) -> FlowInspection:
    """Single-pass static analyzer for shapes, bindings, gate syntax, open cfg slots, and auth rules."""
    if flow_name not in reg.flows:
        return FlowInspection(flow=flow_name, errors=[f"Unregistered workflow contract: '{flow_name}'."])

    visited = (_visited or set()) | {flow_name}
    flow = reg.flows[flow_name]
    rep = FlowInspection(flow=flow_name)
    pool: dict[str, Shape] = {
        k: flow.in_shapes.get(k, Shape())
        for k in (*flow.in_reqs, *flow.in_defaults, *flow.in_optional)
    }
    weak: set[str] = set(flow.in_optional)
    valid_step_targets = {n for s in flow.steps for n in (s.name, s.heal_target) if n}

    for key in flow.cfg_values:
        if key.partition(".")[0] not in valid_step_targets:
            rep.errors.append(f"{flow_name}: cfg '{key}' targets no direct or heal step.")

    for step in flow.steps:
        if step.gate_expr is not None:
            expr = step.gate_expr
            for k in sorted(pool, key=len, reverse=True):
                expr = re.sub(rf"(?<!\$\.)\b{re.escape(k)}\b", f"$.{k}", expr)
            try:
                _, path_map = parse_if_ast(expr)
                for path in path_map.values():
                    root_key = path.removeprefix("$.").partition(".")[0]
                    if root_key not in pool:
                        rep.errors.append(f"{flow_name}:{step.name}: gate references unknown key '{root_key}'.")
            except (SyntaxError, ValueError) as exc:
                rep.errors.append(f"{flow_name}:{step.name}: invalid gate expression '{step.gate_expr}' ({exc}).")
            continue

        step_pool = pool
        if step.is_each and step.each_over and step.each_item:
            src = pool.get(step.each_over)
            if src is None:
                rep.errors.append(f"{flow_name}: each over '{step.each_over}' not in context.")
                continue
            if src.base != "val" and not src.is_list:
                rep.errors.append(f"{flow_name}: each over '{step.each_over}' ({src}) is not a list.")
            item = src.element() if src.is_list else Shape()
            step_pool = {**pool, step.each_item: item}

        produced, opt = _wire_step(reg, flow, step, step.name, step_pool, weak, rep, visited)
        if step.heal_target:
            heal_out, heal_opt = _wire_step(
                reg, flow, StepSpec(name=step.heal_target), step.heal_target, step_pool, weak, rep, visited
            )
            if step.heal_mode == "fallback":
                produced = {**produced, **heal_out}
                opt |= heal_opt

        if step.is_each and step.each_over and step.each_item:
            pool.update({k: s.collected() for k, s in produced.items()})
        else:
            pool.update(produced)
        weak |= opt

    # Exit contract verification (presence, optionality propagation, and shape compatibility)
    for k in flow.out_keys:
        if k not in pool:
            rep.errors.append(f"{flow_name}: exit out('{k}') never produced.")
            continue
        if k not in flow.out_optional and k in weak:
            rep.errors.append(f"{flow_name}: required exit out('{k}') wired from optional output.")
        out_target_shape = flow.out_shapes.get(k, Shape())
        verdict = pool[k].compat(out_target_shape)
        msg = f"{flow_name}:exit: '{k}' ({pool[k]}) -> out '{k}' ({out_target_shape})"
        if verdict == "error":
            rep.errors.append(msg)
        elif verdict == "warn":
            rep.warnings.append(msg)

    rep.auth = flow_auth(reg, flow_name)
    return rep


# =====================================================================
# 3. LLM Feedback, Trigger Summary & Catalog Text Formatters
# =====================================================================

def _format_triggers(triggers: tuple[TriggerSpec, ...]) -> str:
    parts: list[str] = []
    for t in triggers:
        if t.kind == "cron":
            parts.append(f"cron({t.cron_expr!r}, tz={t.tz!r}, missed={t.missed_policy})")
        elif t.kind == "event":
            dedup = f", dedup={t.dedup_path!r}" if t.dedup_path else ""
            parts.append(f"event({t.source!r}{dedup})")
        else:
            parts.append("manual")
    return ", ".join(parts) or "manual"


def format_synthesis_feedback(report: SynthesisReport) -> str:
    if not report.valid or report.snapshot is None:
        err_block = "\n".join(f"  - {e}" for e in report.errors)
        return f"[SYNTHESIS FAILED] {report.flow_name}\n[DSL] {report.dsl}\n[ERRORS]\n{err_block}"

    snap = report.snapshot
    state_tag = "PUBLISHED" if snap.published else ("READY" if snap.is_ready_to_publish else "DRAFT")
    wiring = [
        f"  {e.source} --[{e.kind}: {', '.join(e.bound_keys) or 'ctrl'}]--> {e.target}"
        for e in snap.edges
    ]
    diff = (
        f" (+added={list(report.added_steps)}, -removed={list(report.removed_steps)})"
        if (report.added_steps or report.removed_steps)
        else ""
    )
    lines = [
        f"[SYNTHESIS OK] {report.flow_name} [{state_tag}]{diff}",
        f"[DSL]      {snap.formula_dsl}",
        f"[TRIGGERS] {_format_triggers(snap.triggers)}",
        f"[ENTRY]    in({', '.join(snap.entry_reqs)}) defaults={snap.entry_defaults}",
        f"[EXIT]     out({', '.join(snap.exit_keys)})",
    ]
    if snap.required_auth:
        auth_line = f"[AUTH]     required={list(snap.required_auth)}"
        if snap.missing_auth:
            auth_line += f" missing={list(snap.missing_auth)}"
        lines.append(auth_line)
    if snap.open_cfg_slots or snap.open_account_slots:
        lines.append(f"[SLOTS]    open_cfg={list(snap.open_cfg_slots)} open_accounts={list(snap.open_account_slots)}")
    if report.warnings:
        lines.append("[WARN]     " + "; ".join(report.warnings))
    lines.append("[WIRING]\n" + "\n".join(wiring))
    return "\n".join(lines)


def format_llm_diagnostic(snap: ViewportSnapshot) -> str:
    pub_label = "PUBLISHED" if snap.published else "DRAFT"
    lines = [
        f"[FLOW]     {snap.flow_name} [{pub_label}] ({snap.status.upper()} at '{snap.active_state}')",
        f"[DSL]      {snap.formula_dsl}",
        f"[TRIGGERS] {_format_triggers(snap.triggers)}",
    ]
    if snap.required_auth:
        lines.append(f"[AUTH]     required={list(snap.required_auth)} missing={list(snap.missing_auth)}")
    lines.append("[TRACE]")
    for ev in snap.trace:
        details = [
            *([f"score={ev.score}"] if ev.score is not None else []),
            *([f"out={list(ev.emitted_keys)}"] if ev.emitted_keys else []),
            *([f"error={ev.error!r}"] if ev.error else []),
        ]
        lines.append(f"  #{ev.seq} [{ev.state}] {ev.event} ({', '.join(details)})")
    return "\n".join(lines)


def format_llm_catalog(reg: FlowRegistry, *, max_units: int = 30) -> str:
    units = list(reg.units.values())
    lines = [f"[AVAILABLE UNITS ({min(len(units), max_units)}/{len(units)})]"]
    for u in units[:max_units]:
        lines.append(f"  - {u.name}: {emit_unit_dsl(u)}")
    if len(units) > max_units:
        lines.append("  ... (use `search_catalog(...)` to filter remaining units by in/out/auth)")

    if reg.flows:
        lines.append("[COMPOSABLE SUB-FLOWS]")
        for f in reg.flows.values():
            in_part = ", ".join(_format_arg_items(f.in_reqs, f.in_optional, f.in_defaults, f.in_shapes))
            out_part = ", ".join(_format_out_items(f.out_keys, f.out_optional, f.out_shapes))
            f_auth = sorted(flow_auth(reg, f.name))
            auth_clause = f" -> auth({', '.join(f_auth)})" if f_auth else ""
            lines.append(
                f"  - {f.name}: in({in_part}){auth_clause} -> flow({f.name}) -> out({out_part}) | {emit_flow_dsl(f)}"
            )
    return "\n".join(lines)