# fiber.node.runbook.inspector
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any, Literal
import uuid

from xphi.arch.contract.reflex.flow import Shape, UnitContract

from fiber.node.runbook.manifest import RunbookManifest, SlotPolicy

if TYPE_CHECKING:
    from fiber.node.runbook.viewport.projector import ViewportSnapshot
    from fiber.node.runbook.viewport.runtime import ViewportRuntime

DryMode = Literal["fixture", "hybrid"]

_WRITE_KEYWORDS = (
    "send", "post", "write", "create", "mutate", "apply", "commit",
    "push", "append", "update", "delete", "exec", "act", "deploy", "rewrite", "notify",
)


# =====================================================================
# Data Contracts & Step Summary Helpers
# =====================================================================

@dataclass(frozen=True, slots=True)
class ContentSchema:
    name: str
    required_columns: tuple[str, ...] = ()
    aliases: dict[str, str] = field(default_factory=dict)
    non_empty_columns: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    sha256: str
    source_uri: str
    filename: str | None = None
    row_count: int | None = None


@dataclass(frozen=True, slots=True)
class ContentReport:
    valid: bool
    normalized_rows: list[dict[str, Any]]
    matched_columns: tuple[str, ...] = ()
    missing_columns: tuple[str, ...] = ()
    alias_applied: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StepFixture:
    step_name: str
    input_sha: str
    output_sha: str
    expected_summary: str


def _extract_scheme_and_name(uri: str) -> tuple[str | None, str | None]:
    if "://" not in uri:
        return None, None
    raw_scheme, rest = uri.split("://", 1)
    scheme = "local" if raw_scheme == "file" else raw_scheme
    tail = rest.rstrip("/").rsplit("/", 1)[-1] or None
    return scheme, tail


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

class SourceInspector:
    def __init__(
        self,
        io_dir: Path | None = None,
        *,
        schemas: dict[str, ContentSchema] | None = None,
    ) -> None:
        self.io_dir = io_dir
        self._blobs: dict[str, Any] = {}
        self._refs: dict[str, ArtifactRef] = {}
        self._schemas: dict[str, ContentSchema] = dict(schemas or {})
        self._fixtures: dict[str, dict[str, StepFixture]] = {}
        self.last_step_summaries: dict[str, str] = {}

    # --- 1. Tabular Content Schema & Pre-Run Slot Verification ---
    def register_schema(self, schema: ContentSchema) -> None:
        self._schemas[schema.name] = schema

    def inspect_tabular(self, rows: list[dict[str, Any]], schema_name: str) -> ContentReport:
        schema = self._schemas.get(schema_name)
        if schema is None:
            return ContentReport(valid=True, normalized_rows=rows)

        applied_aliases: dict[str, str] = {}
        normalized: list[dict[str, Any]] = []
        observed_cols: set[str] = set()

        for row in rows:
            new_row: dict[str, Any] = {}
            for col, val in row.items():
                canonical = schema.aliases.get(col, col)
                if canonical != col:
                    applied_aliases[col] = canonical
                new_row[canonical] = val
                observed_cols.add(canonical)
            normalized.append(new_row)

        matched = tuple(c for c in schema.required_columns if c in observed_cols)
        missing = tuple(c for c in schema.required_columns if c not in observed_cols)
        if missing and rows:
            return ContentReport(
                valid=False,
                normalized_rows=normalized,
                matched_columns=matched,
                missing_columns=missing,
                alias_applied=applied_aliases,
                warnings=(f"필수 컬럼 누락: {', '.join(missing)}",),
            )

        check_cols = schema.non_empty_columns or schema.required_columns
        warns = tuple(
            f"빈 {col} {cnt}"
            for col in check_cols
            if (cnt := sum(1 for r in normalized if r.get(col) in (None, ""))) > 0
        )
        return ContentReport(
            valid=True,
            normalized_rows=normalized,
            matched_columns=matched,
            alias_applied=applied_aliases,
            warnings=warns,
        )

    def _resolve_slot_input(self, pol: SlotPolicy, incoming: dict[str, Any]) -> tuple[Any, str, str | None, list[str]]:
        if pol.mode == "fixed":
            if pol.fixed_value is None:
                return None, f"fixed://{pol.name}", None, [f"Fixed slot '{pol.name}' has no value."]
            return pol.fixed_value, f"fixed://{pol.name}", None, []

        raw_val = incoming.get(pol.name, pol.source_uri if pol.mode == "source" else None)
        if raw_val is None:
            return None, f"mem://{pol.name}", None, []

        if isinstance(raw_val, dict) and "uri" in raw_val:
            uri, fname = str(raw_val["uri"]), raw_val.get("filename")
            data = raw_val.get("rows", raw_val.get("data", raw_val))
        elif isinstance(raw_val, str) and "://" in raw_val:
            uri, fname, data = raw_val, None, raw_val
        else:
            return raw_val, f"mem://{pol.name}", None, []

        scheme, parsed_name = _extract_scheme_and_name(uri)
        allowed = set(pol.allowed_schemes)
        if scheme is not None and scheme not in allowed and raw_val is not pol.fixed_value:
            return None, uri, None, [f"Slot '{pol.name}' scheme '{scheme}://' not in allowed {sorted(allowed)}."]

        if scheme == "local" and isinstance(data, str) and data.startswith("file:///"):
            local_path = Path(data.removeprefix("file://"))
            if local_path.is_file() and local_path.suffix == ".json":
                data = json.loads(local_path.read_text(encoding="utf-8"))

        return data, uri, (fname or parsed_name), []

    def verify(
        self,
        raw_inputs: dict[str, Any] | None,
        manifest: RunbookManifest,
    ) -> tuple[dict[str, Any], dict[str, ArtifactRef], bool, list[str]]:
        incoming = dict(raw_inputs or {})
        resolved: dict[str, Any] = {k: v for k, v in incoming.items() if k not in manifest.slots}
        io_refs: dict[str, ArtifactRef] = {}
        warnings: list[str] = []

        for name, pol in manifest.slots.items():
            payload, uri, fname, errs = self._resolve_slot_input(pol, incoming)
            if errs:
                return {}, {}, False, errs
            if payload is None:
                continue

            if pol.contract_ref and isinstance(payload, list) and all(isinstance(r, dict) for r in payload):
                rep = self.inspect_tabular(payload, pol.contract_ref)
                if not rep.valid:
                    return {}, {}, False, list(rep.warnings)
                payload = rep.normalized_rows
                warnings.extend(rep.warnings)

            resolved[name] = payload
            io_refs[name] = self.store_artifact(payload, source_uri=uri, filename=fname)

        return resolved, io_refs, True, warnings

    # --- 2. Content-Addressed io/{sha256} Artifact Store ---
    def store_artifact(
        self,
        payload: Any,
        *,
        source_uri: str = "mem://step",
        filename: str | None = None,
    ) -> ArtifactRef:
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
        ref = ArtifactRef(
            sha256=digest,
            source_uri=source_uri,
            filename=filename,
            row_count=len(payload) if isinstance(payload, list) else None,
        )
        self._blobs[digest] = payload
        self._refs[digest] = ref

        if self.io_dir is not None:
            self.io_dir.mkdir(parents=True, exist_ok=True)
            (self.io_dir / f"{digest}.json").write_text(
                json.dumps({"ref": asdict(ref), "payload": payload}, indent=2, ensure_ascii=False, default=str) + "\n",
                encoding="utf-8",
            )
        return ref

    def load_artifact(self, io_sha: str) -> Any:
        if io_sha in self._blobs:
            return self._blobs[io_sha]
        if self.io_dir is not None and (blob_file := self.io_dir / f"{io_sha}.json").is_file():
            raw = json.loads(blob_file.read_text(encoding="utf-8"))
            self._blobs[io_sha] = raw.get("payload")
            return self._blobs[io_sha]
        raise KeyError(f"Artifact '{io_sha}' not found in io/ store.")

    def preview(self, io_sha: str, max_rows: int = 3) -> tuple[str, list[dict[str, Any]], int]:
        payload = self.load_artifact(io_sha)
        ref = self._refs.get(io_sha)
        uri_part = f"{ref.source_uri} (sha {io_sha[:8]})" if ref else f"sha {io_sha[:8]}"
        name_part = f" · {ref.filename}" if (ref and ref.filename) else ""

        if isinstance(payload, list):
            sample = [r if isinstance(r, dict) else {"value": r} for r in payload[:max_rows]]
            return f"{len(payload)}행 · 입력 {uri_part}{name_part}", sample, max(0, len(payload) - len(sample))
        if isinstance(payload, dict):
            return f"객체({len(payload)}키) · {uri_part}{name_part}", [payload], 0
        return f"값({str(payload)[:40]}) · {uri_part}{name_part}", [], 0

    # --- 3. Recorded Fixture & Dual-Mode (fixture / hybrid) Dry-Run Simulator ---
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
            in_ref = self.store_artifact(in_pay, source_uri=f"fx://{fid}/{step_name}/in")
            out_ref = self.store_artifact(clean_out, source_uri=f"fx://{fid}/{step_name}/out")
            steps[step_name] = StepFixture(
                step_name=step_name,
                input_sha=in_ref.sha256,
                output_sha=out_ref.sha256,
                expected_summary=summarize_step_delta(in_pay, clean_out),
            )
        self._fixtures[fid] = steps
        if self.io_dir is not None:
            self.io_dir.mkdir(parents=True, exist_ok=True)
            doc = {"fixture_id": fid, "flow_name": flow_name, "created_at": time.time(), "steps": {k: asdict(v) for k, v in steps.items()}}
            (self.io_dir / f"{fid}.fixture.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return fid, len(steps)

    def load_fixture_steps(self, fixture_id: str) -> dict[str, StepFixture]:
        if fixture_id in self._fixtures:
            return self._fixtures[fixture_id]
        if self.io_dir is not None and (path := self.io_dir / f"{fixture_id}.fixture.json").is_file():
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
                rec = {**dict(self.load_artifact(fx_steps[step_key].output_sha)), "__dry_run_skipped__": True}
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