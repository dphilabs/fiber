# fiber.node.reflex.runtime.workspace
from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

from xphi.arch.reflex.flow import UnitContract
from xphi.node.runtime.trigger.spec import DEFAULT_TRIGGERS, TriggerKind, TriggerSpec
from xphi.node.space.bind.resolver import resolve_path

from fiber.node.reflex.runtime.projector import SynthesisReport, ViewportSnapshot
from fiber.node.reflex.runtime.trigger import TriggerGate, TriggerRuntime
from fiber.node.reflex.runtime.viewport import ViewportRuntime
from fiber.node.reflex.runbook.spec import DRY_NOTE_KEY, is_write_unit, synthesize_dry_output
from fiber.node.reflex.runbook.inspector import emit_flow_dsl, emit_unit_dsl

DEFAULT_RUNBOOK_FILE = resolve_path("anchor") / "runbook.json"


class ViewportRunbook:
    """Manages workflow persistence, dry-run testing, trigger configuration, and publication."""

    def __init__(
        self,
        session: ViewportRuntime | None = None,
        *,
        storage_path: str | Path = DEFAULT_RUNBOOK_FILE,
        auto_load: bool = True,
    ) -> None:
        self.session = session or ViewportRuntime()
        self.storage_path = Path(storage_path).resolve()
        self.trigger_gate = TriggerGate()
        self.trigger_runtime = TriggerRuntime(self)

        if auto_load and self.storage_path.is_file():
            self.load()

    # =====================================================================
    # 1. Workspace Persistence (`load` / `save`)
    # =====================================================================

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path).resolve() if path else self.storage_path
        target.parent.mkdir(parents=True, exist_ok=True)

        payload: dict[str, Any] = {
            "version": "1.0",
            "updated_at": time.time(),
            "units": {
                name: {"flow": emit_unit_dsl(u), "desc": u.desc}
                for name, u in self.session.registry.units.items()
            },
            "flows": {
                name: {
                    "flow": emit_flow_dsl(f),
                    "desc": f.desc,
                    "published": (name in self.session.published_flows),
                    "triggers": [
                        t.to_dict()
                        for t in self.session.flow_triggers.get(name, DEFAULT_TRIGGERS)
                    ],
                }
                for name, f in self.session.registry.flows.items()
            },
            "seen_trigger_keys": self.trigger_gate.seen_keys,
        }
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(target)
        return target

    def load(self, path: str | Path | None = None) -> None:
        target = Path(path).resolve() if path else self.storage_path
        if not target.is_file():
            return

        raw = json.loads(target.read_text(encoding="utf-8"))
        self.session.registry.load_dict(raw)

        for flow_name, meta in raw.get("flows", {}).items():
            if not isinstance(meta, dict):
                continue
            if meta.get("published"):
                self.session.published_flows.add(flow_name)
            else:
                self.session.published_flows.discard(flow_name)

            raw_triggers = meta.get("triggers") or [{"kind": "manual"}]
            self.session.flow_triggers[flow_name] = tuple(
                TriggerSpec.from_raw(t) for t in raw_triggers if isinstance(t, (dict, str))
            ) or DEFAULT_TRIGGERS

        self.trigger_gate.load_keys(raw.get("seen_trigger_keys") or [])

    # =====================================================================
    # 2. Trigger Configuration (`set_triggers` / `configure_trigger`)
    # =====================================================================

    def set_triggers(self, flow_name: str, triggers: list[TriggerSpec | dict[str, Any] | str]) -> ViewportSnapshot:
        if flow_name not in self.session.registry.flows:
            raise KeyError(f"Flow '{flow_name}' not found in registry.")

        parsed: list[TriggerSpec] = []
        for raw_trig in triggers:
            spec = TriggerSpec.from_raw(raw_trig)
            spec.validate(flow_name)
            parsed.append(spec)

        self.session.flow_triggers[flow_name] = tuple(parsed) or DEFAULT_TRIGGERS
        return self.session.project(flow_name)

    def configure_trigger(
        self,
        flow_name: str,
        kind: TriggerKind,
        *,
        cron_expr: str | None = None,
        tz: str = "UTC",
        missed_policy: str = "skip",
        grace_sec: int = 300,
        source: str | None = None,
        dedup_path: str | None = None,
        map_in: dict[str, str] | None = None,
        append: bool = False,
    ) -> ViewportSnapshot:
        spec = TriggerSpec(
            kind=kind,
            cron_expr=cron_expr,
            tz=tz,
            missed_policy="run_once" if missed_policy == "run_once" else "skip",
            grace_sec=grace_sec,
            source=source,
            dedup_path=dedup_path,
            map_in=dict(map_in or {}),
        )
        existing = list(self.session.flow_triggers.get(flow_name, ())) if append else []
        return self.set_triggers(flow_name, [*existing, spec])

    # =====================================================================
    # 3. Dry-Run & Trigger Execution (`test_dry_run` / `fire_trigger`)
    # =====================================================================

    @staticmethod
    def _is_write_unit(
        scheme_or_unit: str | UnitContract,
        target: str = "",
        unit: UnitContract | None = None,
    ) -> bool:
        if isinstance(scheme_or_unit, UnitContract):
            u = scheme_or_unit
            return is_write_unit(u.scheme, u.target, u)
        return is_write_unit(scheme_or_unit, target, unit)

    def _resolve_flow_unit(self, flow_name: str, scheme: str, target: str) -> UnitContract | None:
        flow_obj = self.session.registry.flows.get(flow_name)
        if flow_obj is not None:
            step_names = {s.name for s in flow_obj.steps}
            for name, u in self.session.registry.units.items():
                if name in step_names and u.scheme == scheme and u.target == target:
                    return u
        return next(
            (u for u in self.session.registry.units.values() if u.scheme == scheme and u.target == target),
            None,
        )

    def test_dry_run(
        self,
        flow_name: str,
        initial_context: dict[str, Any] | None = None,
        *,
        mock_all_io: bool = False,
    ) -> tuple[ViewportSnapshot, dict[str, Any]]:
        def _dry_dispatcher(scheme: str, target: str, payload: dict[str, Any]) -> dict[str, Any] | None:
            if scheme == "flow":
                _, sub_ctx = self.test_dry_run(target, initial_context=payload, mock_all_io=mock_all_io)
                return sub_ctx

            unit = self._resolve_flow_unit(flow_name, scheme, target)
            if mock_all_io or is_write_unit(scheme, target, unit):
                out = synthesize_dry_output(unit, payload)
                out.pop(DRY_NOTE_KEY, None)
                return out

            return self.session.loader.dispatch(scheme, target, payload)

        return self.session.execute_with_trace(
            flow_name,
            dispatcher=_dry_dispatcher,
            initial_context=initial_context,
            allow_incomplete_auth=True,
        )

    def fire_trigger(
        self,
        flow_name: str,
        kind: TriggerKind = "manual",
        payload: dict[str, Any] | None = None,
        *,
        source: str | None = None,
        scheduled_slot_epoch: int | None = None,
        dry_run: bool = False,
        require_published: bool = True,
    ) -> tuple[ViewportSnapshot, dict[str, Any]] | None:
        if require_published and flow_name not in self.session.published_flows:
            raise PermissionError(f"Flow '{flow_name}' is in draft state and cannot fire live triggers.")

        triggers = self.session.flow_triggers.get(flow_name, DEFAULT_TRIGGERS)
        spec = next(
            (t for t in triggers if t.kind == kind and (source is None or t.source == source)),
            None,
        )
        if spec is None:
            raise ValueError(f"Flow '{flow_name}' has no matching '{kind}' trigger.")

        env = self.trigger_gate.prepare(
            flow_name,
            spec,
            payload,
            scheduled_slot_epoch=scheduled_slot_epoch,
        )
        if env is None:
            return None

        if dry_run:
            return self.test_dry_run(flow_name, initial_context=env.initial_context)
        return self.session.execute_with_trace(flow_name, initial_context=env.initial_context)

    def tick_cron(
        self,
        now_epoch: int | None = None,
        *,
        dry_run: bool = False,
        include_drafts: bool = False,
    ) -> list[tuple[str, ViewportSnapshot, dict[str, Any]]]:
        return self.trigger_runtime.tick_cron(now_epoch, dry_run=dry_run, include_drafts=include_drafts)

    def ingest_event(
        self,
        source: str,
        payload: dict[str, Any],
        *,
        dry_run: bool = False,
        include_drafts: bool = False,
    ) -> list[tuple[str, ViewportSnapshot, dict[str, Any]]]:
        return self.trigger_runtime.ingest_event(source, payload, dry_run=dry_run, include_drafts=include_drafts)

    # =====================================================================
    # 4. Publication Gate (`publish` / `unpublish`) & Session Passthrough
    # =====================================================================

    def publish(self, flow_name: str, *, auto_save: bool = True) -> ViewportSnapshot:
        snap = self.session.project(flow_name)
        blockers: list[str] = []
        if snap.missing_auth:
            blockers.append(f"missing credentials {list(snap.missing_auth)}")
        if snap.open_account_slots:
            blockers.append(f"unselected accounts {list(snap.open_account_slots)}")
        if snap.open_cfg_slots:
            blockers.append(f"unfilled cfg slots {list(snap.open_cfg_slots)}")

        if blockers:
            raise ValueError(f"Cannot publish '{flow_name}': " + "; ".join(blockers))

        self.session.published_flows.add(flow_name)
        if auto_save:
            self.save()
        return self.session.project(flow_name)

    def unpublish(self, flow_name: str, *, auto_save: bool = True) -> ViewportSnapshot:
        self.session.published_flows.discard(flow_name)
        if auto_save:
            self.save()
        return self.session.project(flow_name)

    def synthesize(self, flow_name: str, proposed_dsl: str, *, desc: str = "", commit: bool = True) -> SynthesisReport:
        return self.session.synthesize(flow_name, proposed_dsl, desc=desc, commit=commit)

    def patch_slot(self, flow_name: str, step_name: str, **kwargs: Any) -> SynthesisReport:
        return self.session.patch_slot(flow_name, step_name, **kwargs)