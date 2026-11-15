# fiber.gateway.reflex.flow.dsl
from __future__ import annotations
from dataclasses import replace
import json
from pathlib import Path
import re
from typing import Any

from xphi.arch.contract.reflex.flow import (
    SCHEMES,
    ArgSpec,
    AuthReq,
    FlowContract,
    Shape,
    StepSpec,
    UnitContract,
    parse_annotated_args,
)
from xphi.node.space.bind.resolver import resolve_path

FLOW_DSL_ROOT = resolve_path("compats") / "flow"
SAMPLE_PATH = FLOW_DSL_ROOT / "sample.json"

def _split_clause(token: str) -> tuple[str, str]:
    op, paren, rest = token.strip().partition("(")
    if not paren or not rest.endswith(")"):
        raise ValueError(f"Malformed clause '{token.strip()}'.")
    return op.strip(), rest[:-1].strip()

class FlowRegistry:
    def __init__(self) -> None:
        self.units: dict[str, UnitContract] = {}
        self.flows: dict[str, FlowContract] = {}

    def get(self, name: str) -> UnitContract | FlowContract:
        """Retrieve either a UnitContract or FlowContract by name."""
        if name in self.units:
            return self.units[name]
        if name in self.flows:
            return self.flows[name]
        raise KeyError(f"Unregistered unit or flow contract: '{name}'")

    def register_unit(self, name: str, spec: str, desc: str = "") -> UnitContract:
        unit = FlowDSL.parse_unit(name, spec, desc)
        self.units[name] = unit
        return unit

    def register_flow(self, name: str, spec: str, desc: str = "") -> FlowContract:
        flow = FlowDSL.parse_flow(name, spec, desc)
        self.flows[name] = flow
        return flow

    def load_dict(self, data: dict[str, Any]) -> FlowRegistry:
        for kind, register_fn in (("units", self.register_unit), ("flows", self.register_flow)):
            for name, meta in data.get(kind, {}).items():
                spec = meta if isinstance(meta, str) else meta["flow"]
                desc = "" if isinstance(meta, str) else meta.get("desc", "")
                register_fn(name, spec, desc)
        return self

    def load_json(self, path: str | Path = SAMPLE_PATH) -> FlowRegistry:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return self.load_dict(raw)

    def resolve_io(self, name: str) -> tuple[str, tuple[str, ...], dict[str, Any], tuple[str, ...]]:
        """Return (resource_uri, in_reqs, in_defaults, out_keys) for either a Unit or a Sub-Flow."""
        c = self.get(name)
        return c.resource_uri, c.in_reqs, c.in_defaults, c.out_keys

    def resolve_full_io(
        self, name: str
    ) -> tuple[
        tuple[str, ...],
        dict[str, Any],
        tuple[str, ...],
        dict[str, Shape],
        tuple[str, ...],
        dict[str, Shape],
        tuple[str, ...],
    ]:
        """Return (in_reqs, in_defaults, in_optional, in_shapes, out_keys, out_shapes, out_optional)."""
        c = self.get(name)
        return c.in_reqs, c.in_defaults, c.in_optional, c.in_shapes, c.out_keys, c.out_shapes, c.out_optional

class FlowDSL:
    @staticmethod
    def parse_args(arg_str: str) -> tuple[tuple[str, ...], dict[str, Any]]:
        spec = parse_annotated_args(arg_str)
        return spec.all_keys, spec.defaults

    @classmethod
    def parse_unit(cls, name: str, spec_str: str, desc: str = "") -> UnitContract:
        ins, cfgs, outs = ArgSpec(), ArgSpec(), ArgSpec()
        auth: tuple[AuthReq, ...] = ()
        scheme, target = "", ""

        for token in (s.strip() for s in spec_str.split("->") if s.strip()):
            op, body = _split_clause(token)
            if op == "in":
                ins = parse_annotated_args(body)
            elif op == "cfg":
                cfgs = parse_annotated_args(body)
            elif op == "auth":
                auth = AuthReq.parse_list(body)
            elif op == "out":
                outs = parse_annotated_args(body)
            elif op in SCHEMES:
                scheme, target = op, body
            else:
                raise ValueError(f"Unknown unit clause '{op}(...)' in unit '{name}'.")

        if not scheme or not target:
            raise ValueError(f"Unit '{name}' is missing an execution target ({'/'.join(SCHEMES)}).")

        return UnitContract.with_specs(
            name, scheme, target, desc, ins=ins, cfgs=cfgs, outs=outs, auth=auth
        )

    @classmethod
    def parse_step(cls, token: str) -> StepSpec:
        t = token.strip()
        if t.startswith("gate(") and t.endswith(")"):
            inner = t[5:-1].strip()
            name, sep, expr = inner.partition(":")
            return (
                StepSpec(name=name.strip(), gate_expr=expr.strip())
                if sep
                else StepSpec(name="evaluate_gate", gate_expr=name.strip())
            )

        if t.startswith("each(") and t.endswith(")"):
            head, _, inner = t[5:-1].partition(":")
            item, _, over = head.partition(" in ")
            return replace(cls.parse_step(inner), each_item=item.strip(), each_over=over.strip())

        base, heal_mode, heal_target = t, "retry", None
        if "(!" in t:
            base, _, rest = t.partition("(!")
            heal_mode = "fallback" if rest.startswith(">") else "retry"
            heal_target = rest.lstrip(">").rstrip(")").strip()

        name, _, bind_body = base.partition("[")
        bindings = {
            dst.strip(): src.strip()
            for pair in bind_body.rstrip("]").split(",")
            if pair.strip()
            for dst, _, src in [pair.partition("=")]
        }

        return StepSpec(name=name.strip(), heal_target=heal_target, heal_mode=heal_mode, bindings=bindings)

    @classmethod
    def parse_flow(cls, name: str, flow_str: str, desc: str = "") -> FlowContract:
        tokens = [s.strip() for s in flow_str.split("->") if s.strip()]
        ins, outs = ArgSpec(), ArgSpec()
        cfg_values: dict[str, Any] = {}
        steps: list[StepSpec] = []

        for idx, tok in enumerate(tokens):
            if not steps and tok.startswith("in(") and tok.endswith(")"):
                ins = parse_annotated_args(_split_clause(tok)[1])
            elif not steps and tok.startswith("cfg(") and tok.endswith(")"):
                cfg_values.update(parse_annotated_args(_split_clause(tok)[1]).defaults)
            elif idx == len(tokens) - 1 and tok.startswith("out(") and tok.endswith(")"):
                outs = parse_annotated_args(_split_clause(tok)[1])
            else:
                steps.append(cls.parse_step(tok))

        if not steps:
            raise ValueError(f"Flow '{name}' must contain at least one execution step.")

        return FlowContract.with_specs(
            name, tuple(steps), desc, ins=ins, outs=outs, cfg_values=cfg_values
        )

    @classmethod
    def _build_asl_task(
        cls,
        step: StepSpec,
        owner: FlowContract,
        registry: FlowRegistry,
        ctx_pool: set[str],
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        contract = registry.get(step.name)
        params: dict[str, Any] = {}

        local_pool = set(ctx_pool)
        if step.is_each and step.each_item:
            if step.each_over not in ctx_pool:
                raise KeyError(
                    f"Contract violation at '{step.name}': each_over '{step.each_over}' not in context {sorted(ctx_pool)}."
                )
            local_pool.add(step.each_item)

        # Config defaults & flow-level cfg overrides (for atomic units)
        if isinstance(contract, UnitContract):
            params.update(contract.cfg_defaults)
            params.update(owner.cfg_for(step.name))

        # Data defaults (overridden by context if source key exists in local_pool)
        for k, def_val in contract.in_defaults.items():
            src = step.source_of(k)
            if src in local_pool:
                params[f"{k}.$"] = f"$.{src}"
            else:
                params[k] = def_val

        # Optional & Required data inputs
        for k in contract.in_optional:
            src = step.source_of(k)
            if src in local_pool:
                params[f"{k}.$"] = f"$.{src}"

        for k in contract.in_reqs:
            src = step.source_of(k)
            if src not in local_pool and k not in contract.in_defaults:
                raise KeyError(
                    f"Contract violation at '{step.name}': required input '{k}' (<- '{src}') not provided by upstream context {sorted(local_pool)}."
                )
            if src in local_pool:
                params[f"{k}.$"] = f"$.{src}"

        # Fiber runtime extensions (Projection, Optional inputs, Each fan-out)
        fiber_ext: dict[str, Any] = {}
        if contract.out_keys or contract.in_optional:
            proj: dict[str, Any] = {}
            if contract.out_keys:
                proj["extract_keys"] = [f"$.{k}" for k in contract.out_keys]
            if contract.in_optional:
                proj["optional_in"] = list(contract.in_optional)
            fiber_ext["Projection"] = proj
        if step.is_each and step.each_item and step.each_over:
            fiber_ext["Each"] = {"item": step.each_item, "over": f"$.{step.each_over}"}
        if fiber_ext:
            params["__fiber_extensions"] = fiber_ext

        node: dict[str, Any] = {"Type": "Task", "Resource": contract.resource_uri}
        if params:
            node["Parameters"] = params
        return node, contract.out_keys

    @classmethod
    def compile_to_asl(
        cls,
        target: str | dict[str, Any],
        registry: FlowRegistry,
    ) -> dict[str, Any]:
        """Compile a FlowContract (including nested Sub-Flow blocks) into hidden ASL IR."""
        if isinstance(target, dict) and "States" in target:
            return target

        flow_name = target if isinstance(target, str) else target["name"]
        if flow_name not in registry.flows:
            raise KeyError(f"Unregistered workflow contract: '{flow_name}'")
        flow = registry.flows[flow_name]

        states: dict[str, Any] = {}
        ctx_pool: set[str] = set(flow.in_reqs) | set(flow.in_defaults.keys()) | set(flow.in_optional)

        for i, step in enumerate(flow.steps):
            next_id = flow.steps[i + 1].name if i + 1 < len(flow.steps) else "success_end"

            if step.gate_expr is not None:
                expr = step.gate_expr
                for k in sorted(ctx_pool, key=len, reverse=True):
                    expr = re.sub(rf"(?<!\$\.)\b{re.escape(k)}\b", f"$.{k}", expr)
                states[step.name] = {
                    "Type": "Choice",
                    "Choices": [{"if": expr, "Next": next_id}],
                    "Default": "fail_end",
                }
                continue

            node, outs = cls._build_asl_task(step, flow, registry, ctx_pool)
            node["Next"] = next_id

            if step.heal_target:
                node["Catch"] = step.heal_target
                states[step.name] = node

                heal_step = StepSpec(name=step.heal_target)
                heal_node, heal_outs = cls._build_asl_task(heal_step, flow, registry, ctx_pool)
                if step.heal_mode == "retry":
                    heal_node["Next"] = step.name
                else:
                    heal_node["Next"] = next_id
                    ctx_pool.update(heal_outs)
                heal_node["Catch"] = "fail_end"
                states[step.heal_target] = heal_node
            else:
                node["Catch"] = "fail_end"
                states[step.name] = node

            ctx_pool.update(outs)

        missing_outs = set(flow.out_keys) - ctx_pool
        if missing_outs:
            raise KeyError(
                f"Workflow '{flow.name}' exit contract violation: declared out({', '.join(sorted(missing_outs))}) never produced."
            )
        states["success_end"] = {"Type": "Succeed"}
        states["fail_end"] = {"Type": "Fail"}
        return {"Version": "1.0", "StartAt": flow.steps[0].name, "States": states}

def load_sample_registry(path: str | Path = SAMPLE_PATH) -> FlowRegistry:
    return FlowRegistry().load_json(path)