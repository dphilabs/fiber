# fiber.studio.harness.contract.flowdsl
"""
Harness verifier for externally injected FlowDSL specs (JSON string, file path, or dict).

Runs the full canonical verification chain per candidate against the Reflexor catalog:
  1. Grammar & Contract Parse (`parse_unit_dsl` / `parse_flow_dsl`)
  2. Blackboard Wiring & Shape Inspection (`inspect_flow`)
  3. Auth & Account Slot Resolution (`flow_auth` / `missing_credentials` / `resolve_accounts`)
  4. ASL Compilation & Structural Verification (`compile_to_asl` / `verify_asl`)
  5. FSM Bytecode/Graph Compilation (`FSMCompiler.compile_from_asl`)
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from xphi.arch.contract.registry.flow import FlowRegistry, expand_entries, parse_flow_dsl
from xphi.arch.reflex.auth import flow_auth, missing_credentials, resolve_accounts
from xphi.arch.reflex.compiler import compile_to_asl
from xphi.arch.reflex.flow import is_flow_meta
from xphi.arch.xor.asl.evaluator import verify_asl
from xphi.arch.xor.fsm.compiler import FSMCompiler

from fiber.node.reflex.runbook.inspector import inspect_flow
from fiber.studio.reflex.indexer import UnitsIndexer
from fiber.studio.reflex.loader import UnitsLoader

class ContractVerificationError(AssertionError):
    """Raised in `strict=True` mode when any injected unit or flow fails verification."""

def _clone_registry(base: FlowRegistry) -> FlowRegistry:
    cloned = FlowRegistry()
    cloned.units.update(base.units)
    cloned.flows.update(base.flows)
    return cloned

def load_base_registry(repo_root: str | Path | None = None) -> FlowRegistry:
    """Load the current Reflexor repository units & sub-flows as the baseline registry."""
    reg = FlowRegistry()
    loader = UnitsLoader(local_repo_dir=repo_root)
    if loader.local_repo_dir:
        UnitsIndexer(loader).sync_into_registry(reg)
    return reg

def _parse_payload(source: str | Path | dict[str, Any]) -> dict[str, Any]:
    if isinstance(source, dict):
        return source
    if isinstance(source, Path):
        raw = source.read_text(encoding="utf-8")
    else:
        s = source.strip()
        if not s.startswith("{") and s.endswith(".json") and Path(s).is_file():
            raw = Path(s).read_text(encoding="utf-8")
        else:
            raw = s
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("FlowDSL payload root must be a JSON object.")
    return data

def _extract_entries(data: dict[str, Any]) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
    """Normalize standard `{"flows": ...}`, LLM `{"candidates": [...]}`, or single-flow dicts."""
    units_block = data.get("units") if isinstance(data.get("units"), dict) else {}
    flows: list[tuple[str, str, str]] = []

    if isinstance(f_map := data.get("flows"), dict):
        for f_name, f_meta in f_map.items():
            if is_flow_meta(f_meta):
                flows.extend(expand_entries(f_name, f_meta))
    elif isinstance(cands := data.get("candidates"), list):
        for i, item in enumerate(cands):
            if isinstance(item, dict) and item.get("flow"):
                name = str(item.get("name") or f"candidate_{i + 1}").strip()
                flows.append((name, str(item["flow"]).strip(), str(item.get("desc") or "")))
    elif isinstance(data.get("flow"), str):
        name = str(data.get("name") or data.get("flow_name") or "injected_flow").strip()
        flows.append((name, data["flow"].strip(), str(data.get("desc") or "")))
    else:
        for k, v in data.items():
            if k != "meta" and is_flow_meta(v):
                flows.extend(expand_entries(k, v))

    return units_block, flows

class FlowDSLHarness:
    """Stateless/reusable harness evaluating injected FlowDSL payloads against a base `FlowRegistry`."""

    def __init__(self, base_reg: FlowRegistry | None = None, *, repo_root: str | Path | None = None) -> None:
        self.base_reg = base_reg if base_reg is not None else load_base_registry(repo_root)
        self._fsm = FSMCompiler(use_wasm_linker=False)

    def verify_one(
        self,
        reg: FlowRegistry,
        flow_name: str,
        dsl: str,
        desc: str = "",
        *,
        available_auth: set[str] | None = None,
        connected_accounts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        errors: list[str] = []
        try:
            fc = parse_flow_dsl(flow_name, dsl, desc)
        except ValueError as exc:
            return {
                "name": flow_name, "ok": False, "stage": "grammar", "flow": dsl, "desc": desc,
                "steps": 0, "bindings": dsl.count("["), "states": 0,
                "errors": [f"Grammar error: {exc}"], "warnings": [], "open_slots": [], "auth": [],
            }

        work_reg = _clone_registry(reg)
        work_reg.flows[flow_name] = fc
        rep = inspect_flow(work_reg, flow_name)
        errors.extend(rep.errors)
        stage = "wiring"
        state_count = 0

        if not errors:
            stage = "asl"
            try:
                asl = compile_to_asl(flow_name, work_reg)
                if asl_errs := verify_asl(asl):
                    errors.extend(f"ASL error: {e}" for e in asl_errs)
                else:
                    stage = "fsm"
                    self._fsm.compile_from_asl(asl, trace_id=flow_name)
                    state_count = len(asl.get("States") or {})
                    stage = "ready"
            except Exception as exc:
                errors.append(f"{stage.upper()} compile error: {exc}")

        req_auth = sorted(flow_auth(work_reg, flow_name)) if stage in ("asl", "fsm", "ready") else sorted(rep.auth)
        out: dict[str, Any] = {
            "name": flow_name,
            "ok": not errors,
            "stage": stage,
            "flow": dsl,
            "desc": desc,
            "steps": len(fc.steps),
            "bindings": sum(len(s.bindings) for s in fc.steps),
            "states": state_count,
            "errors": errors,
            "warnings": list(rep.warnings),
            "open_slots": list(rep.open_slots),
            "auth": req_auth,
        }
        if available_auth is not None:
            out["missing_auth"] = sorted(missing_credentials(set(req_auth), available_auth))
        if connected_accounts is not None and not errors:
            selected, open_acc = resolve_accounts(work_reg, flow_name, connected_accounts)
            out["selected_accounts"] = selected
            out["open_account_slots"] = open_acc
        return out

    def evaluate(
        self,
        source: str | Path | dict[str, Any],
        *,
        target_flow: str | None = None,
        max_keep: int | None = None,
        available_auth: set[str] | None = None,
        connected_accounts: dict[str, Any] | None = None,
        strict: bool = False,
    ) -> dict[str, Any]:
        data = _parse_payload(source)
        units_block, flow_entries = _extract_entries(data)
        reg = _clone_registry(self.base_reg)
        unit_errors: list[str] = []

        for u_name, u_meta in units_block.items():
            if not is_flow_meta(u_meta):
                continue
            for key, spec, u_desc in expand_entries(u_name, u_meta):
                try:
                    u = reg.register_unit(key, spec, u_desc)
                    if u.scheme == "llm" and u.auth:
                        unit_errors.append(f"[BAD-UNIT] llm unit '{u.name}' must not declare auth.")
                except ValueError as exc:
                    unit_errors.append(f"[BAD-UNIT] '{key}': {exc}")

        # Pre-register valid sibling flows so intra-file sub-flow calls resolve cleanly
        for f_name, f_dsl, f_desc in flow_entries:
            try:
                reg.flows[f_name] = parse_flow_dsl(f_name, f_dsl, f_desc)
            except ValueError:
                pass

        if target_flow:
            flow_entries = [e for e in flow_entries if e[0] == target_flow]
            if not flow_entries:
                raise KeyError(f"Flow '{target_flow}' not found in injected payload.")

        reports = [
            self.verify_one(
                reg, name, dsl, desc,
                available_auth=available_auth,
                connected_accounts=connected_accounts,
            )
            for name, dsl, desc in flow_entries
        ]

        passed = [r for r in reports if r["ok"]]
        passed.sort(key=lambda r: (r["steps"], r["bindings"], r["states"]))
        finalists = passed[:max_keep] if max_keep is not None else passed
        rejected = [
            *unit_errors,
            *(f"[{r['stage'].upper()}] {r['name']}: {'; '.join(r['errors'])}" for r in reports if not r["ok"]),
        ]

        if strict and (unit_errors or len(passed) != len(reports) or not reports):
            raise ContractVerificationError("FlowDSL verification failed:\n  - " + "\n  - ".join(rejected or ["no flows found"]))

        return {
            "ok": bool(finalists) and not unit_errors and len(passed) == len(reports),
            "valid_count": len(finalists),
            "total_count": len(reports),
            "selected": finalists[0] if finalists else None,
            "finalists": finalists,
            "reports": {r["name"]: r for r in reports},
            "rejected_notes": rejected,
        }


def verify_injected_flows(source: str | Path | dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Convenience entrypoint for one-shot verification of a JSON string, file path, or dict."""
    repo_root = kwargs.pop("repo_root", None)
    return FlowDSLHarness(repo_root=repo_root).evaluate(source, **kwargs)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m fiber.studio.harness.contract.flowdsl")
    p.add_argument("source", nargs="?", default="-", help="Flow JSON file path, inline JSON string, or '-' for stdin")
    p.add_argument("--flow", default="", help="Verify a single named flow within the payload")
    p.add_argument("--root", default="", help="Reflexor repository root override")
    p.add_argument("--keep", type=int, default=0, help="Keep top N minimal valid candidates (0 = all)")
    p.add_argument("--strict", action="store_true", help="Exit non-zero if any flow fails verification")
    args = p.parse_args(argv)

    raw_src: str | Path = sys.stdin.read() if args.source == "-" else (
        Path(args.source) if Path(args.source).is_file() else args.source
    )
    try:
        res = FlowDSLHarness(repo_root=args.root or None).evaluate(
            raw_src,
            target_flow=args.flow or None,
            max_keep=args.keep or None,
            strict=args.strict,
        )
        sys.stdout.write(json.dumps(res, indent=2, ensure_ascii=False) + "\n")
        return 0 if (res["valid_count"] > 0 and (not args.strict or res["ok"])) else 1
    except (ContractVerificationError, ValueError, KeyError, OSError) as exc:
        print(f"[HARNESS:ERR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())