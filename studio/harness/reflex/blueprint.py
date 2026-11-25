# fiber.studio.harness.reflex.blueprint
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from xphi.node.runtime.reflex.engine import FlowEngine
from fiber.studio.runbook.unit.indexer import UnitsIndexer
from fiber.studio.runbook.unit.loader import UnitsLoader
from fiber.studio.runbook.stepper import emit_flow_dsl, emit_unit_dsl, inspect_flow
from fiber.studio.runbook.runtime.viewport import ViewportRuntime
from xphi.arch.reflex.flow import (
    CompiledChoice,
    CompiledTask,
    CompiledTerminal,
    FlowContract,
    Shape,
    UnitContract,
)
from xphi.arch.reflex.compiler import compile_to_asl


def _mock_val_for_shape(shape: Shape) -> Any:
    if shape.is_list:
        return [_mock_val_for_shape(shape.element())]
    if shape.base == "num":
        return 1.0
    if shape.base == "bool":
        return True
    if shape.base == "obj":
        return {"text": "mock_text", "url": "https://example.com"}
    return "mock_text"


def _task_steps(flow: FlowContract) -> list[str]:
    return [s.name for s in flow.steps if s.gate_expr is None]

class BlueprintAnalyzer:
    def __init__(self, repo_arg: str | Path = "reflexor", spec_arg: str | Path = "", **_: Any) -> None:
        cand = Path(repo_arg).resolve()
        self.vp = ViewportRuntime(auto_sync_catalog=False)
        self.loader = UnitsLoader(local_repo_dir=cand if cand.is_dir() else None)
        self.indexer = UnitsIndexer(self.loader)
        root = self.loader.local_repo_dir

        scen_file = root / "flow" / "scenarios.json" if root else None
        self.scenarios = json.loads(scen_file.read_text(encoding="utf-8")) if (scen_file and scen_file.is_file()) else {}

        # 1. 엔진 기본 샘플을 비우고 현재 저장소(스크립트 주석 + flow/*.json)만 로드
        reg = self.vp.registry
        reg.units.clear()
        reg.flows.clear()

        self.idx = self.indexer.load_catalog_index() if root else {}
        if root:
            self.indexer.sync_into_registry(reg)
        if spec_arg and Path(spec_arg).is_file():
            reg.load_json(spec_arg)

        # 2. 플로우에 적힌 미선언 유닛이 있어도 전체 조망/트레이스가 끊기지 않게 스텁 등록
        self.stubs: set[str] = set()
        for flow in reg.flows.values():
            for sname in _task_steps(flow):
                if sname not in reg.units and sname not in reg.flows:
                    reg.register_unit(sname, f"in() -> mcp(stub/{sname}) -> out(_score: num)", "[UNRESOLVED]")
                    self.stubs.add(sname)

    @property
    def flow_names(self) -> list[str]:
        return list(self.vp.registry.flows.keys())

    def _unit_path(self, unit_name: str, target_str: str = "") -> str | None:
        units_idx = self.idx.get("units", {})
        meta = units_idx.get(unit_name) or units_idx.get(unit_name.split(":", 1)[0], {})
        if isinstance(meta, dict) and meta.get("path"):
            return str(meta["path"])
        cand = target_str.split(":", 1)[0].strip()
        root = self.loader.local_repo_dir
        return cand if (root and cand and (root / cand).is_file()) else None

    def _fixture_for(self, unit_name: str, rel_path: str | None) -> Path | None:
        root = self.loader.local_repo_dir
        if not root or not (fx_dir := root / "fixtures").is_dir():
            return None
        safe_full = unit_name.replace(":", "_") if ":" in unit_name else unit_name
        base_unit = unit_name.split(":", 1)[0] if ":" in unit_name else ""
        candidates = dict.fromkeys(filter(None, (safe_full, base_unit, Path(rel_path).stem if rel_path else "")))
        for stem in candidates:
            if hits := sorted(fx_dir.rglob(f"*{stem}*.json")):
                return hits[0]
        return None

    def resolve_targets(self, arg: str = "") -> list[str]:
        token, names = arg.strip(), self.flow_names
        if not token or token == "all":
            return names
        if token.isdigit() and 1 <= int(token) <= len(names):
            return [names[int(token) - 1]]
        if token in names:
            return [token]
        # flow/<token>.json 파일 단위 지정
        root = self.loader.local_repo_dir
        spec = root / "flow" / f"{token.removesuffix('.json')}.json" if root else None
        if spec and spec.is_file():
            declared = json.loads(spec.read_text(encoding="utf-8")).get("flows", {})
            if hits := [f for f in names if f in declared]:
                return hits
        if len(matches := [f for f in names if token in f]) == 1:
            return matches
        menu = ", ".join(f"[{i + 1}] {n}" for i, n in enumerate(names))
        raise KeyError(f"Unknown flow '{token}'. Available: {menu}")

    def evaluate_readiness(self) -> str:
        reg = self.vp.registry
        root = self.loader.local_repo_dir
        adapters_map = self.loader.discover_adapters()
        lines = [
            "==========================================================================",
            " [REFLEX OVERVIEW] Workflows, Units (Script SSOT) & Adapters",
            "==========================================================================",
            f" Repo Root : {root or '(Not found)'}",
        ]
        if adapters_map:
            lines.append(" Adapters  : " + ", ".join(f"{k}({len(v)})" for k, v in adapters_map.items()))

        lines.append("\n[1] Workflows Overview")
        for fname, flow in reg.flows.items():
            steps = _task_steps(flow)
            unresolved = [s for s in steps if s in self.stubs]
            ins = inspect_flow(reg, fname)
            notes = list(ins.errors)
            if not notes:
                try:
                    compile_to_asl(fname, reg)
                except Exception as exc:
                    notes.append(str(exc))

            status = f"PARTIAL(stub:{len(unresolved)})" if unresolved else (f"MISMATCH({len(notes)})" if notes else "READY")
            icon = "✓" if status == "READY" else "~"
            chain = " -> ".join(f"{s}(?)" if s in self.stubs else s for s in steps)
            lines.append(f"  {icon} [{status:<18}] {fname}")
            lines.append(f"      Chain : {chain}")
            for n in notes:
                lines.append(f"      Note  : {n}")

        lines.append("\n[2] Units Inventory")
        for uname, u in sorted(reg.units.items()):
            if uname in self.stubs:
                lines.append(f"  ? [{'UNRESOLVED':<18}] {uname:<26} -> missing script/preset")
                continue
            rel = self._unit_path(uname, u.target)
            fx = self._fixture_for(uname, rel)
            fx_tag = fx.relative_to(root).as_posix() if (root and fx) else "-"
            kind = "SCRIPT_OK" if rel else "DELEGATED"
            loc = f"{rel} ({u.scheme}) | fx={fx_tag}" if rel else f"{u.scheme}://{u.target}"
            lines.append(f"  ✓ [{kind:<18}] {uname:<26} -> {loc}")

        return "\n".join(lines)

    def inspect_workflow(self, flow_name: str) -> str:
        reg = self.vp.registry
        root = self.loader.local_repo_dir
        flow = reg.flows[flow_name]
        ins = inspect_flow(reg, flow_name)
        lines = [f"=== Flow: {flow_name} ===", f"DSL : {emit_flow_dsl(flow)}"]
        for e in ins.errors:
            lines.append(f"  ! Note: {e}")

        try:
            asl = FlowDSL.compile_to_asl(flow_name, reg)
            _, nodes = FlowEngine.compile_asl_nodes(asl, validate=False)
        except Exception as exc:
            lines.append(f"\n[Steps View] (ASL compile bypassed: {exc})")
            for s in flow.steps:
                if s.gate_expr:
                    lines.append(f"  * [GATE] {s.name} : {s.gate_expr}")
                else:
                    u = reg.units.get(s.name)
                    lines.append(f"  * [STEP] {s.name:<24} -> {emit_unit_dsl(u) if u else '(subflow)'}")
            return "\n".join(lines)

        lines.append("\n[Compiled Nodes & Physical Binding]")
        for name, node in nodes.items():
            if isinstance(node, CompiledTask):
                unit = reg.units.get(name) or reg.units.get(node.target)
                u_name = unit.name if unit else name
                rel = self._unit_path(u_name, unit.target if unit else node.target)
                fx = self._fixture_for(u_name, rel)
                fx_rel = fx.relative_to(root).as_posix() if (root and fx) else "-"
                lines.append(f"  * [TASK] {name} -> next: {node.next_state} (catch: {node.catch_state or '-'})")
                lines.append(f"      Contract : {emit_unit_dsl(unit) if unit else f'{node.scheme}://{node.target}'}")
                lines.append(f"      Repo     : script={rel or '-'} | fixture={fx_rel}")
            elif isinstance(node, CompiledChoice):
                branches = ", ".join(f"{dst} (on {rule})" for rule, dst in node.rules)
                lines.append(f"  * [CHOICE] {name} -> {branches} | default: {node.default_state}")
            elif isinstance(node, CompiledTerminal):
                lines.append(f"  * [END:{'SUCCEED' if node.succeeded else 'FAIL'}] {name}")
        return "\n".join(lines)

    def run_trace(self, flow_name: str, *, hybrid: bool = False) -> dict[str, Any]:
        reg = self.vp.registry
        flow = reg.flows[flow_name]
        scen_cfg = self.scenarios.get("flows", {}).get(flow_name, {})
        local_exec = set(self.scenarios.get("defaults", {}).get("local_exec_units", []))
        mock_outputs: dict[str, dict[str, Any]] = scen_cfg.get("mock_outputs", {})

        seeded_ctx: dict[str, Any] = {k: _mock_val_for_shape(flow.in_shapes.get(k, Shape())) for k in flow.in_reqs}
        seeded_ctx.update(flow.in_defaults)
        for opt_k in flow.in_optional:
            seeded_ctx.setdefault(opt_k, None)
        if isinstance(scen_cfg.get("seed"), dict):
            seeded_ctx.update(scen_cfg["seed"])

        def _dispatcher(scheme: str, target: str, payload: dict[str, Any]) -> dict[str, Any]:
            unit: UnitContract | None = next(
                (u for u in reg.units.values() if u.scheme == scheme and u.target == target and u.name not in self.stubs),
                next((u for u in reg.units.values() if u.scheme == scheme and u.target == target), None),
            )
            u_name = unit.name if unit else ""
            rel_path = self._unit_path(u_name, target)

            if hybrid and rel_path and scheme in ("py", "exec") and (not local_exec or u_name in local_exec) and u_name not in mock_outputs:
                fn_part = target.partition(":")[2]
                real_target = f"{rel_path}:{fn_part}" if fn_part else rel_path
                res = self.loader.dispatch(scheme, real_target, payload) or {}
                res.setdefault("_score", 1.0)
                return res

            if u_name in mock_outputs:
                out = dict(mock_outputs[u_name])
            elif (fx := self._fixture_for(u_name, rel_path)) and fx.is_file():
                raw = json.loads(fx.read_text(encoding="utf-8"))
                out = dict(raw.get("output") or raw.get("out") or raw) if isinstance(raw, dict) else {}
            elif unit is not None:
                out = {k: _mock_val_for_shape(unit.out_shape(k)) for k in unit.out_keys}
            else:
                out = {}

            out.setdefault("_score", 1.0)
            out["__dry_run_skipped__"] = True
            return out

        try:
            snap, final_ctx = self.vp.execute_with_trace(
                flow_name,
                dispatcher=_dispatcher,
                initial_context=seeded_ctx,
                allow_incomplete_auth=True,
            )
            return {
                "flow": flow_name,
                "mode": "hybrid" if hybrid else "dry-run",
                "status": snap.status,
                "active_state": snap.active_state,
                "trace": [{"seq": t.seq, "state": t.state, "event": t.event, "emitted": list(t.emitted_keys)} for t in snap.trace],
                "final_context": final_ctx,
            }
        except Exception as exc:
            cause = f" ({exc.__cause__})" if exc.__cause__ else ""
            return {
                "flow": flow_name,
                "mode": "hybrid" if hybrid else "dry-run",
                "status": "FAILED",
                "error": f"{exc}{cause}",
                "steps": _task_steps(flow),
            }

    def format_list(self, max_units: int = 60) -> str:
        lines = [f"  [{i}] {f:<28} : {emit_flow_dsl(self.vp.registry.flows[f])}" for i, f in enumerate(self.flow_names, 1)]
        lines.append("\n" + self.vp.export_llm_catalog(max_units=max_units))
        return "\n".join(lines)