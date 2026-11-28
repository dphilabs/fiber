# fiber.studio.console.runbook.bridge
from __future__ import annotations

from dataclasses import replace
import hashlib
import inspect
import json
from pathlib import Path
import time
from typing import Any

from xphi.arch.reflex.flow import FlowContract, Shape, split_unit_ref
from xphi.node.space.bind.resolver import resolve_path

from fiber.studio.console.runbook.ops import LiveTriggerOps, atomic_write_json, load_json
from fiber.studio.console.runbook.view import (
     build_draft_maps,
    compile_diagnostics,
    render_draft_model,
    render_pinned_model,
    render_run_model,
    render_template_model,
    short_val,
    step_status,
    strip_ui_prefix,
    trace_to_events,
)
from fiber.studio.harness.reflex.context import FlowCatalog
from fiber.studio.runbook.runtime.viewport import ViewportRuntime
from fiber.studio.runbook.spec import (
    PinnedRelease,
    UNBOUND_TOKEN,
    coerce_value_for_shape,
    summarize_value_brief,
)
from fiber.studio.runbook.stepper import emit_flow_dsl
from fiber.studio.runbook.store import ReleaseStudio

class RunbookBridge:
    """Console Runbook Facade coordinating `FlowCatalog`, `ReleaseStudio` (Draft/Pin), and `LiveTriggerOps`."""

    def __init__(self, root_dir: Path | None = None) -> None:
        base = (root_dir or (resolve_path("anchor") / "runbook")).resolve()
        base.mkdir(parents=True, exist_ok=True)
        self._draft_file = base / "draft_work.json"

        self.repo = FlowCatalog.from_env()
        self.ops = LiveTriggerOps(base, self.repo)

        draft_vp = ViewportRuntime(
            registry=self.ops.live.session._clone_registry(),
            loader=self.ops.live.session.loader,
            available_credentials=self.ops.live.session.available_credentials,
            connected_accounts=self.ops.live.session.connected_accounts,
        )
        self.studio = ReleaseStudio(base / "studio", viewport=draft_vp)
        self.ops.sync_releases(self.studio.list_active())

        self.origin: str | None = None
        self._draft_flow: str = ""
        self._draft_ver: int = 1
        self._input_modes: dict[str, str] = {}
        self._init_draft_cursor()
        self._saved_digest: str = self._draft_digest()

    def _init_draft_cursor(self) -> None:
        saved = load_json(self._draft_file, None)
        if isinstance(saved, dict) and saved.get("flow") and saved.get("flow_dsl"):
            fname = str(saved["flow"])
            self.studio.viewport.synthesize(fname, str(saved["flow_dsl"]), desc=str(saved.get("desc", "")), commit=True)
            self._draft_flow, self._draft_ver = fname, int(saved.get("version", 1))
            self.origin = saved.get("origin")
            self._input_modes = dict(saved.get("input_modes") or {})
            return
        if active := self.studio.list_active():
            top = active[0]
            self._draft_flow, self._draft_ver = top.flow_name, top.version
            self.studio.viewport.synthesize(top.flow_name, top.flow_dsl, commit=True)
            return
        if self.repo.enabled and (tpls := self.repo.flows()):
            name, dsl, desc = tpls[0]
            self.origin, self._draft_flow, self._draft_ver = name, name, 1
            self.studio.viewport.synthesize(name, dsl, desc=desc, commit=True)
            return
        if flows := list(self.studio.viewport.registry.flows):
            self._draft_flow, self._draft_ver = flows[0], 1

    def _draft_contract(self) -> FlowContract | None:
        return self.studio.viewport.registry.flows.get(self._draft_flow)

    def _draft_digest(self) -> str:
        fc = self._draft_contract()
        if fc is None:
            return ""
        raw = json.dumps(
            {"flow": self._draft_flow, "dsl": emit_flow_dsl(fc), "cfg": fc.cfg_values, "modes": self._input_modes},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @property
    def dirty(self) -> bool:
        return self._draft_digest() != self._saved_digest

    @property
    def triggers(self) -> dict[str, dict[str, Any]]:
        return self.ops.triggers

    @property
    def draft(self) -> dict[str, Any]:
        fc = self._draft_contract()
        if fc is None:
            return {"flow": "", "version": 1, "nodes": [], "inputs": {}, "outputs": {}}
        d, _ = build_draft_maps(self.studio.viewport.registry, fc, self._draft_ver, self._input_modes)
        return d

    # --- 1. Queries & Viewer Models --------------------------------------------

    def list_versions(self, flow: str) -> list[int]:
        return self.studio.list_versions(flow)

    def list_active(self) -> list[dict[str, Any]]:
        releases = self.studio.list_active()
        stale_set = set(self.studio.scan_staleness(releases))
        self.ops.sync_releases(releases)
        rows: list[dict[str, Any]] = []
        for rel in releases:
            fname = rel.flow_name
            last = (self.ops.list_runs(fname) or [None])[0]
            rows.append({
                "flow": fname, "version": rel.version,
                "trigger": self.triggers.get(fname, {"kind": "manual"})["kind"],
                "status": last["status"] if last else "none",
                "note": last["note"] if last else "",
                "stale": fname in stale_set,
            })
        return rows

    def list_catalog(self) -> list[dict[str, Any]]:
        repo_map = (
            {n: desc for n, _, desc in self.repo.flows()}
            if self.repo.enabled
            else {n: f.desc for n, f in self.studio.viewport.registry.flows.items()}
        )
        active_map = {r["flow"]: r for r in self.list_active()}
        return [
            {
                "flow": f, "desc": repo_map.get(f, ""), "src": (f in repo_map) or not self.repo.enabled,
                "seeds": len(self.repo.samples(f)), "fixture": bool(self.repo.mock_outputs(f)),
                "pin": active_map[f]["version"] if f in active_map else None,
                "trigger": active_map[f]["trigger"] if f in active_map else "",
                "status": active_map[f]["status"] if f in active_map else "",
                "note": active_map[f]["note"] if f in active_map else "",
            }
            for f in sorted(set(repo_map) | set(active_map))
        ]

    def list_templates(self) -> list[dict[str, Any]]:
        if self.repo.enabled and (tpls := self.repo.flows()):
            return [{"name": n, "desc": desc} for n, _, desc in tpls]
        return [{"name": n, "desc": f.desc} for n, f in sorted(self.studio.viewport.registry.flows.items())]

    def list_samples(self, flow: str) -> list[str]:
        return list(self.repo.samples(flow)) or ["default_seed"]

    def repo_status(self) -> tuple[str, str]:
        if not self.repo.enabled:
            return ("[LOCAL · REPO ERR]", "red") if self.repo.notes else ("[LOCAL STUDIO]", "cyan")
        n, err = len(self.repo.flows()), len(self.repo.notes)
        return (f"[REPO {n} · {err} ERR]", "red") if err else (f"[REPO {n}]", "green")

    def available_keys(self, step_no: str) -> list[str]:
        fc = self._draft_contract()
        if fc is None:
            return []
        _, avail = build_draft_maps(self.studio.viewport.registry, fc, self._draft_ver, self._input_modes)
        return avail.get(step_no, [])

    def template_model(self, name: str) -> dict[str, Any]:
        tpl = self.repo.flow(name)
        if tpl is None and name in self.studio.viewport.registry.flows:
            fc = self.studio.viewport.registry.flows[name]
            tpl = (name, emit_flow_dsl(fc), fc.desc)
        return render_template_model(name, tpl, self.repo)

    def project(self, flow: str, version: int | None = None) -> dict[str, Any]:
        pinned_vers = self.list_versions(flow)
        is_draft = (flow == self._draft_flow) and (version is None or (version == self._draft_ver and version not in pinned_vers))
        if not is_draft and pinned_vers:
            rel = self.studio.load_active(flow, version)
            shadow = self.studio.viewport._clone_registry()
            shadow.register_flow(rel.flow_name, rel.flow_dsl)
            runs = self.ops.list_runs(flow)
            return render_pinned_model(
                rel, shadow.flows[rel.flow_name],
                stale=not self.studio.verify_pin_shas(rel),
                published=flow in self.ops.live.session.published_flows,
                last_events=runs[0].get("events") if runs else None,
            )
        d = self.draft
        comp = compile_diagnostics(self.studio.viewport.registry, self._draft_flow, d)
        orig = self.repo.flow(self.origin) if (self.origin and self.origin == flow) else None
        return render_draft_model(d, comp, dirty=self.dirty, origin_tpl=orig)

    def run_model(self, flow: str, *, version: int | None = None, run_no: int | None = None) -> dict[str, Any]:
        vers = self.list_versions(flow)
        ver = version or (vers[-1] if vers else 1)
        return render_run_model(flow, ver, self.ops.list_runs(flow), run_no=run_no)

    def new_draft(self, *, from_template: str | None = None, from_version: int | None = None) -> dict[str, Any]:
        if from_template is not None:
            if tpl := self.repo.flow(from_template):
                _, dsl, desc = tpl
                self.origin = from_template
            elif from_template in self.studio.viewport.registry.flows:
                fc = self.studio.viewport.registry.flows[from_template]
                dsl, desc, self.origin = emit_flow_dsl(fc), fc.desc, None
            else:
                raise KeyError(f"Template '{from_template}' not found.")
            self._draft_flow, self._draft_ver = from_template, max(self.list_versions(from_template), default=0) + 1
            self._input_modes.clear()
            self.studio.viewport.synthesize(from_template, dsl, desc=desc, commit=True)
        elif from_version is not None:
            rel = self.studio.load_active(self._draft_flow, from_version)
            self.origin, self._draft_ver = None, max(self.list_versions(self._draft_flow), default=from_version) + 1
            self._input_modes.clear()
            self.studio.viewport.synthesize(rel.flow_name, rel.flow_dsl, commit=True)
        return self.project(self._draft_flow)

    def save_draft(self) -> None:
        fc = self._draft_contract()
        if fc is None:
            return
        atomic_write_json(self._draft_file, {
            "flow": self._draft_flow, "version": self._draft_ver, "origin": self.origin,
            "flow_dsl": emit_flow_dsl(fc), "desc": fc.desc, "input_modes": self._input_modes, "saved_at": time.time(),
        })
        self._saved_digest = self._draft_digest()

    def import_package(self, raw: str) -> dict[str, Any]:
        data = json.loads(raw)
        fname = str(data.get("flow_name") or data.get("flow") or "imported_runbook")
        dsl = str(data.get("flow_dsl") or "")
        rep = self.studio.viewport.synthesize(fname, dsl, desc=str(data.get("desc", "")), commit=True) if dsl else None
        if isinstance(data.get("manifest"), dict):
            self.studio.configure_manifest(fname, data["manifest"])
        local_shas = self.studio._collect_unit_shas(fname)
        stale = sum(1 for u, s in dict(data.get("unit_shas") or {}).items() if local_shas.get(u) not in (None, s))
        self.ops.register_imported_trigger(fname)
        self._draft_flow, self._draft_ver, self.origin = fname, max(self.list_versions(fname), default=0) + 1, None
        return {"flow": fname, "compile": "OK" if (rep and rep.valid) else "FAIL", "stale_units": stale, "triggers": "OFF"}

    def set_binding(self, step_no: str, key: str, source: str) -> None:
        fc = self._draft_contract()
        idx = int(step_no) - 1
        if fc is None or not (0 <= idx < len(fc.steps)):
            return
        step = fc.steps[idx]
        pure = strip_ui_prefix(source)
        new_bind = dict(step.bindings)
        if not pure:
            new_bind[key] = UNBOUND_TOKEN
        elif pure == key:
            new_bind.pop(key, None)
        else:
            new_bind[key] = pure
        steps = list(fc.steps)
        steps[idx] = replace(step, bindings=new_bind)
        self.studio.viewport.synthesize(self._draft_flow, emit_flow_dsl(replace(fc, steps=tuple(steps))), desc=fc.desc, commit=True)

    def patch(self, step_no: str, *, cfg: dict[str, Any]) -> None:
        fc = self._draft_contract()
        idx = int(step_no) - 1
        if fc is None or not (0 <= idx < len(fc.steps)):
            return
        sname = fc.steps[idx].name
        reg = self.studio.viewport.registry
        u_obj = reg.units.get(sname) or reg.units.get(split_unit_ref(sname)[0])
        coerced = (
            {k: coerce_value_for_shape(v, u_obj.cfg_shapes[k]) if k in u_obj.cfg_shapes else v for k, v in cfg.items()}
            if u_obj is not None else dict(cfg)
        )
        self.studio.viewport.patch_slot(self._draft_flow, sname, new_cfg=coerced, commit=True)

    def configure_manifest(self, key: str, mode: str) -> None:
        self._input_modes[key] = mode
        fc = self._draft_contract()
        if self._draft_flow and fc is not None:
            slot_cfg: dict[str, Any] = {"mode": mode}
            if mode == "fixed":
                slot_cfg["fixed_value"] = fc.in_defaults.get(key, "")
            try:
                self.studio.configure_manifest(self._draft_flow, {"slots": {key: slot_cfg}})
            except Exception:
                pass

    def dryrun(self, sample: str, sources: dict[str, str]) -> dict[str, Any]:
        d = self.draft
        comp = compile_diagnostics(self.studio.viewport.registry, self._draft_flow, d)
        fc = self._draft_contract()
        seed_ctx = {**(fc.in_defaults if fc else {}), **dict(self.repo.samples(d["flow"]).get(sample) or {})}
        final_ctx: dict[str, Any] = {}
        step_inputs: dict[str, dict[str, str]] = {}

        if comp["ok"] and fc is not None:
            mode = "fixture" if (sources and all(m == "FIXTURE" for m in sources.values())) else "hybrid"
            try:
                snap, final_ctx, _ = self.studio.simulator.simulate(
                    self.studio.viewport, d["flow"], seed_ctx, mode=mode, node_sources=sources or None
                )
                step_inputs = getattr(self.studio.simulator, "last_step_inputs", {})
                events = trace_to_events(d["nodes"], snap.trace, self.studio.simulator.last_step_summaries, sources)
                out_rows = [[k, short_val(final_ctx.get(k, "-"))] for k in fc.out_keys]
            except Exception as exc:
                events = [{"node": "runtime", "status": "fail", "tag": "ERR", "dur": "", "note": str(exc)}]
                out_rows = [[k, "-"] for k in d["outputs"]]
        else:
            events = [
                {"node": f"{n['no']} {n['name']}", "status": step_status(n, comp),
                 "tag": sources.get(n["no"], "LIVE"), "dur": "", "note": "compile check"}
                for n in d["nodes"]
            ]
            out_rows = [[k, "-"] for k in d["outputs"]]

        last_n = d["nodes"][-1] if d["nodes"] else {"no": "00", "step_name": "", "in": {}}
        last_in_snap = step_inputs.get(last_n.get("step_name", ""), {})
        bind_rows = [
            [
                k,
                v or "(unbound)",
                last_in_snap.get(k) or (summarize_value_brief(final_ctx[strip_ui_prefix(v)]) if strip_ui_prefix(v) in final_ctx else "-"),
            ]
            for k, v in last_n["in"].items()
        ]
        blocks: list[dict[str, Any]] = [
            {"kind": "callout", "tone": "ok" if comp["ok"] else "fail",
             "text": "COMPILE OK · dry run finished" if comp["ok"] else "COMPILE FAIL · fix bindings before dry run"},
            {"kind": "timeline", "events": events},
            {"kind": "table", "title": f"node {last_n['no']} · binding resolution",
             "headers": ["in", "source", "value"], "rows": bind_rows},
            {"kind": "table", "title": "$out", "headers": ["key", "value"], "rows": out_rows},
        ]
        if seed_ctx:
            blocks.append({"kind": "table", "title": f"seed · {sample}", "headers": ["key", "value"],
                           "rows": [[k, short_val(v)] for k, v in seed_ctx.items()]})
        return {"flow": d["flow"], "version": d["version"], "stage": "dryrun",
                "title": f"{d['flow']} · Draft v{d['version']} · dry run · {sample}", "blocks": blocks}

    def pin(self) -> dict[str, Any]:
        comp = compile_diagnostics(self.studio.viewport.registry, self._draft_flow, self.draft)
        if not comp["ok"]:
            raise ValueError("; ".join(comp["errors"] or comp["unbound"] or comp["mismatch"]))
        snap = self.studio.viewport.project(self._draft_flow)
        if not snap.is_ready_to_publish:
            raise ValueError(f"missing_auth={list(snap.missing_auth)}, open_cfg={list(snap.open_cfg_slots)}")

        fc = self._draft_contract()
        assert fc is not None
        vers = self.studio.list_versions(self._draft_flow)
        nxt = (vers[-1] + 1) if vers else 1
        if not vers:
            return {"flow": self._draft_flow, "version": nxt, "summary": f"v{nxt} 최초 핀 생성", "keep": "initial release"}
        prev = self.studio.load_active(self._draft_flow, vers[-1])
        parts = [f"v{prev.version} -> v{nxt}"]
        if emit_flow_dsl(fc) != prev.flow_dsl:
            parts.append("배선/설정 변경")
        return {"flow": self._draft_flow, "version": nxt, "summary": " · ".join(parts), "keep": f"v{prev.version} 보관 (롤백 가능)"}

    def confirm_pin(self, receipt: dict[str, Any]) -> None:
        fname = str(receipt["flow"])
        fc = self.studio.viewport.registry.flows[fname]
        step_names = tuple(n for s in fc.steps for n in (s.name, s.heal_target) if n)
        _, slots_ok, mile_ok = self.studio.manifests.inspect(fname, fc.in_reqs, valid_step_names=step_names)
        if not (slots_ok and mile_ok) and step_names:
            slots = {k: {"mode": self._input_modes.get(k, "runtime")} for k in fc.in_reqs}
            self.studio.configure_manifest(fname, {"slots": slots, "milestones": [step_names[-1]]})

        rel = self.studio.pin(fname).release
        self.ops.promote_release(rel, desc=fc.desc)
        self._draft_ver = rel.version
        self.save_draft()

    def export_package(self, flow: str, version: int) -> bytes:
        rel: PinnedRelease = self.studio.load_active(flow, version)
        pkg = rel.to_dict()
        pkg["triggers"] = [{**t, "on": False} for t in pkg.get("triggers", [])]
        pkg["auth_refs"] = sorted(self.studio.viewport.project(flow).required_auth) if flow in self.studio.viewport.registry.flows else []
        return (json.dumps(pkg, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    def configure_trigger(self, flow: str, **spec: Any) -> None:
        self.ops.configure_trigger(flow, **spec)

    def next_run(self, flow: str) -> str:
        return self.ops.next_run(flow)

    def list_runs(self, flow: str) -> list[dict[str, Any]]:
        return self.ops.list_runs(flow)

    def fire(self, flow: str, payload: dict[str, Any] | None = None) -> int:
        if payload is not None and "payload" in inspect.signature(self.ops.fire).parameters:
            return self.ops.fire(flow, payload=payload)  # type: ignore[call-arg]
        return self.ops.fire(flow)

    def resume(self, flow: str, run_no: int) -> None:
        if self.list_versions(flow):
            rel = self.studio.load_active(flow)
            if not self.studio.verify_pin_shas(rel):
                raise RuntimeError(f"[RECOMPILE] unit SHA changed since pinned v{rel.version}; cannot resume run #{run_no}.")
        self.ops.resume(flow, run_no)

class _LazyRunbookBridge:
    _instance: RunbookBridge | None = None

    def __getattr__(self, name: str) -> Any:
        if self._instance is None:
            self._instance = RunbookBridge()
        return getattr(self._instance, name)

runbook: RunbookBridge = _LazyRunbookBridge()