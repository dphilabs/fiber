# fiber.node.reflex.runtime.manager
## @lineage: fiber.reflex.runtime.manager
## @lineage: reflexor.workspace.manager
## @lineage: reflexor.runtime.workspace.manager
from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

from fiber.node.reflex.runtime.trigger.base import (
    DEFAULT_TRIGGERS,
    TriggerGate,
    TriggerKind,
    TriggerSpec,
)
from fiber.node.reflex.runtime.inspector import emit_flow_dsl, emit_unit_dsl
from fiber.node.reflex.runtime.projection import SynthesisReport, ViewportSnapshot
from fiber.node.reflex.flow.viewport import WorkflowViewport

from xphi.arch.contract.reflex.flow import Shape, UnitContract
from xphi.node.space.bind.resolver import resolve_path

DEFAULT_WORKSPACE_FILE = resolve_path("anchor") / "workflows.json"
_WRITE_KEYWORDS = (
    "send", "post", "create", "mutate", "apply", "commit",
    "push", "append", "update", "delete", "exec", "act", "deploy", "rewrite",
)


def _default_for_shape(shape: Shape) -> Any:
    if shape.is_list:
        return []
    if shape.base == "num":
        return 1.0
    if shape.base == "bool":
        return True
    if shape.base == "obj":
        return {}
    return "dry_run_val"


class ViewportWorkspace:
    """Manages workflow persistence, dry-run testing, trigger configuration, and publication."""

    def __init__(
        self,
        session: WorkflowViewport | None = None,
        *,
        storage_path: str | Path = DEFAULT_WORKSPACE_FILE,
        auto_load: bool = True,
    ) -> None:
        self.session = session or WorkflowViewport()
        self.storage_path = Path(storage_path).resolve()
        self.trigger_gate = TriggerGate()

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
        target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
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
                TriggerSpec.from_dict(t) for t in raw_triggers if isinstance(t, dict)
            )

        self.trigger_gate.load_keys(raw.get("seen_trigger_keys") or [])

    # =====================================================================
    # 2. Trigger Configuration (`set_triggers` / `configure_trigger`)
    # =====================================================================

    def set_triggers(self, flow_name: str, triggers: list[TriggerSpec | dict[str, Any]]) -> ViewportSnapshot:
        if flow_name not in self.session.registry.flows:
            raise KeyError(f"Flow '{flow_name}' not found in registry.")

        parsed: list[TriggerSpec] = []
        for t in triggers:
            spec = TriggerSpec.from_dict(t) if isinstance(t, dict) else t
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
    def _is_write_unit(unit: UnitContract) -> bool:
        if unit.scheme == "mcp" and any(w in unit.target.lower() or w in unit.name.lower() for w in _WRITE_KEYWORDS):
            return True
        return any(w in unit.name.lower() for w in _WRITE_KEYWORDS)

    def _synthesize_mock_output(self, scheme: str, target: str, payload: dict[str, Any]) -> dict[str, Any]:
        for u in self.session.registry.units.values():
            if u.scheme == scheme and u.target == target:
                out = {k: payload.get(k, _default_for_shape(u.out_shape(k))) for k in u.out_keys}
                out.setdefault("_score", 1.0)
                out["__dry_run_skipped__"] = True
                return out
        return {"_score": 1.0, "__dry_run_skipped__": True}

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

            unit = next((u for u in self.session.registry.units.values() if u.scheme == scheme and u.target == target), None)
            if mock_all_io or (unit is not None and self._is_write_unit(unit)):
                return self._synthesize_mock_output(scheme, target, payload)

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