# fiber.studio.runbook.store
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time
from typing import Any

from xphi.arch.reflex.flow import Shape
from xphi.arch.reflex.compiler import compile_to_asl
from xphi.arch.xor.asl.evaluator import verify_asl
from xphi.node.runtime.reflex.manifest import ManifestStore, OutputPolicy, OutputWidget, ReflexManifest
from xphi.node.runtime.trigger.scheduler import SchedulePlanner, ScheduleVerdict
from xphi.node.runtime.trigger.spec import DEFAULT_TRIGGERS, TriggerSpec
from xphi.arch.model.content.artifact import ArtifactStore
from xphi.arch.model.content.intake import ContentSchema, SlotVerifier

from fiber.studio.runbook.simulator import DryMode, DryRunSimulator
from fiber.studio.runbook.stepper import emit_flow_dsl, inspect_flow
from fiber.studio.runbook.runtime.viewport import ViewportRuntime


def _default_widget_for_shape(shape: Shape) -> OutputWidget:
    if shape.is_list:
        return "table"
    if shape.base == "num":
        return "metric"
    return "text"


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


class ReleaseStudio:
    """Studio 진입점(Draft 편집 · Dry-Run · Pin) 겸 디스크 영속 Pinned vN 저장소."""

    def __init__(
        self,
        root_dir: Path,
        *,
        viewport: ViewportRuntime | None = None,
        artifacts: ArtifactStore | None = None,
        intake: SlotVerifier | None = None,
        simulator: DryRunSimulator | None = None,
        schemas: dict[str, ContentSchema] | None = None,
    ) -> None:
        self.root_dir = root_dir
        self.releases_dir = root_dir / "releases"
        self.viewport = viewport or ViewportRuntime()
        self.manifests = ManifestStore(root_dir / "drafts")
        self.planner = SchedulePlanner()

        io_dir = root_dir / "io"
        self.artifacts = artifacts or ArtifactStore(io_dir)
        self.intake = intake or SlotVerifier(self.artifacts, schemas=schemas)
        self.simulator = simulator or DryRunSimulator(self.artifacts, fixture_dir=io_dir)
        self._pins: dict[str, list[PinnedRelease]] = {}

        if self.releases_dir.is_dir():
            self._reload_pins_from_disk()

    # =====================================================================
    # 1. Disk Persistence (`releases/<flow>/vNNNN.json`)
    # =====================================================================

    def _reload_pins_from_disk(self) -> None:
        for flow_dir in sorted(self.releases_dir.iterdir()):
            if not flow_dir.is_dir():
                continue
            versions: list[PinnedRelease] = []
            for v_file in sorted(flow_dir.glob("v*.json")):
                rel = PinnedRelease.from_dict(json.loads(v_file.read_text(encoding="utf-8")))
                versions.append(rel)
                if rel.flow_name not in self.viewport.registry.flows:
                    try:
                        self.viewport.registry.register_flow(rel.flow_name, rel.flow_dsl)
                    except ValueError:
                        pass
            if versions:
                versions.sort(key=lambda r: r.version)
                fname = versions[-1].flow_name
                self._pins[fname] = versions
                self.viewport.published_flows.add(fname)
                self.viewport.flow_triggers[fname] = versions[-1].triggers

    def _save_pin_disk(self, rel: PinnedRelease) -> Path:
        safe_name = rel.flow_name.replace("/", "__")
        target = self.releases_dir / safe_name / f"v{rel.version:04d}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(rel.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return target

    # =====================================================================
    # 2. Draft Manifest, Schema & Dry-Run Orchestration
    # =====================================================================

    def register_schema(self, schema: ContentSchema) -> None:
        self.intake.register_schema(schema)

    def configure_manifest(self, flow_name: str, patch_ops: dict[str, Any]) -> ReflexManifest:
        return self.manifests.patch(flow_name, patch_ops)

    def _ensure_declared_outputs(self, flow_name: str, manifest: ReflexManifest) -> ReflexManifest:
        """플로우 계약의 out(...) 키가 manifest.outputs에 비어 있으면 Shape 기반 기본 위젯으로 보강."""
        flow = self.viewport.registry.flows.get(flow_name)
        if flow is None or not flow.out_keys:
            return manifest

        missing_outs = [k for k in flow.out_keys if not k.startswith("_") and k not in manifest.outputs]
        if not missing_outs:
            return manifest

        return self.manifests.patch(
            flow_name,
            {
                "outputs": {
                    k: OutputPolicy(key=k, widget=_default_widget_for_shape(flow.out_shape(k))).to_dict()
                    for k in missing_outs
                }
            },
        )

    def dryrun(
        self,
        flow_name: str,
        sample_inputs: dict[str, Any] | None = None,
        *,
        mode: DryMode = "hybrid",
        fixture_id: str | None = None,
    ) -> dict[str, Any]:
        if flow_name not in self.viewport.registry.flows:
            raise KeyError(f"Flow '{flow_name}' not found in registry.")

        flow = self.viewport.registry.flows[flow_name]
        manifest, _, _ = self.manifests.inspect(flow_name, flow.in_reqs)
        manifest = self._ensure_declared_outputs(flow_name, manifest)

        resolved_ctx, io_refs, ok, warnings = self.intake.verify(sample_inputs, manifest)
        if not ok:
            raise ValueError(f"Sample input contract failed for '{flow_name}': {warnings}")

        snap, preview_out, score = self.simulator.simulate(
            self.viewport,
            flow_name,
            resolved_ctx,
            mode=mode,
            fixture_id=fixture_id,
        )
        return {
            "snapshot": snap,
            "preview_out": preview_out,
            "step_summaries": dict(self.simulator.last_step_summaries),
            "io_refs": io_refs,
            "warnings": warnings,
            "score": score,
        }

    # =====================================================================
    # 3. Release Pinning, Lookup & Staleness Checks
    # =====================================================================

    def pin(
        self,
        flow_name: str,
        proposed_dsl: str | None = None,
        *,
        fixture_id: str | None = None,
    ) -> PinReceipt:
        if proposed_dsl is not None:
            rep = self.viewport.synthesize(flow_name, proposed_dsl, commit=True)
            if not rep.valid:
                raise ValueError(f"Synthesis failed: {rep.errors}")

        snap = self.viewport.project(flow_name)
        if not snap.is_ready_to_publish:
            raise ValueError(
                f"Cannot pin '{flow_name}': missing_auth={snap.missing_auth}, "
                f"open_accounts={snap.open_account_slots}, open_cfg={snap.open_cfg_slots}"
            )

        inspection = inspect_flow(self.viewport.registry, flow_name)
        if inspection.errors:
            raise ValueError(f"Static flow inspection failed for '{flow_name}': {inspection.errors}")

        flow = self.viewport.registry.flows[flow_name]
        step_names = tuple(n for s in flow.steps for n in (s.name, s.heal_target) if n)
        manifest, slots_ok, mile_ok = self.manifests.inspect(
            flow_name, flow.in_reqs, valid_step_names=step_names
        )
        if not (slots_ok and mile_ok):
            raise ValueError(
                f"Manifest incomplete for '{flow_name}' (slots_ok={slots_ok}, milestone_ok={mile_ok})."
            )
        manifest = self._ensure_declared_outputs(flow_name, manifest)

        asl = compile_to_asl(flow_name, self.viewport.registry)
        if asl_errors := verify_asl(asl):
            raise ValueError(f"ASL verification failed: {asl_errors}")

        unit_shas = self._collect_unit_shas(flow_name)
        triggers = self.viewport.flow_triggers.get(flow_name, DEFAULT_TRIGGERS)

        history = self._pins.setdefault(flow_name, [])
        prev = history[-1] if history else None
        next_ver = (prev.version + 1) if prev else 1

        rel = PinnedRelease(
            flow_name=flow_name,
            version=next_ver,
            pinned_at=time.time(),
            flow_dsl=emit_flow_dsl(flow),
            compiled_asl=asl,
            unit_shas=unit_shas,
            manifest=manifest,
            triggers=triggers,
            fixture_id=fixture_id,
        )
        history.append(rel)
        self._save_pin_disk(rel)
        self.viewport.published_flows.add(flow_name)

        diff_summary = self._summarize_diff(prev, rel, tuple(inspection.warnings))
        verdict = self.planner.evaluate(manifest, triggers)
        return PinReceipt(release=rel, change_summary=diff_summary, schedule=verdict)

    def load_active(self, flow_name: str, version: int | None = None) -> PinnedRelease:
        history = self._pins.get(flow_name) or []
        if not history:
            raise KeyError(f"No pinned release for '{flow_name}'.")
        if version is None:
            return history[-1]
        for rel in reversed(history):
            if rel.version == version:
                return rel
        raise KeyError(f"Pinned version v{version} not found for '{flow_name}'.")

    def list_active(self) -> list[PinnedRelease]:
        return [versions[-1] for versions in self._pins.values() if versions]

    def verify_pin_shas(self, release: PinnedRelease) -> bool:
        current = self._collect_unit_shas(release.flow_name)
        return all(current.get(u) == sha for u, sha in release.unit_shas.items())

    def scan_staleness(self, releases: list[PinnedRelease]) -> list[str]:
        return [r.flow_name for r in releases if not self.verify_pin_shas(r)]

    def _collect_unit_shas(self, flow_name: str) -> dict[str, str]:
        flow = self.viewport.registry.flows.get(flow_name)
        if flow is None:
            return {}
        shas: dict[str, str] = {}
        target_names = (n for s in flow.steps for n in (s.name, s.heal_target) if n)
        for unit_name in target_names:
            unit = self.viewport.registry.units.get(unit_name)
            if unit is None or unit_name in shas:
                continue
            if hasattr(self.viewport.indexer, "unit_sha"):
                shas[unit_name] = str(self.viewport.indexer.unit_sha(unit_name))
            else:
                spec_sig = json.dumps(
                    {
                        "scheme": unit.scheme,
                        "target": unit.target,
                        "in": list(unit.in_reqs) + list(unit.in_optional),
                        "out": list(unit.out_keys),
                        "cfg": unit.cfg_defaults,
                    },
                    sort_keys=True,
                )
                shas[unit_name] = hashlib.sha256(spec_sig.encode("utf-8")).hexdigest()[:12]
        return shas

    @staticmethod
    def _summarize_diff(prev: PinnedRelease | None, curr: PinnedRelease, warnings: tuple[str, ...]) -> str:
        if prev is None:
            return f"v{curr.version} 최초 핀 생성"
        parts = [f"v{prev.version} -> v{curr.version}"]
        added_slots = sorted(set(curr.manifest.slots) - set(prev.manifest.slots))
        removed_slots = sorted(set(prev.manifest.slots) - set(curr.manifest.slots))
        changed_units = sorted(
            u for u, sha in curr.unit_shas.items() if prev.unit_shas.get(u) not in (None, sha)
        )
        if added_slots:
            parts.append(f"슬롯 추가: {added_slots}")
        if removed_slots:
            parts.append(f"슬롯 제거: {removed_slots}")
        if changed_units:
            parts.append(f"유닛 갱신: {changed_units}")
        if prev.flow_dsl != curr.flow_dsl and not (added_slots or removed_slots):
            parts.append("토폴로지 변경")
        if warnings:
            parts.append(f"경고 {len(warnings)}건")
        return " · ".join(parts)