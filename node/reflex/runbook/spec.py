# fiber.node.reflex.runbook.spec
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Literal

from xphi.arch.reflex.flow import Shape, UnitContract
from xphi.node.runtime.reflex.manifest import OutputWidget, ReflexManifest
from xphi.node.runtime.trigger.scheduler import ScheduleVerdict
from xphi.node.runtime.trigger.spec import DEFAULT_TRIGGERS, TriggerSpec

# Execution, Telemetry & Binding Protocol Constants
DryMode = Literal["fixture", "hybrid"]
NodeSourceMode = Literal["LIVE", "FIXTURE", "REPLAY", "DRY"]
RunStatus = Literal["idle", "running", "parked", "succeeded", "failed"]
TelemetryEventKind = Literal["step_ok", "dry_run_skip", "hitl_parked", "gate_eval", "fail_caught", "halted"]

DRY_SKIP_FLAG = "__dry_run_skipped__"
DRY_NOTE_KEY = "_dry_preview_note"
HITL_YIELD_KEY = "_hitl_yield"
HITL_TICKET_KEY = "_hitl_ticket"
HITL_APPROVED_KEY = "_hitl_approved"
UNBOUND_TOKEN = "__unbound__"

WRITE_KEYWORDS: tuple[str, ...] = (
    "send", "post", "write", "create", "mutate", "apply", "commit",
    "push", "append", "update", "upsert", "insert", "save", "put", "patch",
    "delete", "remove", "drop", "upload", "publish", "exec", "act", "deploy",
    "rewrite", "notify",
)

_PURE_SIDE_EFFECT_TAGS: frozenset[str] = frozenset({"", "none", "false", "read", "pure", "readonly"})

# Side-Effect Classification, Dry-Run Synthesis & Shape Coercion
def is_write_unit(scheme: str, target: str, unit: UnitContract | None = None) -> bool:
    """Classifies mutating/external-write units via `@side_effect` -> scheme -> verb keywords."""
    if unit is not None:
        se = getattr(unit, "side_effect", None)
        if isinstance(se, bool):
            return se
        if isinstance(se, str) and se.strip():
            return se.strip().lower() not in _PURE_SIDE_EFFECT_TAGS
    if scheme == "exec":
        return True
    haystack = f"{unit.name if unit else ''} {target}".lower()
    return any(kw in haystack for kw in WRITE_KEYWORDS)


def default_widget_for_shape(shape: Shape) -> OutputWidget:
    if shape.is_list:
        return "table"
    if shape.base == "num":
        return "metric"
    return "text"

def mock_value_for_shape(shape: Shape, count: int = 1) -> Any:
    if shape.is_list:
        return []
    if shape.base == "num":
        return count
    if shape.base == "bool":
        return True
    if shape.base == "obj":
        return {"dry_run": True, "count": count}
    return f"dry_preview_{count}"

def coerce_value_for_shape(value: Any, shape: Shape) -> Any:
    """Coerces raw UI/config inputs (e.g. `'1600'`, `'true'`) to match target `Shape` (R-1)."""
    if shape.is_list:
        if isinstance(value, list):
            elem_shape = shape.element()
            return [coerce_value_for_shape(v, elem_shape) for v in value]
        if isinstance(value, str):
            parsed = json.loads(value)
            if isinstance(parsed, list):
                elem_shape = shape.element()
                return [coerce_value_for_shape(v, elem_shape) for v in parsed]
        raise ValueError(f"Expected list for shape '{shape}', got {value!r}.")

    base = shape.base
    if base in ("any", ""):
        return value
    if base == "text":
        if isinstance(value, (dict, list)):
            raise ValueError(f"Expected text for shape '{shape}', got {type(value).__name__}.")
        return str(value)
    if base == "num":
        if isinstance(value, bool):
            raise ValueError(f"Expected num for shape '{shape}', got bool {value!r}.")
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str) and value.strip():
            s = value.strip()
            try:
                return int(s) if ("." not in s and "e" not in s.lower()) else float(s)
            except ValueError as exc:
                raise ValueError(f"Cannot coerce {value!r} to num.") from exc
        raise ValueError(f"Cannot coerce {value!r} to num.")
    if base == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "1", "yes", "on"):
                return True
            if low in ("false", "0", "no", "off"):
                return False
        raise ValueError(f"Cannot coerce {value!r} to bool.")
    if base == "obj":
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        raise ValueError(f"Expected object for shape '{shape}', got {value!r}.")
    return value


def infer_write_delta(payload: dict[str, Any]) -> tuple[int, str]:
    for key in ("rows", "valid_rows", "records", "items", "blocks", "entries"):
        if isinstance(payload.get(key), list):
            cnt = len(payload[key])
            return cnt, f"◌ dry · +{cnt}행"
    for val in payload.values():
        if isinstance(val, list):
            return len(val), f"◌ dry · +{len(val)}건"
    return 1, "◌ dry · ▸미리보기"


def synthesize_dry_output(unit: UnitContract | None, payload: dict[str, Any]) -> dict[str, Any]:
    count, note = infer_write_delta(payload)
    out: dict[str, Any] = {}
    if unit is not None:
        for out_k in unit.out_keys:
            out[out_k] = payload[out_k] if out_k in payload else mock_value_for_shape(unit.out_shape(out_k), count)
    return {**out, "_score": out.get("_score", 1.0), DRY_NOTE_KEY: note, DRY_SKIP_FLAG: True}


def summarize_value_brief(val: Any, limit: int = 48) -> str:
    """Compact value summary for binding inspection (`[×36]`, `{id, name}`, scalar)."""
    if isinstance(val, list):
        return f"[×{len(val)}]"
    if isinstance(val, dict):
        keys = list(val.keys())
        preview = ", ".join(keys[:3]) + (", ..." if len(keys) > 3 else "")
        return f"{{{preview}}}"
    text = repr(val) if isinstance(val, str) else str(val)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def summarize_step_delta(payload: dict[str, Any], output: dict[str, Any]) -> str:
    if output.get(DRY_SKIP_FLAG) and DRY_NOTE_KEY in output:
        return str(output[DRY_NOTE_KEY])
    if isinstance(output.get("_summary"), str) and output["_summary"]:
        return output["_summary"]

    in_lists = [len(v) for v in payload.values() if isinstance(v, list)]
    out_lists = [len(v) for k, v in output.items() if not k.startswith("_") and isinstance(v, list)]
    if in_lists and out_lists:
        return f"{in_lists[0]} → {out_lists[0]}"
    if out_lists:
        return f"{out_lists[0]}행"

    for k in ("rows_written", "written", "saved_count", "updated", "inserted", "count"):
        if isinstance(output.get(k), int):
            return f"+{output[k]}행"
    return "통과"

# Immutable Release & Fixture Value Objects
@dataclass(frozen=True, slots=True)
class StepFixture:
    step_name: str
    input_sha: str
    output_sha: str
    expected_summary: str

@dataclass(frozen=True, slots=True)
class PinnedRelease:
    flow_name: str
    version: int
    pinned_at: float
    flow_dsl: str
    compiled_asl: dict[str, Any]
    unit_shas: dict[str, str]
    manifest: ReflexManifest
    triggers: tuple[TriggerSpec, ...] = DEFAULT_TRIGGERS
    fixture_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "flow_name": self.flow_name,
            "version": self.version,
            "pinned_at": self.pinned_at,
            "flow_dsl": self.flow_dsl,
            "compiled_asl": self.compiled_asl,
            "unit_shas": dict(self.unit_shas),
            "manifest": self.manifest.to_dict(),
            "triggers": [t.to_dict() for t in self.triggers],
            "fixture_id": self.fixture_id,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PinnedRelease:
        raw_trigs = raw.get("triggers") or [{"kind": "manual"}]
        triggers = tuple(
            TriggerSpec.from_raw(t)
            for t in raw_trigs
            if isinstance(t, (dict, str, TriggerSpec))
        )
        return cls(
            flow_name=str(raw["flow_name"]),
            version=int(raw["version"]),
            pinned_at=float(raw["pinned_at"]),
            flow_dsl=str(raw["flow_dsl"]),
            compiled_asl=dict(raw.get("compiled_asl") or {}),
            unit_shas=dict(raw.get("unit_shas") or {}),
            manifest=ReflexManifest.from_dict(raw["manifest"]),
            triggers=triggers or DEFAULT_TRIGGERS,
            fixture_id=raw.get("fixture_id"),
        )

@dataclass(frozen=True, slots=True)
class PinReceipt:
    release: PinnedRelease
    change_summary: str
    schedule: ScheduleVerdict