# fiber.studio.runbook.simulator
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any, Literal
import uuid

from xphi.arch.reflex.flow import Shape, UnitContract
from xphi.arch.model.content.artifact import ArtifactStore

if TYPE_CHECKING:
    from fiber.studio.runbook.runtime.projector import ViewportSnapshot
    from fiber.studio.runbook.runtime.viewport import ViewportRuntime

DryMode = Literal["fixture", "hybrid"]

_WRITE_KEYWORDS = (
    "send", "post", "write", "create", "mutate", "apply", "commit",
    "push", "append", "update", "delete", "exec", "act", "deploy", "rewrite", "notify",
)


@dataclass(frozen=True, slots=True)
class StepFixture:
    step_name: str
    input_sha: str
    output_sha: str
    expected_summary: str


def _mock_value_for_shape(shape: Shape, count: int) -> Any:
    if shape.is_list:
        return []
    if shape.base == "num":
        return count
    if shape.base == "bool":
        return True
    if shape.base == "obj":
        return {"dry_run": True, "count": count}
    return f"dry_preview_{count}"


def _infer_write_delta(payload: dict[str, Any]) -> tuple[int, str]:
    for key in ("rows", "valid_rows", "records", "items", "blocks", "entries"):
        if isinstance(payload.get(key), list):
            cnt = len(payload[key])
            return cnt, f"◌ dry · +{cnt}행"
    for val in payload.values():
        if isinstance(val, list):
            return len(val), f"◌ dry · +{len(val)}건"
    return 1, "◌ dry · ▸미리보기"


def summarize_step_delta(payload: dict[str, Any], output: dict[str, Any]) -> str:
    """S2/L3 스텝 요약 문자열 계산 ('3 → 2', '+2행 반영', '◌ dry · +2행' 등)."""
    if output.get("__dry_run_skipped__") and "_dry_preview_note" in output:
        return str(output["_dry_preview_note"])
    if isinstance(output.get("_summary"), str) and output["_summary"]:
        return output["_summary"]

    in_lists = [len(v) for v in payload.values() if isinstance(v, list)]
    out_lists = [len(v) for k, v in output.items() if not k.startswith("_") and isinstance(v, list)]
    if in_lists and out_lists:
        return f"{in_lists[0]} → {out_lists[0]}"
    if out_lists:
        return f"{out_lists[0]}행"

    for k in ("rows_written", "written", "updated", "inserted", "count"):
        if isinstance(output.get(k), int):
            return f"+{output[k]}행"
    return "통과"


class DryRunSimulator:
    """Recorded step-fixture manager and dual-mode (`fixture` / `hybrid`) dry-run engine."""

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

    def capture_fixture(
        self,
        flow_name: str,
        step_outputs: dict[str, tuple[dict[str, Any], dict[str, Any]]],
        fixture_id: str | None = None,
    ) -> tuple[str, int]:
        fid = fixture_id or f"fx_{flow_name}_{uuid.uuid4().hex[:6]}"
        steps: dict[str, StepFixture] = {}
        for step_name, (in_pay, out_pay) in step_outputs.items():
            clean_out = {k: v for k, v in out_pay.items() if k != "__dry_run_skipped__"}
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
    def _is_write_unit(unit: UnitContract | None, target: str) -> bool:
        haystack = f"{unit.name if unit else ''} {target}".lower()
        return any(kw in haystack for kw in _WRITE_KEYWORDS)

    @staticmethod
    def _synthesize_write_preview(unit: UnitContract | None, payload: dict[str, Any]) -> dict[str, Any]:
        count, note = _infer_write_delta(payload)
        out: dict[str, Any] = {}
        if unit is not None:
            for out_k in unit.out_keys:
                out[out_k] = payload[out_k] if out_k in payload else _mock_value_for_shape(unit.out_shape(out_k), count)
        return {**out, "_score": out.get("_score", 1.0), "_dry_preview_note": note, "__dry_run_skipped__": True}

    def simulate(
        self,
        viewport: ViewportRuntime,
        flow_name: str,
        context: dict[str, Any],
        *,
        mode: DryMode = "hybrid",
        fixture_id: str | None = None,
    ) -> tuple[ViewportSnapshot, dict[str, Any], float]:
        fx_steps = self.load_fixture_steps(fixture_id) if (fixture_id and mode == "fixture") else None
        step_summaries: dict[str, str] = {}

        def _dry_dispatcher(scheme: str, target: str, payload: dict[str, Any]) -> dict[str, Any] | None:
            if scheme == "flow":
                _, sub_ctx, _ = self.simulate(viewport, target, payload, mode=mode, fixture_id=fixture_id)
                return sub_ctx

            unit = next((u for u in viewport.registry.units.values() if u.scheme == scheme and u.target == target), None)
            step_key = unit.name if unit is not None else target

            if mode == "fixture" and fx_steps is not None and step_key in fx_steps:
                rec = {**dict(self.artifacts.get(fx_steps[step_key].output_sha)), "__dry_run_skipped__": True}
                step_summaries[step_key] = fx_steps[step_key].expected_summary
                return rec

            if mode == "fixture" or self._is_write_unit(unit, target):
                preview = self._synthesize_write_preview(unit, payload)
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
        clean_final = {k: v for k, v in final_ctx.items() if not k.startswith("_dry_")}
        return snap, clean_final, float(final_ctx.get("_score", 1.0))