# fiber.node.runbook.manifest
from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
from pathlib import Path
from typing import Any, Literal

SlotMode = Literal["fixed", "runtime", "source"]
OutputWidget = Literal["metric", "link", "file", "table", "text"]

_VALID_SLOT_MODES: set[str] = {"fixed", "runtime", "source"}
_VALID_WIDGETS: set[str] = {"metric", "link", "file", "table", "text"}
_DEFAULT_SCHEMES: tuple[str, ...] = ("upload", "local", "url", "gdrive")


@dataclass(frozen=True, slots=True)
class SlotPolicy:
    name: str
    mode: SlotMode = "runtime"
    fixed_value: Any = None
    source_uri: str | None = None
    template_schemes: tuple[str, ...] = _DEFAULT_SCHEMES
    allowed_schemes: tuple[str, ...] = ("upload", "local", "url")
    read_options: dict[str, Any] = field(default_factory=dict)
    contract_ref: str | None = None

    def is_satisfied(self) -> bool:
        """핀 검사(S2 하단) 시 개별 슬롯의 설정 충족 여부 판정."""
        if self.mode not in _VALID_SLOT_MODES:
            return False
        # 허용 소스는 반드시 템플릿 상한(template_schemes)의 부분집합이어야 함 (스펙 4.3)
        if not set(self.allowed_schemes).issubset(set(self.template_schemes)):
            return False
        if self.mode == "fixed":
            return self.fixed_value is not None
        if self.mode == "source":
            if not self.source_uri or "://" not in self.source_uri:
                return False
            scheme = self.source_uri.split("://", 1)[0]
            return scheme in self.allowed_schemes
        # runtime 모드: 실행 시 입력받으므로 최소 1개 이상의 허용 스킴 또는 일반 값 입력이 열려 있어야 함
        return len(self.allowed_schemes) > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mode": self.mode,
            "fixed_value": self.fixed_value,
            "source_uri": self.source_uri,
            "template_schemes": list(self.template_schemes),
            "allowed_schemes": list(self.allowed_schemes),
            "read_options": dict(self.read_options),
            "contract_ref": self.contract_ref,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SlotPolicy:
        return cls(
            name=str(raw["name"]),
            mode=raw.get("mode", "runtime"),
            fixed_value=raw.get("fixed_value"),
            source_uri=raw.get("source_uri"),
            template_schemes=tuple(raw.get("template_schemes", _DEFAULT_SCHEMES)),
            allowed_schemes=tuple(raw.get("allowed_schemes", ("upload", "local", "url"))),
            read_options=dict(raw.get("read_options") or {}),
            contract_ref=raw.get("contract_ref"),
        )


@dataclass(frozen=True, slots=True)
class OutputPolicy:
    key: str
    widget: OutputWidget = "text"
    show_in_result: bool = True
    include_in_notify: bool = False

    def is_valid(self) -> bool:
        return self.widget in _VALID_WIDGETS

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "widget": self.widget,
            "show_in_result": self.show_in_result,
            "include_in_notify": self.include_in_notify,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> OutputPolicy:
        return cls(
            key=str(raw["key"]),
            widget=raw.get("widget", "text"),
            show_in_result=bool(raw.get("show_in_result", True)),
            include_in_notify=bool(raw.get("include_in_notify", False)),
        )


@dataclass(slots=True)
class RunbookManifest:
    flow_name: str
    template_ref: str | None = None
    slots: dict[str, SlotPolicy] = field(default_factory=dict)
    outputs: dict[str, OutputPolicy] = field(default_factory=dict)
    milestones: set[str] = field(default_factory=set)

    def runtime_slot_names(self) -> tuple[str, ...]:
        """자동 실행 가능 판정(SchedulePlanner)의 기준이 되는 '실행 시' 슬롯 목록."""
        return tuple(name for name, pol in self.slots.items() if pol.mode == "runtime")

    def clone(self) -> RunbookManifest:
        """PinnedRelease 박제 시 이후 Draft 수정에 오염되지 않도록 깊은 사본 생성."""
        return RunbookManifest(
            flow_name=self.flow_name,
            template_ref=self.template_ref,
            slots={k: replace(v, read_options=dict(v.read_options)) for k, v in self.slots.items()},
            outputs={k: replace(v) for k, v in self.outputs.items()},
            milestones=set(self.milestones),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "flow_name": self.flow_name,
            "template_ref": self.template_ref,
            "slots": {k: v.to_dict() for k, v in self.slots.items()},
            "outputs": {k: v.to_dict() for k, v in self.outputs.items()},
            "milestones": sorted(self.milestones),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RunbookManifest:
        return cls(
            flow_name=str(raw["flow_name"]),
            template_ref=raw.get("template_ref"),
            slots={k: SlotPolicy.from_dict(v) for k, v in (raw.get("slots") or {}).items()},
            outputs={k: OutputPolicy.from_dict(v) for k, v in (raw.get("outputs") or {}).items()},
            milestones=set(raw.get("milestones") or ()),
        )


class ManifestStore:
    """S2 화면의 Draft 매니페스트 저장소 (`{storage_dir}/{flow_name}.manifest.json`)."""

    def __init__(self, storage_dir: Path | None = None) -> None:
        self.storage_dir = storage_dir
        self._cache: dict[str, RunbookManifest] = {}

    def _file_for(self, flow_name: str) -> Path | None:
        if self.storage_dir is None:
            return None
        safe_name = flow_name.replace("/", "__")
        return self.storage_dir / f"{safe_name}.manifest.json"

    def get_or_create(
        self,
        flow_name: str,
        required_entry_keys: tuple[str, ...] = (),
        exit_keys: tuple[str, ...] = (),
    ) -> RunbookManifest:
        if flow_name not in self._cache:
            path = self._file_for(flow_name)
            if path is not None and path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                self._cache[flow_name] = RunbookManifest.from_dict(raw)
            else:
                self._cache[flow_name] = RunbookManifest(flow_name=flow_name)

        manifest = self._cache[flow_name]
        for key in required_entry_keys:
            manifest.slots.setdefault(key, SlotPolicy(name=key, mode="runtime"))
        for out_key in exit_keys:
            manifest.outputs.setdefault(out_key, OutputPolicy(key=out_key))
        return manifest

    def save(self, flow_name: str) -> Path | None:
        manifest = self._cache.get(flow_name)
        path = self._file_for(flow_name)
        if manifest is None or path is None:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return path

    def inspect(
        self,
        flow_name: str,
        required_entry_keys: tuple[str, ...] = (),
        *,
        valid_step_names: tuple[str, ...] | None = None,
    ) -> tuple[RunbookManifest, bool, bool]:
        """
        ReleaseStudio.dryrun() 및 pin()에서 호출.
        - slots_ok: 모든 필수 입력 슬롯이 분류되었고 각 모드별 값/스킴 조건을 만족하는가
        - milestone_ok: 지정된 ★ 마일스톤들이 실제 플로우 스텝 이름 안에 존재하는가
        """
        manifest = self.get_or_create(flow_name, required_entry_keys)

        slots_ok = all(
            key in manifest.slots and manifest.slots[key].is_satisfied()
            for key in required_entry_keys
        ) and all(pol.is_satisfied() for pol in manifest.slots.values())

        outputs_ok = all(pol.is_valid() for pol in manifest.outputs.values())
        milestone_ok = outputs_ok and (
            valid_step_names is None or manifest.milestones.issubset(set(valid_step_names))
        )

        # pin() 시점에 박제해도 이후 Draft 편집과 격리되도록 clone() 반환 가능하도록 정합성 유지
        return manifest.clone(), slots_ok, milestone_ok

    def patch(self, flow_name: str, patch_ops: dict[str, Any], *, auto_save: bool = True) -> RunbookManifest:
        """
        S2 설정 조작 반영:
        patch_ops 예시:
        {
            "template_ref": "quote_to_sheet@a1f9",
            "slots": {"sheet_id": {"mode": "fixed", "fixed_value": "1AbX..."}},
            "outputs": {"rows_written": {"widget": "metric", "include_in_notify": True}},
            "milestones": ["sheet.read", "sheet.write"],
        }
        """
        manifest = self.get_or_create(flow_name)

        if "template_ref" in patch_ops:
            manifest.template_ref = patch_ops["template_ref"]

        for name, cfg in (patch_ops.get("slots") or {}).items():
            prev = manifest.slots.get(name, SlotPolicy(name=name))
            merged = {**prev.to_dict(), **cfg, "name": name}
            updated = SlotPolicy.from_dict(merged)
            if not set(updated.allowed_schemes).issubset(set(updated.template_schemes)):
                raise ValueError(
                    f"Slot '{name}' allowed_schemes {updated.allowed_schemes} "
                    f"exceeds template ceiling {updated.template_schemes}."
                )
            manifest.slots[name] = updated

        for key, cfg in (patch_ops.get("outputs") or {}).items():
            prev_out = manifest.outputs.get(key, OutputPolicy(key=key))
            merged_out = {**prev_out.to_dict(), **cfg, "key": key}
            manifest.outputs[key] = OutputPolicy.from_dict(merged_out)

        if "milestones" in patch_ops:
            manifest.milestones = set(patch_ops["milestones"])

        if auto_save:
            self.save(flow_name)
        return manifest.clone()