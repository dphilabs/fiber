# fiber.studio.console.runbook.draft
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nicegui import ui

from fiber.studio.console.bridge.runbook import runbook
from fiber.studio.console.runbook.catalog import SELECT_PROPS
import fiber.studio.console.ui.kit as kit
from fiber.studio.console.ui.kit import StudioTab
from fiber.node.reflex.runbook.spec import is_write_unit
from fiber.studio.console.runbook.frame.loader import DagViewer, DocViewer, build_dag_model

class RunbookDraftTab(StudioTab):
    layer = "runbook"
    title = "Draft"
    left_header = "Draft Controls"
    right_header = "DAG · Viewer"
    ratio = 44

    def __init__(self, *, on_state_mutated: Callable[[], None] | None = None) -> None:
        self._on_state_mutated = on_state_mutated
        self._origin = "loaded at start"
        self.dry_mode = "HYBRID"
        self.sample = ""
        self._ensure_draft_ready()

        self._dag: DagViewer | None = None
        self._viewer: DocViewer | None = None
        self._viewer_tag: kit.Tag | None = None
        self._name: kit.Tag | None = None
        self._origin_tag: kit.Tag | None = None
        self._dirty_tag: kit.Tag | None = None
        self._save_btn: ui.button | None = None
        self._open_menu: ui.menu | None = None
        self._binding_box: ui.column | None = None
        self._inputs_box: ui.column | None = None
        self._sample_box: ui.row | None = None
        self._plan_box: ui.column | None = None
        self._pin_dlg: ui.dialog | None = None
        self._imp_dlg: ui.dialog | None = None

    def _ensure_draft_ready(self) -> None:
        d = runbook.draft
        if not d.get("flow"):
            tpls = runbook.list_templates()
            if tpls:
                tname = tpls[0]["name"]
                runbook.new_draft(from_template=tname)
                runbook.save_draft()
                self._origin = f"from template · {tname}"
                d = runbook.draft
        samples = runbook.list_samples(d["flow"]) if d.get("flow") else []
        if samples and self.sample not in samples:
            self.sample = samples[0]

    def load_external(self, flow_name: str, version: int | None = None) -> None:
        if version is not None:
            if runbook.draft.get("flow") != flow_name:
                runbook.new_draft(from_template=flow_name)
            runbook.new_draft(from_version=version)
            self._origin = f"from pinned · {flow_name} v{version}"
        else:
            runbook.new_draft(from_template=flow_name)
            self._origin = f"from template · {flow_name}"
        self._ensure_draft_ready()
        self._render_controls()
        self._changed()

    def _mutated(self) -> None:
        if self._on_state_mutated:
            self._on_state_mutated()

    def _sync_header(self) -> None:
        d = runbook.draft
        is_dirty = runbook.dirty
        if self._name is not None:
            self._name.set(f"{d['flow']} · target v{d['version']}" if d.get("flow") else "(no draft)")
        if self._origin_tag is not None:
            self._origin_tag.set(f"[{self._origin}]", "cyan")
        if self._dirty_tag is not None:
            self._dirty_tag.set("[● UNSAVED]" if is_dirty else "[SAVED]", "amber" if is_dirty else "muted")
        if self._save_btn is not None:
            self._save_btn.props(f"color={'warning' if is_dirty else 'positive'} {'unelevated' if is_dirty else 'outline'}")

    def _changed(self) -> None:
        self._sync_header()
        fname = runbook.draft.get("flow", "")
        if self._viewer_tag is not None:
            self._viewer_tag.set(f"showing draft spec · {fname}", "muted")
        if self._viewer is not None and fname:
            self._viewer.show(runbook.project(fname))

    def _rebuild_open_menu(self) -> None:
        if self._open_menu is None:
            return
        self._open_menu.clear()
        cur = runbook.draft.get("flow", "")
        with self._open_menu:
            for t in runbook.list_templates():
                ui.menu_item(f"Template · {t['name']}", on_click=lambda n=t["name"]: self.load_external(n, None))
            if cur and runbook.list_versions(cur):
                ui.separator()
                for v in reversed(runbook.list_versions(cur)):
                    ui.menu_item(f"Pinned · {cur} v{v}", on_click=lambda v=v, f=cur: self.load_external(f, v))
            ui.separator()
            ui.menu_item("Import package (.json)...", on_click=self._open_import)

    def _open_import(self) -> None:
        assert self._imp_dlg is not None
        self._imp_dlg.clear()
        buf = {"text": ""}

        def _apply() -> None:
            if not buf["text"].strip():
                ui.notify("[ERR] paste package JSON first", type="negative")
                return
            try:
                res = runbook.import_package(buf["text"])
            except Exception as exc:
                ui.notify(f"[ERR] import failed: {exc}", type="negative")
                return
            assert self._imp_dlg is not None
            self._imp_dlg.close()
            self._origin = f"imported · triggers {res['triggers']}"
            ui.notify(f"[OK] imported '{res['flow']}' · compile {res['compile']}", type="positive")
            self._render_controls()
            self._changed()
            self._mutated()

        with self._imp_dlg, ui.card().classes("tui-modal-shell w-[480px] p-0 pt-2.5 gap-0"):
            with kit.box(title="Import Runbook Package (.json)", flex=False, width="w-full gap-2"):
                ui.textarea(
                    label="Paste .runbook.json content",
                    on_change=lambda e: buf.__setitem__("text", e.value or ""),
                ).props("dark outlined dense square rows=8").classes("w-full text-xs font-mono")
                with ui.row().classes("w-full justify-end gap-2"):
                    kit.button("Cancel", self._imp_dlg.close, color="muted", flat=True)
                    kit.button("Import to Draft", _apply, color="cyan")
        self._imp_dlg.open()

    def _save(self) -> None:
        runbook.save_draft()
        ui.notify("[OK] draft saved", type="positive")
        self._sync_header()

    def _dag_model(self) -> dict[str, Any]:
        return build_dag_model(runbook.draft, runbook.available_keys)

    def _on_bind(self, no: str, key: str, src: str) -> dict[str, Any]:
        """Slot source chosen in the DAG viewer. Errors propagate: the viewer notifies and reverts."""
        runbook.set_binding(no, key, src)
        self._render_controls()
        self._changed()
        return self._dag_model()

    def _patch_cfg(self, step_no: str, key: str, val: Any) -> None:
        try:
            runbook.patch(step_no, cfg={key: val})
        except ValueError as exc:
            ui.notify(f"[TYPE?] {key}: {exc}", type="warning")
            return
        self._changed()

    def _dry(self) -> None:
        d = runbook.draft
        # HYBRID passes `{}` so read steps run LIVE while write steps auto-skip; FIXTURE forces all to FIXTURE.
        sources = (
            {n["no"]: "FIXTURE" for n in d["nodes"] if n["kind"] != "gate"}
            if self.dry_mode == "FIXTURE"
            else {}
        )
        if self._viewer_tag is not None:
            self._viewer_tag.set(f"showing dry-run ({self.dry_mode}) · {self.sample} · run으로 남지 않음", "amber")
        if self._viewer is not None:
            self._viewer.show(runbook.dryrun(self.sample, sources))

    def _open_pin(self) -> None:
        try:
            receipt = runbook.pin()
        except ValueError as exc:
            ui.notify(f"[ERR] cannot pin: {exc}", type="negative")
            return
        assert self._pin_dlg is not None
        self._pin_dlg.clear()
        with self._pin_dlg, ui.card().classes("tui-modal-shell w-[420px] p-0 pt-2.5 gap-0"):
            with kit.box(title=f"Pin {receipt['flow']} v{receipt['version']}", flex=False, width="w-full gap-2"):
                with ui.row().classes("items-center gap-1.5"):
                    kit.tag("[OK] compile", "green")
                    kit.tag("[OK] asl verified", "green")
                kit.tag(receipt["summary"], "fg", framed=False)
                kit.tag(receipt["keep"], "muted", framed=False)
                with ui.row().classes("w-full justify-end gap-2"):
                    kit.button("Cancel", self._pin_dlg.close, color="muted", flat=True)
                    kit.button("Confirm Pin", lambda r=receipt: self._confirm_pin(r), color="green")
        self._pin_dlg.open()

    def _confirm_pin(self, receipt: dict[str, Any]) -> None:
        runbook.confirm_pin(receipt)
        if self._pin_dlg is not None:
            self._pin_dlg.close()
        self._origin = f"pinned v{receipt['version']}"
        self._rebuild_open_menu()
        ui.notify(f"[OK] pinned {receipt['flow']} v{receipt['version']}", type="positive")
        self._changed()
        self._mutated()

    def _render_dry_plan(self) -> None:
        if self._plan_box is None:
            return
        self._plan_box.clear()
        with self._plan_box:
            for n in runbook.draft.get("nodes", []):
                if n["kind"] == "gate":
                    desc, tone = "gate 조건 평가 (실행 대상 아님)", "muted"
                elif self.dry_mode == "FIXTURE":
                    desc, tone = "[FIXTURE] 기록된 픽스처 / 미리보기 대체", "amber"
                elif is_write_unit(n["kind"], n["name"]):
                    desc, tone = "[DRY] 외부 쓰기 자동 차단", "amber"
                else:
                    desc, tone = "[LIVE] 실제 유닛 실행", "green"
                with ui.row().classes("w-full items-center justify-between no-wrap text-xs px-1"):
                    kit.tag(f"{n['no']} {n['name']}", "fg", framed=False)
                    kit.tag(desc, tone, framed=False)

    def compose_left(self) -> None:
        self._pin_dlg, self._imp_dlg = ui.dialog(), ui.dialog()
        with kit.bar():
            with ui.column().classes("gap-0.5"):
                self._name = kit.tag("", "fg", framed=False).classes("font-bold")
                with ui.row().classes("items-center gap-1.5 no-wrap"):
                    self._origin_tag = kit.tag("", "cyan", framed=False)
                    self._dirty_tag = kit.tag("", "muted", framed=False)
            with ui.row().classes("items-center gap-1.5 no-wrap"):
                with kit.button("[OPEN ▾]", lambda: None, color="cyan", outline=True):
                    self._open_menu = ui.menu()
                self._save_btn = kit.button("[SAVE]", self._save, color="green", outline=True)

        kit.section("Step Config")
        self._binding_box = ui.column().classes("w-full gap-1.5")
        kit.section("Flow $input")
        self._inputs_box = ui.column().classes("w-full gap-1")

        kit.section("Dry Run & Pin")
        self._sample_box = ui.row().classes("w-full items-center justify-between gap-2 no-wrap")
        self._plan_box = ui.column().classes("w-full gap-0.5 py-1")
        with kit.row(gap="gap-1.5"):
            kit.button("Run Dry", self._dry, color="cyan", flex=True)
            kit.button("Pin Release →", self._open_pin, color="green")
        kit.tag("Run Dry 결과는 오른쪽 뷰어에만 보이고 run으로 남지 않습니다.", "muted", framed=False).classes("text-xs")
        self._render_controls()

    def _render_controls(self) -> None:
        self._ensure_draft_ready()
        d = runbook.draft
        self._rebuild_open_menu()
        if self._dag is not None:
            self._dag.show(self._dag_model())

        if self._binding_box is not None:
            self._binding_box.clear()
            with self._binding_box:
                cfg_nodes = [n for n in d["nodes"] if n.get("cfg")]
                if not cfg_nodes:
                    kit.tag("(cfg가 있는 단계 없음)", "muted", framed=False).classes("text-xs")
                for n in cfg_nodes:
                    with ui.column().classes("tui-row w-full gap-1 py-1.5 px-1"):
                        kit.tag(f"{n['no']} {n['name']}", "fg", framed=False).classes("font-bold")
                        for ck, cv in n.get("cfg", {}).items():
                            with ui.row().classes("w-full items-center gap-1.5 no-wrap pl-2"):
                                kit.tag(f"cfg.{ck} =", "muted", framed=False).classes("w-24 truncate")
                                ui.input(
                                    value=str(cv),
                                    on_change=lambda e, no=n["no"], k=ck: self._patch_cfg(no, k, e.value),
                                ).props('dark outlined dense square debounce="350"').classes("flex-1 text-xs font-mono")

        if self._inputs_box is not None:
            self._inputs_box.clear()
            with self._inputs_box:
                for key, spec in d["inputs"].items():
                    fixed_preview = f' (= {spec["value"]!r})' if spec["mode"] == "fixed" and "value" in spec else ""
                    with ui.row().classes("w-full items-center gap-1.5 no-wrap"):
                        kit.tag(f"{key} : {spec['shape']}{fixed_preview}", "fg", framed=False).classes("flex-1 truncate")
                        ui.select(
                            {"runtime": "runtime", "fixed": "fixed"},
                            value=spec["mode"],
                            on_change=lambda e, k=key: (
                                runbook.configure_manifest(k, e.value),
                                self._render_controls(),
                                self._changed(),
                            ),
                        ).props(SELECT_PROPS).classes("w-28 text-xs")

        if self._sample_box is not None:
            samples = runbook.list_samples(d["flow"]) if d.get("flow") else []
            if samples and self.sample not in samples:
                self.sample = samples[0]
            self._sample_box.clear()
            with self._sample_box:
                if samples:
                    kit.select(samples, "Sample", self, "sample")
                ui.toggle(
                    ["HYBRID", "FIXTURE"],
                    value=self.dry_mode,
                    on_change=lambda e: (setattr(self, "dry_mode", e.value), self._render_dry_plan()),
                ).props("dense flat square no-caps toggle-color=info text-color=grey-6").classes("text-xs")
        self._render_dry_plan()
        self._sync_header()

    def compose_right(self) -> None:
        self._dag = DagViewer(on_bind=self._on_bind, min_height="0px")
        self._dag.show(self._dag_model())
        with kit.bar():
            self._viewer_tag = kit.tag("showing draft spec", "muted", framed=False)
            kit.button("Show Spec", self._changed, color="muted", flat=True, size="xs")
        self._viewer = DocViewer(min_height="0px")
        self._changed()