# fiber.studio.base.reflex.compass
"""System architecture & runtime invariant checker (managed by `reflexor.shell.flow`)."""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import inspect
import io
import json
from pathlib import Path
import time
from typing import Any

from xphi.node.runtime.reflex.engine import FlowEngine
from fiber.node.reflex.unit.indexer import UnitsIndexer
from fiber.node.reflex.workspace.inspector import inspect_flow
from fiber.node.reflex.workspace.viewport import WorkflowViewport
from fiber.studio.base.reflex.arch import (
    classify_module_layer,
    resolve_workspace_root,
    scan_reflex_architecture,
)
from xphi.arch.contract.reflex.flow import CompiledTask
from xphi.arch.contract.registry.flow import FlowDSL

FROZEN_CONTRACT_MODULES: tuple[str, ...] = (
    "xphi.arch.contract.reflex.fsm",
    "xphi.arch.xor.fsm.compiler",
    "xphi.arch.xor.fsm.parser",
)


def _module_exists(mod_path: str) -> bool:
    top_pkg = mod_path.split(".")[0]
    if importlib.util.find_spec(top_pkg) is None:
        return False
    try:
        return importlib.util.find_spec(mod_path) is not None
    except (ModuleNotFoundError, ValueError):
        return False


def check_architecture_invariants(
    ws_root: Path,
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str]]:
    modules = scan_reflex_architecture(ws_root)
    violations: list[dict[str, str]] = []
    stale_refs: list[dict[str, str]] = []
    imported_targets: set[str] = set()

    for m in modules:
        src_rank = int(m.layer[1]) if m.layer.startswith("L") and m.layer[1].isdigit() else 99

        for imp in m.reflex_imports:
            imp_mod = imp.split(" (")[0].strip()
            imported_targets.add(imp_mod)
            dst_rank = classify_module_layer(imp_mod)
            if dst_rank > src_rank:
                violations.append({
                    "source": m.mod_name,
                    "source_layer": f"L{src_rank}",
                    "target": imp_mod,
                    "target_layer": f"L{dst_rank}",
                })

        for pt in m.patch_targets:
            target_mod = pt.rsplit(".", 1)[0]
            if not _module_exists(target_mod):
                stale_refs.append({
                    "file": m.rel_path,
                    "symbol": pt,
                    "missing_module": target_mod,
                })

    unwired_entrypoints = [
        m.mod_name for m in modules
        if m.layer.startswith("L") and m.layer[1].isdigit() and int(m.layer[1]) in (2, 3, 4)
        and m.mod_name not in imported_targets
        and not m.mod_name.endswith(("runner", "extract"))
    ]
    return violations, stale_refs, unwired_entrypoints


def analyze_catalog_and_flows(vp: WorkflowViewport) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    reg = vp.registry
    scheme_counts: dict[str, int] = {"py": 0, "exec": 0, "mcp": 0, "llm": 0, "flow": 0}
    for u in reg.units.values():
        scheme_counts[u.scheme] = scheme_counts.get(u.scheme, 0) + 1

    exercised_targets: set[str] = set()
    flow_reports: list[dict[str, Any]] = []

    for fname in reg.flows:
        ins = inspect_flow(reg, fname)
        snap = vp.project(fname, inspection=ins)
        nodes: dict[str, Any] = {}

        # 정적 검사 에러가 없는 플로우만 ASL 컴파일 수행 (예외 중단 방지)
        if not ins.errors:
            try:
                asl = FlowDSL.compile_to_asl(fname, reg)
                _, nodes = FlowEngine.compile_asl_nodes(asl, validate=False)
            except Exception as exc:
                ins.errors.append(str(exc))

        for node in nodes.values():
            if isinstance(node, CompiledTask):
                exercised_targets.add(node.target)
                exercised_targets.add(node.name)
                if node.scheme == "flow" or node.target in reg.flows:
                    scheme_counts["flow"] += 1
                if node.catch_state:
                    exercised_targets.add(node.catch_state)

        flow_reports.append({
            "flow": fname,
            "valid": len(ins.errors) == 0,
            "nodes": len(nodes),
            "edges": len(getattr(snap, "edges", ()) or ()),
            "missing_auth": list(snap.missing_auth),
            "open_slots": list(snap.open_account_slots),
            "errors": list(ins.errors),
        })

    unreferenced_units = sorted(set(reg.units.keys()) - exercised_targets)
    coverage = {
        "schemes": scheme_counts,
        "total_units": len(reg.units),
        "exercised_units": len(reg.units) - len(unreferenced_units),
        "unreferenced_units": unreferenced_units,
    }
    return coverage, flow_reports


def run_verification_suites() -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    try:
        from theoria.reflex.tester.fsm import verify as fsm_verify
    except Exception as exc:
        return [{"suite": "tester.fsm.verify", "status": "SKIP", "elapsed_ms": 0, "error": str(exc)}]

    suite_names = (
        "verify_suite_1_contracts",
        "verify_suite_2_runtime_primitives",
        "verify_suite_3_polyglot_bridge",
        "verify_suite_4_heterogeneous_dag",
    )
    for name in suite_names:
        fn = getattr(fsm_verify, name, None)
        if not callable(fn):
            continue
        buf = io.StringIO()
        t0 = time.perf_counter()
        try:
            with contextlib.redirect_stdout(buf):
                if inspect.iscoroutinefunction(fn):
                    asyncio.run(fn())
                else:
                    res = fn()
                    if inspect.iscoroutine(res):
                        asyncio.run(res)
            ms = (time.perf_counter() - t0) * 1000.0
            results.append({"suite": name, "status": "PASS", "elapsed_ms": round(ms, 1)})
        except Exception as exc:
            ms = (time.perf_counter() - t0) * 1000.0
            results.append({
                "suite": name,
                "status": "FAIL",
                "elapsed_ms": round(ms, 1),
                "error": f"{type(exc).__name__}: {exc}",
            })
    return results


def _load_viewport_from_index(units_root: Path) -> tuple[WorkflowViewport, list[str]]:
    vp = WorkflowViewport(auto_sync_catalog=False)
    load_errs: list[str] = []
    idx_path = units_root / UnitsIndexer.INDEX_FILENAME
    if not idx_path.is_file():
        return vp, [f"Missing {idx_path}"]

    data = json.loads(idx_path.read_text(encoding="utf-8"))
    for uname, umeta in data.get("units", {}).items():
        try:
            vp.synthesize_unit(uname, umeta["flow"], desc=umeta.get("desc", ""), commit=True)
        except Exception as exc:
            load_errs.append(f"unit '{uname}': {exc}")
    for fname, fmeta in data.get("flows", {}).items():
        try:
            vp.registry.register_flow(fname, fmeta["flow"], fmeta.get("desc", ""))
        except Exception as exc:
            load_errs.append(f"flow '{fname}': {exc}")
    return vp, load_errs


def run_compass(units_root: Path, *, run_verify: bool = False) -> tuple[bool, str]:
    """Returns `(is_aligned, formatted_report)`."""
    ws_root = resolve_workspace_root()
    vp, load_errs = _load_viewport_from_index(units_root)

    violations, stale_refs, unwired = check_architecture_invariants(ws_root)
    coverage, flow_reports = analyze_catalog_and_flows(vp)
    suite_results = run_verification_suites() if run_verify else []

    lines = [
        "==========================================================================",
        " [REFLEX META-COMPASS] System Alignment & Verification Anchor",
        "==========================================================================",
        f" Workspace   : {ws_root}",
        f" Units Index : {units_root / UnitsIndexer.INDEX_FILENAME}",
        "\n[1] Layer Invariants (L1 Contract -> L2 WASM/Watcher -> L3 Bridge -> L4 Node -> L5 Ops)",
    ]
    if violations:
        for v in violations:
            lines.append(f"  ✗ LAYER_VIOLATION : {v['source']} ({v['source_layer']}) -> {v['target']} ({v['target_layer']})")
    else:
        lines.append("  ✓ LAYER_ORDER     : Clean downward dependency hierarchy (L1 -> L5).")

    if stale_refs:
        for sr in stale_refs:
            lines.append(f"  ✗ STALE_REFERENCE : {sr['file']} references non-existent '{sr['symbol']}'")
    else:
        lines.append("  ✓ SYMBOL_BINDING  : All static imports and mock.patch targets resolve cleanly.")

    if unwired:
        lines.append(f"  ! UNWIRED_MODULES : {', '.join(unwired)}")

    lines.append("\n[2] Catalog & ASL Execution Coverage")
    sc_str = " | ".join(f"{k}={v}" for k, v in sorted(coverage["schemes"].items()))
    lines.append(f"  • Schemes         : {sc_str}")
    lines.append(f"  • Units           : {coverage['exercised_units']}/{coverage['total_units']} exercised across compiled ASL graphs")
    for le in load_errs:
        lines.append(f"  ✗ CATALOG_LOAD_ERR: {le}")

    lines.append("\n[3] Flow Topology & Auth Readiness")
    for f in flow_reports:
        badge = "✓ OK " if f["valid"] else "✗ ERR"
        auth = f"missing:{f['missing_auth']}" if f["missing_auth"] else "complete"
        lines.append(f"  {badge} {f['flow']:<28} | nodes={f['nodes']:<2} edges={f['edges']:<2} | auth={auth}")

    suite_failed = False
    if run_verify:
        lines.append("\n[4] Verification Suites (Sync & Async Coroutines)")
        for s in suite_results:
            icon = "✓ PASS" if s["status"] == "PASS" else ("~ SKIP" if s["status"] == "SKIP" else "✗ FAIL")
            lines.append(f"  {icon} {s['suite']:<34} ({s['elapsed_ms']} ms)")
            if s["status"] == "FAIL":
                suite_failed = True
                lines.append(f"         -> {s.get('error')}")

    action_items: list[dict[str, Any]] = []
    for sr in stale_refs:
        action_items.append({
            "id": "FIX_STALE_PATCH_TARGET",
            "priority": "CRITICAL",
            "file": sr["file"],
            "broken_target": sr["symbol"],
        })
    for v in violations:
        action_items.append({
            "id": "RESOLVE_UPWARD_IMPORT",
            "priority": "HIGH",
            "file": v["source"].replace(".", "/") + ".py",
            "illegal_import": v["target"],
        })
    if unwired:
        action_items.append({
            "id": "REVIEW_UNWIRED_ENTRYPOINTS",
            "priority": "LOW",
            "modules": unwired,
        })

    ok = not (violations or stale_refs or load_errs or suite_failed)
    llm_spec = {
        "status": "ALIGNED" if ok else "NEEDS_ALIGNMENT",
        "frozen_core_guard": list(FROZEN_CONTRACT_MODULES),
        "metrics": {
            "layer_violations": len(violations),
            "stale_references": len(stale_refs),
            "unwired_modules": len(unwired),
            "unit_coverage": f"{coverage['exercised_units']}/{coverage['total_units']}",
            "suites_passed": (
                f"{sum(1 for s in suite_results if s['status'] == 'PASS')}/{len(suite_results)}"
                if run_verify else "not_run"
            ),
        },
        "next_actions": action_items,
    }
    lines.append("\n--- [LLM_ALIGNMENT_SPEC] ---")
    lines.append(json.dumps(llm_spec, indent=2, ensure_ascii=False))
    return ok, "\n".join(lines)