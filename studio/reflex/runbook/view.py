# fiber.studio.reflex.runbook.view
from __future__ import annotations

import json
from typing import Any

from xphi.arch.contract.registry.flow import FlowRegistry
from xphi.arch.reflex.flow import FlowContract, Shape

from fiber.studio.reflex.flow.catalog import FlowCatalog
from fiber.node.reflex.runtime.projector import TelemetryEvent
from fiber.node.reflex.runbook.spec import PinnedRelease, UNBOUND_TOKEN
from fiber.node.reflex.runbook.inspector import emit_flow_dsl, inspect_flow


def short_val(value: Any, limit: int = 80) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def strip_ui_prefix(ui_ref: str) -> str:
    """Translates UI source ('02.items', '$input.quote_file') to flat context key ('items', 'quote_file')."""
    if not ui_ref:
        return ""
    if ui_ref.startswith("$input."):
        return ui_ref.removeprefix("$input.")
    head, sep, tail = ui_ref.partition(".")
    return tail if (sep and head.isdigit()) else ui_ref


def build_draft_maps(
    reg: FlowRegistry,
    flow: FlowContract,
    version: int,
    input_modes: dict[str, str],
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Builds UI `draft` dict, per-step `available_keys`, and output-key collision warnings."""
    producers: dict[str, str] = {
        k: f"$input.{k}" for k in (*flow.in_reqs, *flow.in_defaults, *flow.in_optional)
    }
    avail_by_no: dict[str, list[str]] = {}
    nodes: list[dict[str, Any]] = []
    shadow_warnings: list[str] = []

    for idx, step in enumerate(flow.steps, 1):
        no = f"{idx:02d}"
        avail_by_no[no] = list(producers.values())

        if step.gate_expr is not None:
            snap_in = {k: producers[k] for k in producers if k in step.gate_expr}
            nodes.append({
                "no": no, "name": f"gate({step.name})", "step_name": step.name, "kind": "gate",
                "in": snap_in, "want": dict.fromkeys(snap_in, "bool"), "cfg": {}, "out": [],
            })
            continue

        c = reg.get(step.name) if (step.name in reg.units or step.name in reg.flows) else None
        if c is None:
            nodes.append({
                "no": no, "name": step.name, "step_name": step.name, "kind": "unknown",
                "in": dict(step.bindings), "want": {}, "cfg": flow.cfg_for(step.name), "out": [],
            })
            continue

        step_avail = dict(producers)
        if step.is_each and step.each_item:
            step_avail[step.each_item] = step.each_item
            avail_by_no[no].append(step.each_item)

        in_dict: dict[str, str] = {}
        want_dict: dict[str, str] = {}
        for k in (*c.in_reqs, *c.in_defaults, *c.in_optional):
            raw_src = step.source_of(k)
            if raw_src in ("", UNBOUND_TOKEN):
                in_dict[k] = ""
            elif raw_src in step_avail:
                in_dict[k] = step_avail[raw_src]
            else:
                in_dict[k] = ""
            want_dict[k] = str(c.in_shapes.get(k, Shape()))

        is_unit = hasattr(c, "scheme")
        cfg_merged = {**(dict(c.cfg_defaults) if is_unit else {}), **flow.cfg_for(step.name)}
        if step.remap:
            inv_remap = {old_k: new_k for new_k, old_k in step.remap.items()}
            eff_outs = [inv_remap.get(k, k) for k in c.out_keys]
        else:
            eff_outs = list(c.out_keys)

        nodes.append({
            "no": no, "name": step.name, "step_name": step.name,
            "kind": c.scheme if is_unit else "flow",
            "in": in_dict, "want": want_dict, "cfg": cfg_merged, "out": eff_outs,
        })
        for out_k in eff_outs:
            if out_k in producers:
                shadow_warnings.append(
                    f"{no} {step.name}: output '{out_k}' overwrites '{producers[out_k]}' in flat context (use '=>' remap)"
                )
            producers[out_k] = f"{no}.{out_k}"

    inputs = {
        k: {
            "shape": str(flow.in_shapes.get(k, Shape())),
            "mode": input_modes.get(k, "fixed" if k in flow.in_defaults else "runtime"),
            **({"value": flow.in_defaults[k]} if k in flow.in_defaults else {}),
        }
        for k in (*flow.in_reqs, *flow.in_defaults, *flow.in_optional)
    }
    outputs = {k: producers.get(k, k) for k in flow.out_keys}
    draft_dict = {
        "flow": flow.name, "version": version, "nodes": nodes,
        "inputs": inputs, "outputs": outputs, "warnings": shadow_warnings,
    }
    return draft_dict, avail_by_no


def compile_diagnostics(reg: FlowRegistry, flow_name: str, draft_dict: dict[str, Any]) -> dict[str, Any]:
    if not flow_name or flow_name not in reg.flows:
        return {"ok": False, "unbound": [], "mismatch": [], "warnings": [], "errors": ["No active draft flow."]}

    rep = inspect_flow(reg, flow_name)
    unbound: list[str] = []
    mismatch: list[str] = []
    no_by_step = {n["step_name"]: n["no"] for n in draft_dict["nodes"]}

    for err in rep.errors:
        no = next((num for sname, num in no_by_step.items() if f":{sname}:" in err), "00")
        target = mismatch if ("->" in err or "incompatible" in err) else unbound
        target.append(f"{no}:{err}")

    fc = reg.flows[flow_name]
    for idx, n in enumerate(draft_dict["nodes"]):
        if n["kind"] == "gate" or idx >= len(fc.steps):
            continue
        step = fc.steps[idx]
        c = reg.get(step.name) if (step.name in reg.units or step.name in reg.flows) else None
        req_keys = (set(c.in_reqs) - set(c.in_defaults)) if c is not None else set()
        for k, v in n["in"].items():
            explicitly_cleared = step.bindings.get(k) in ("", UNBOUND_TOKEN)
            if v == "" and (k in req_keys or explicitly_cleared):
                already = any(u.startswith(f"{n['no']}:") and f"'{k}'" in u for u in unbound)
                if not already:
                    unbound.append(f"{n['no']}.{k}")

    warnings = [*list(rep.warnings), *list(draft_dict.get("warnings") or ())]
    return {
        "ok": not (rep.errors or unbound or mismatch),
        "unbound": unbound,
        "mismatch": mismatch,
        "warnings": warnings,
        "errors": rep.errors,
    }


def step_status(node: dict[str, Any], comp: dict[str, Any]) -> str:
    prefixes = (f"{node['no']}.", f"{node['no']}:")
    if any(i.startswith(prefixes) for i in comp["unbound"]):
        return "fail"
    if any(i.startswith(prefixes) for i in comp["mismatch"]):
        return "wait"
    return "ok"


def trace_to_events(
    ui_nodes: list[dict[str, Any]],
    trace: list[TelemetryEvent],
    summaries: dict[str, str],
    sources: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    no_by_name = {n["step_name"]: n["no"] for n in ui_nodes}
    status_map = {"step_ok": "ok", "dry_run_skip": "dry", "hitl_parked": "wait", "fail_caught": "fail", "halted": "fail"}
    events: list[dict[str, Any]] = []
    prev_ts: float | None = None
    for ev in trace:
        no = no_by_name.get(ev.state, "")
        dur = f"{max(0.0, ev.timestamp - prev_ts):.1f}s" if prev_ts is not None else ""
        prev_ts = ev.timestamp
        req_src = (sources or {}).get(no, "LIVE").upper()
        if ev.event == "dry_run_skip":
            tag = req_src if req_src in ("FIXTURE", "REPLAY") else "DRY-SKIP"
        elif ev.event == "gate_eval":
            tag = "GATE"
        else:
            tag = "LIVE"
        events.append({
            "node": f"{no} {ev.state}".strip(),
            "status": status_map.get(ev.event, "ok"),
            "tag": tag,
            "dur": dur,
            "note": ev.error or summaries.get(ev.state, ""),
        })
    return events


def render_template_model(name: str, tpl: tuple[str, str, str] | None, repo: FlowCatalog) -> dict[str, Any]:
    if tpl is None:
        return {
            "flow": name, "stage": "template", "title": f"{name} · template",
            "blocks": [{"kind": "callout", "tone": "fail", "text": f"Template '{name}' not found in catalog or registry."}],
        }
    _, spec, desc = tpl
    from_repo = repo.enabled and repo.flow(name) is not None
    src_badge = f"TEMPLATE · reflexor repo ({repo.commit[:8] or 'index'})" if from_repo else "BUILT-IN SAMPLE · local studio registry"
    blocks: list[dict[str, Any]] = [
        {"kind": "callout", "tone": "ok" if from_repo else "dry", "text": src_badge},
        *([{"kind": "paragraph", "text": desc}] if desc else []),
        {"kind": "code", "title": "flow_dsl", "lang": "reflex", "text": spec},
    ]
    if repo.notes:
        blocks.append({"kind": "table", "title": "catalog notes", "headers": ["note"], "rows": [[n] for n in repo.notes]})
    if seeds := repo.samples(name):
        blocks.append({
            "kind": "table", "title": "seed samples", "headers": ["sample", "key", "value"],
            "rows": [[s, k, short_val(v)] for s, kv in seeds.items() for k, v in kv.items()],
        })
    return {"flow": name, "version": None, "stage": "template", "title": f"{name} · template", "blocks": blocks}


def render_draft_model(
    draft_dict: dict[str, Any],
    comp: dict[str, Any],
    *,
    dirty: bool,
    origin_tpl: tuple[str, str, str] | None = None,
) -> dict[str, Any]:
    warns = comp.get("warnings") or []
    warn_suffix = f" · {len(warns)} warning(s)" if warns else ""
    compile_text = (
        f"COMPILE OK · {len(draft_dict['nodes'])} steps verified{warn_suffix}"
        if comp["ok"]
        else f"COMPILE FAIL · {len(comp['unbound'])} unbound/err · {len(comp['mismatch'])} type mismatch"
    )
    steps = [
        {
            "label": f"{n['no']} {n['name']}",
            "detail": " · ".join(f"{k} <- {v or '(unbound)'}" for k, v in n["in"].items()) or n["kind"],
            "status": step_status(n, comp),
        }
        for n in draft_dict["nodes"]
    ]
    origin_blocks = (
        [*([{"kind": "paragraph", "text": origin_tpl[2]}] if origin_tpl[2] else []),
         {"kind": "code", "title": "origin template", "lang": "reflex", "text": origin_tpl[1]}]
        if origin_tpl else []
    )
    blocks: list[dict[str, Any]] = [
        {"kind": "callout", "tone": ("dry" if warns else "ok") if comp["ok"] else "fail", "text": compile_text},
        *origin_blocks,
        {"kind": "flow", "title": "draft wiring", "steps": steps},
        {"kind": "table", "title": "$input", "headers": ["key", "shape", "mode"],
         "rows": [[k, v["shape"], v["mode"]] for k, v in draft_dict["inputs"].items()]},
        {"kind": "table", "title": "$out", "headers": ["key", "from", "show"],
         "rows": [[k, v, "show"] for k, v in draft_dict["outputs"].items()]},
    ]
    if warns:
        blocks.append({"kind": "table", "title": "warnings", "headers": ["warning"], "rows": [[w] for w in warns]})
    if comp["errors"]:
        blocks.append({"kind": "table", "title": "diagnostics", "headers": ["error"], "rows": [[e] for e in comp["errors"]]})
    return {
        "flow": draft_dict["flow"], "version": draft_dict["version"], "stage": "draft",
        "title": f"{draft_dict['flow']} · Draft v{draft_dict['version']}" + (" [*]" if dirty else ""),
        "blocks": blocks,
    }


def render_pinned_model(
    rel: PinnedRelease,
    fc: FlowContract,
    *,
    stale: bool,
    published: bool,
    last_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    steps = [
        {
            "label": f"{idx:02d} {s.name if s.gate_expr is None else f'gate({s.name})'}",
            "detail": s.gate_expr or ", ".join(f"{k}={v}" for k, v in s.bindings.items()) or "direct",
            "status": "ok",
        }
        for idx, s in enumerate(fc.steps, 1)
    ]
    trig_state = "LIVE TRIGGER ON" if published else "TRIGGER OFF (pinned snapshot)"
    status_note = "[RECOMPILE] unit SHA changed" if stale else trig_state
    blocks: list[dict[str, Any]] = [
        {"kind": "callout", "tone": "dry" if (stale or not published) else "ok",
         "text": f"PINNED v{rel.version} · {status_note}"},
        {"kind": "code", "title": "flow_dsl", "lang": "reflex", "text": rel.flow_dsl or emit_flow_dsl(fc)},
        {"kind": "flow", "title": "compiled pipeline", "steps": steps},
        {"kind": "table", "title": "$input", "headers": ["key", "shape", "mode"],
         "rows": [[k, str(fc.in_shapes.get(k, Shape())), "fixed" if k in fc.in_defaults else "runtime"]
                  for k in (*fc.in_reqs, *fc.in_defaults, *fc.in_optional)]},
        {"kind": "table", "title": "$out", "headers": ["key", "widget"],
         "rows": [[k, pol.widget] for k, pol in rel.manifest.outputs.items()]},
    ]
    if last_events:
        blocks.append({"kind": "timeline", "title": "latest run trace", "events": last_events})
    return {
        "flow": rel.flow_name, "version": rel.version,
        "stage": "run" if published else "pinned",
        "title": f"{rel.flow_name} · v{rel.version}", "blocks": blocks,
    }


def render_run_model(flow: str, ver: int, runs: list[dict[str, Any]], run_no: int | None = None) -> dict[str, Any]:
    run = next((r for r in runs if r["no"] == run_no), runs[0] if runs else None)
    blocks: list[dict[str, Any]] = []
    if run is None:
        blocks.append({"kind": "callout", "tone": "none", "text": "No runs recorded yet. Press [▶ RUN NOW] in [3] Trigger."})
    else:
        tone = {"paused": "dry", "dry": "dry", "ok": "ok", "fail": "fail"}.get(run["status"], "run")
        prefix = "DRY-RUN PREVIEW (trigger OFF)" if run["status"] == "dry" else f"LIVE {run['status'].upper()}"
        blocks.append({"kind": "callout", "tone": tone, "text": f"{prefix} · {run['note']}"})
        if run.get("events"):
            blocks.append({"kind": "timeline", "title": f"run #{run['no']} trace", "events": run["events"]})
    blocks.append({
        "kind": "table", "title": "run history", "headers": ["#", "at", "by", "status", "note"],
        "rows": [[f"#{r['no']}", r["at"], r["by"], r["status"].upper(), r["note"]] for r in runs],
    })
    title = f"{flow} · v{ver}" + (f" · Run #{run['no']} · {run['by']} · {run['at']}" if run else "")
    return {"flow": flow, "version": ver, "stage": "run", "title": title, "blocks": blocks}