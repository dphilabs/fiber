# fiber.studio.console.runbook.ops
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import time
from typing import Any
from zoneinfo import ZoneInfo

from xphi.node.runtime.reflex.engine import FlowEngine
from xphi.node.runtime.trigger.cron import compile_cron
from xphi.node.runtime.trigger.spec import DEFAULT_TRIGGERS, TriggerSpec

from fiber.studio.console.runbook.view import short_val
from fiber.studio.harness.reflex.context import FlowCatalog
from fiber.studio.runbook.runtime.workspace import ViewportRunbook
from fiber.studio.runbook.spec import (
    DRY_SKIP_FLAG,
    HITL_APPROVED_KEY,
    HITL_YIELD_KEY,
    PinnedRelease,
)


def load_json(path: Path, default: Any) -> Any:
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default
    return default


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)


def format_next_cron(spec: TriggerSpec | None, *, enabled: bool) -> str:
    if not enabled or spec is None or spec.kind != "cron" or not spec.cron_expr:
        return "-"
    try:
        compiled = compile_cron(spec.cron_expr)
        tz = ZoneInfo(spec.tz or "UTC")
        now_epoch = int(time.time())
        for attr in ("next_slot_after", "next_slot_at", "next_epoch"):
            if hasattr(compiled, attr):
                if nxt := getattr(compiled, attr)(now_epoch, spec.tz):
                    return datetime.fromtimestamp(int(nxt), tz).strftime("%m-%d %H:%M")
        base_min = ((now_epoch // 60) + 1) * 60
        for offset in range(0, 60 * 24 * 8, 60):
            cand = base_min + offset
            if compiled.latest_slot_at(cand, spec.tz, lookback_minutes=0) == cand:
                return datetime.fromtimestamp(cand, tz).strftime("%m-%d %H:%M")
    except Exception:
        return "-"
    return "-"


class LiveTriggerOps:
    """Manages isolated live execution (`ViewportRunbook`), trigger UI state, and run history."""

    def __init__(self, base_dir: Path, repo: FlowCatalog) -> None:
        self.base_dir = base_dir
        self.repo = repo
        self._runs_file = base_dir / "runs.json"
        self._trig_file = base_dir / "trigger_ui.json"

        self.live = ViewportRunbook(storage_path=base_dir / "live_runbook.json", auto_load=True)
        if self.repo.enabled:
            self.live.session.sync_catalog()

        self.runs: dict[str, list[dict[str, Any]]] = load_json(self._runs_file, {})
        self.triggers: dict[str, dict[str, Any]] = load_json(self._trig_file, {})

    def sync_releases(self, releases: list[PinnedRelease]) -> None:
        for rel in releases:
            fname = rel.flow_name
            if fname not in self.live.session.registry.flows:
                self.live.synthesize(fname, rel.flow_dsl, commit=True)
            if fname not in self.live.session.flow_triggers:
                self.live.session.flow_triggers[fname] = rel.triggers
            spec = (self.live.session.flow_triggers.get(fname) or rel.triggers or DEFAULT_TRIGGERS)[0]
            cur = self.triggers.get(fname, {})
            self.triggers[fname] = {
                "kind": cur.get("kind", spec.kind),
                "cron": cur.get("cron", spec.cron_expr or "0 9 * * 1-5"),
                "tz": cur.get("tz", spec.tz or "Asia/Seoul"),
                "on": fname in self.live.session.published_flows,
                "source": cur.get("source", spec.source or "webhook"),
                "map": dict(spec.map_in) if spec.map_in else dict(cur.get("map") or {}),
            }

    def promote_release(self, rel: PinnedRelease, desc: str = "") -> None:
        fname = rel.flow_name
        self.live.synthesize(fname, rel.flow_dsl, desc=desc, commit=True)
        specs = list(rel.triggers)
        if not any(t.kind == "manual" for t in specs):
            specs.append(TriggerSpec(kind="manual"))
        self.live.set_triggers(fname, specs)
        self.live.publish(fname, auto_save=True)
        self.sync_releases([rel])
        atomic_write_json(self._trig_file, self.triggers)

    def register_imported_trigger(self, flow: str) -> None:
        self.triggers[flow] = {"kind": "manual", "cron": "0 9 * * 1-5", "tz": "Asia/Seoul", "on": False, "map": {}}
        atomic_write_json(self._trig_file, self.triggers)

    def configure_trigger(self, flow: str, **spec: Any) -> None:
        cur = self.triggers.setdefault(
            flow, {"kind": "manual", "cron": "0 9 * * 1-5", "tz": "Asia/Seoul", "on": False, "map": {}}
        )
        cur.update(spec)

        if flow in self.live.session.registry.flows:
            kind = cur["kind"]
            primary = TriggerSpec(
                kind=kind,
                cron_expr=cur["cron"] if kind == "cron" else None,
                tz=cur.get("tz", "Asia/Seoul"),
                source=cur.get("source", "webhook") if kind == "event" else None,
                map_in=dict(cur.get("map") or {}) if kind == "event" else {},
            )
            # Keep an explicit `manual` trigger alongside cron/event so `[▶ RUN NOW]` always works
            specs = [primary] if kind == "manual" else [primary, TriggerSpec(kind="manual")]
            self.live.set_triggers(flow, specs)

            if cur.get("on"):
                try:
                    self.live.publish(flow, auto_save=True)
                except ValueError:
                    cur["on"] = False
            else:
                self.live.unpublish(flow, auto_save=True)

        atomic_write_json(self._trig_file, self.triggers)

    def next_run(self, flow: str) -> str:
        t = self.triggers.get(flow)
        if not t:
            return "-"
        specs = self.live.session.flow_triggers.get(flow, ())
        cron_spec = next((s for s in specs if s.kind == "cron"), None)
        if cron_spec is None and t["kind"] == "cron":
            cron_spec = TriggerSpec(kind="cron", cron_expr=t.get("cron"), tz=t.get("tz", "Asia/Seoul"))
        return format_next_cron(cron_spec, enabled=bool(t.get("on")))

    def list_runs(self, flow: str) -> list[dict[str, Any]]:
        return list(self.runs.get(flow, []))

    def fire(self, flow: str, payload: dict[str, Any] | None = None) -> int:
        runs = self.runs.setdefault(flow, [])
        run_no = (runs[0]["no"] + 1) if runs else 1
        now_str = time.strftime("%m-%d %H:%M")
        samples = self.repo.samples(flow)
        seed_ctx = dict(next(iter(samples.values()), {})) if samples else {}
        init_ctx = {**seed_ctx, **dict(payload)} if payload is not None else seed_ctx
        is_live = flow in self.live.session.published_flows

        try:
            if is_live:
                specs = self.live.session.flow_triggers.get(flow, ())
                if not any(s.kind == "manual" for s in specs):
                    self.live.set_triggers(flow, [*specs, TriggerSpec(kind="manual")])
                res = self.live.fire_trigger(flow, kind="manual", payload=init_ctx, require_published=False)
                assert res is not None
                snap, final_ctx = res
                status = "paused" if snap.status == "parked" else ("ok" if snap.status == "succeeded" else "fail")
                by_label = "manual"
            else:
                # Trigger is OFF: run safe preview (`test_dry_run`) and explicitly label status as `dry` (R-13)
                snap, final_ctx = self.live.test_dry_run(flow, initial_context=init_ctx)
                status = "paused" if snap.status == "parked" else ("dry" if snap.status == "succeeded" else "fail")
                by_label = "manual (dry)"

            preview = ", ".join(f"{k}={short_val(final_ctx[k], 24)}" for k in snap.exit_keys if k in final_ctx)
            if status == "paused":
                note = f"{snap.active_state} · approval pending"
            elif status == "dry":
                note = f"trigger OFF · dry preview ({preview or f'{len(snap.trace)} steps'})"
            else:
                note = preview or f"{len(snap.trace)} steps ok"

            ev_map = {"step_ok": "ok", "dry_run_skip": "dry", "hitl_parked": "wait", "gate_eval": "ok"}
            events = [
                {
                    "node": ev.state,
                    "status": ev_map.get(ev.event, "fail"),
                    "tag": "DRY-SKIP" if ev.event == "dry_run_skip" else ("LIVE" if is_live else "DRY"),
                    "dur": "",
                    "note": ev.error or ("write skipped (trigger OFF)" if ev.event == "dry_run_skip" else ""),
                }
                for ev in snap.trace
            ]
            rec: dict[str, Any] = {
                "no": run_no, "at": now_str, "by": by_label, "status": status, "note": note,
                "active_state": snap.active_state, "saved_ctx": final_ctx if status == "paused" else None,
                "events": events,
            }
        except BaseException as exc:
            rec = {
                "no": run_no, "at": now_str, "by": "manual" if is_live else "manual (dry)",
                "status": "fail", "note": str(exc),
                "events": [{"node": "engine", "status": "fail", "tag": "ERR", "dur": "", "note": str(exc)}],
            }

        runs.insert(0, rec)
        atomic_write_json(self._runs_file, self.runs)
        return run_no

    def resume(self, flow: str, run_no: int) -> None:
        target = next((r for r in self.runs.get(flow, []) if r["no"] == run_no and r["status"] == "paused"), None)
        if target is None:
            return

        saved_ctx = dict(target.get("saved_ctx") or {})
        saved_ctx.pop(HITL_YIELD_KEY, None)
        parked_state = target.get("active_state") or None
        vp = self.live.session
        fc = vp.registry.flows.get(flow)
        exit_keys = fc.out_keys if fc is not None else ()
        resumed_events: list[dict[str, Any]] = []

        def _resume_dispatch(scheme: str, unit_target: str, bound_payload: dict[str, Any]) -> dict[str, Any] | None:
            eff = {**bound_payload, HITL_APPROVED_KEY: True} if engine.state == parked_state else bound_payload
            out = vp._default_dispatch(scheme, unit_target, eff) or {}
            still_yield = bool(isinstance(out, dict) and out.get(HITL_YIELD_KEY) is True)
            skipped = bool(isinstance(out, dict) and out.get(DRY_SKIP_FLAG))
            resumed_events.append({
                "node": engine.state,
                "status": "wait" if still_yield else ("dry" if skipped else "ok"),
                "tag": "APPROVED" if engine.state == parked_state and not still_yield else "LIVE",
                "dur": "",
                "note": "approval pending" if still_yield else ("approved" if engine.state == parked_state else ""),
            })
            return out

        try:
            engine = FlowEngine.from_contract(flow, dispatcher=_resume_dispatch, registry=vp.registry)
            start_node = parked_state if (parked_state and parked_state in engine.states) else None
            final_ctx = engine.invoke(saved_ctx, start_at=start_node)

            if final_ctx.get(HITL_YIELD_KEY) is True:
                target["active_state"] = engine.state
                target["saved_ctx"] = final_ctx
                target["note"] = f"{engine.state} · approval pending"
            else:
                preview = ", ".join(f"{k}={short_val(final_ctx[k], 24)}" for k in exit_keys if k in final_ctx)
                target["status"] = "ok"
                target["note"] = f"approved · {preview or 'completed'}"
                target["saved_ctx"] = None

            prior = [ev for ev in target.get("events", []) if ev.get("status") != "wait"]
            target["events"] = [*prior, *resumed_events]
        except BaseException as exc:
            target["status"], target["note"] = "fail", f"resume failed: {exc}"

        atomic_write_json(self._runs_file, self.runs)