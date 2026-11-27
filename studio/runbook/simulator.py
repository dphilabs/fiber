# fiber.studio.runbook.simulator
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any
import uuid

from xphi.arch.model.content.artifact import ArtifactStore
from xphi.arch.reflex.flow import UnitContract

from fiber.studio.runbook.spec import (
    DRY_SKIP_FLAG,
    DryMode,
    NodeSourceMode,
    StepFixture,
    is_write_unit,
    summarize_step_delta,
    summarize_value_brief,
    synthesize_dry_output,
)

if TYPE_CHECKING:
    from fiber.studio.runbook.runtime.projector import ViewportSnapshot
    from fiber.studio.runbook.runtime.viewport import ViewportRuntime

__all__ = [
    "DryMode",
    "DryRunSimulator",
    "NodeSourceMode",
    "StepFixture",
    "summarize_step_delta",
    "summarize_value_brief",
]


class DryRunSimulator:
    """Recorded step-fixture manager and node-aware (`fixture` / `hybrid` / per-node source) dry-run engine."""

    def __init__(
        self,
        artifacts: ArtifactStore | None = None,
        *,
        fixture_dir: Path | None = None,
    ) -> None:
        self.artifacts = artifacts or ArtifactStore(fixture_dir)
        self.fixture_dir = fixture_dir if fixture_dir is not None else self.artifacts.io_dir
        self._fixtures: dict[str, dict[str, StepFixture]] = {}
        self.last_step_summaries: dict[str, str] = {}
        self.last_step_inputs: dict[str, dict[str, str]] = {}

    def capture_fixture(
        self,
        flow_name: str,
        step_outputs: dict[str, tuple[dict[str, Any], dict[str, Any]]],
        fixture_id: str | None = None,
    ) -> tuple[str, int]:
        fid = fixture_id or f"fx_{flow_name}_{uuid.uuid4().hex[:6]}"
        steps: dict[str, StepFixture] = {}
        for step_name, (in_pay, out_pay) in step_outputs.items():
            clean_out = {k: v for k, v in out_pay.items() if k != DRY_SKIP_FLAG}
            in_ref = self.artifacts.put(in_pay, source_uri=f"fx://{fid}/{step_name}/in")
            out_ref = self.artifacts.put(clean_out, source_uri=f"fx://{fid}/{step_name}/out")
            steps[step_name] = StepFixture(
                step_name=step_name,
                input_sha=in_ref.sha256,
                output_sha=out_ref.sha256,
                expected_summary=summarize_step_delta(in_pay, clean_out),
            )
        self._fixtures[fid] = steps
        if self.fixture_dir is not None:
            self.fixture_dir.mkdir(parents=True, exist_ok=True)
            doc = {
                "fixture_id": fid,
                "flow_name": flow_name,
                "created_at": time.time(),
                "steps": {k: asdict(v) for k, v in steps.items()},
            }
            (self.fixture_dir / f"{fid}.fixture.json").write_text(
                json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        return fid, len(steps)

    def load_fixture_steps(self, fixture_id: str) -> dict[str, StepFixture]:
        if fixture_id in self._fixtures:
            return self._fixtures[fixture_id]
        if self.fixture_dir is not None and (path := self.fixture_dir / f"{fixture_id}.fixture.json").is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            steps = {k: StepFixture(**v) for k, v in (raw.get("steps") or {}).items()}
            self._fixtures[fixture_id] = steps
            return steps
        raise KeyError(f"Fixture '{fixture_id}' not found.")

    @staticmethod
    def _is_write_unit(
        scheme_or_unit: str | UnitContract | None,
        target: str = "",
        unit: UnitContract | None = None,
    ) -> bool:
        if isinstance(scheme_or_unit, str):
            return is_write_unit(scheme_or_unit, target, unit)
        u = scheme_or_unit
        return is_write_unit(u.scheme if u is not None else "", target, u)

    @staticmethod
    def _synthesize_write_preview(unit: UnitContract | None, payload: dict[str, Any]) -> dict[str, Any]:
        return synthesize_dry_output(unit, payload)

    def simulate(
        self,
        viewport: ViewportRuntime,
        flow_name: str,
        context: dict[str, Any],
        *,
        mode: DryMode = "hybrid",
        fixture_id: str | None = None,
        node_sources: dict[str, str] | None = None,
    ) -> tuple[ViewportSnapshot, dict[str, Any], float]:
        need_fx = bool(fixture_id) and (
            mode == "fixture"
            or any(v.upper() in ("FIXTURE", "REPLAY") for v in (node_sources or {}).values())
        )
        fx_steps = self.load_fixture_steps(fixture_id) if (need_fx and fixture_id) else None
        step_summaries: dict[str, str] = {}
        step_inputs: dict[str, dict[str, str]] = {}

        flow_obj = viewport.registry.flows.get(flow_name)
        no_by_step = {
            s.name: f"{idx:02d}" for idx, s in enumerate(flow_obj.steps, 1)
        } if flow_obj is not None else {}
        flow_unit_names = set(no_by_step)

        def _resolve_unit(scheme: str, target: str) -> UnitContract | None:
            for name, u in viewport.registry.units.items():
                if name in flow_unit_names and u.scheme == scheme and u.target == target:
                    return u
            return next(
                (u for u in viewport.registry.units.values() if u.scheme == scheme and u.target == target),
                None,
            )

        def _dry_dispatcher(scheme: str, target: str, payload: dict[str, Any]) -> dict[str, Any] | None:
            if scheme == "flow":
                _, sub_ctx, _ = self.simulate(viewport, target, payload, mode=mode, fixture_id=fixture_id)
                return sub_ctx

            unit = _resolve_unit(scheme, target)
            step_key = unit.name if unit is not None else target
            step_no = no_by_step.get(step_key, "")
            step_inputs[step_key] = {
                k: summarize_value_brief(v) for k, v in payload.items() if not k.startswith("_")
            }

            src_override = None
            if node_sources:
                raw_src = (
                    node_sources.get(step_no)
                    or node_sources.get(step_key)
                    or node_sources.get(f"{step_key}@{step_no}")
                )
                src_override = raw_src.upper() if raw_src else None

            use_fx = (src_override in ("FIXTURE", "REPLAY")) or (src_override is None and mode == "fixture")
            if use_fx and fx_steps is not None:
                fx_rec = fx_steps.get(f"{step_key}@{step_no}") or fx_steps.get(step_key)
                if fx_rec is not None:
                    rec = {**dict(self.artifacts.get(fx_rec.output_sha)), DRY_SKIP_FLAG: True}
                    step_summaries[step_key] = fx_rec.expected_summary
                    return rec

            force_dry = src_override in ("DRY", "FIXTURE", "REPLAY")
            force_live = src_override == "LIVE"
            if force_dry or (not force_live and (mode == "fixture" or is_write_unit(scheme, target, unit))):
                preview = synthesize_dry_output(unit, payload)
                step_summaries[step_key] = summarize_step_delta(payload, preview)
                return preview

            live_out = viewport._default_dispatch(scheme, target, payload) or {}
            step_summaries[step_key] = summarize_step_delta(payload, live_out)
            return live_out

        snap, final_ctx = viewport.execute_with_trace(
            flow_name,
            dispatcher=_dry_dispatcher,
            initial_context=context,
            allow_incomplete_auth=True,
        )
        self.last_step_summaries = step_summaries
        self.last_step_inputs = step_inputs
        clean_final = {k: v for k, v in final_ctx.items() if not k.startswith("_dry_")}
        return snap, clean_final, float(final_ctx.get("_score", 1.0))