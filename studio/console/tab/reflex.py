# fiber.studio.console.tab.reflex
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nicegui import ui

from fiber.studio.console.runbook.bridge import runbook
from fiber.studio.console.tab.catalog import (
    SELECT_PROPS,
    STATUS_BADGE,
    RunbookCatalogTab,
    RunbookViewer,
    switch_console_tab,
)
import fiber.studio.console.ui.kit as kit
from fiber.studio.console.ui.kit import StudioTab
from fiber.studio.runbook.spec import is_write_unit


def _format_source_label(raw_key: str, node_names: dict[str, str]) -> str:
    if raw_key.startswith("$input."):
        return f"$input · {raw_key.removeprefix('$input.')}"
    head, sep, tail = raw_key.partition(".")
    if sep and head in node_names:
        return f"{head} {node_names[head]} · {tail}"
    return raw_key


class RunbookDraftTab(StudioTab):
    layer = "runbook"
    title = "Draft"
    left_header = "Draft Controls"
    right_header = "Viewer · Draft"
    ratio = 44

    def __init__(self, *, on_state_mutated: Callable[[], None] | None = None) -> None:
        self._on_state_mutated = on_state_mutated
        self._origin = "loaded at start"
        self.dry_mode = "HYBRID"
        self.sample = ""
        self._ensure_draft_ready()

        self._viewer: RunbookViewer | None = None
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
            if self._on_state_mutated:
                self._on_state_mutated()

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
            self._viewer_tag.set(f"showing dry-run ({self.dry_mode}) · {self.sample}", "amber")
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
        if self._on_state_mutated:
            self._on_state_mutated()

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

        kit.section("Step Wiring & Config")
        self._binding_box = ui.column().classes("w-full gap-1.5")
        kit.section("Flow $input")
        self._inputs_box = ui.column().classes("w-full gap-1")

        kit.section("Dry Run & Pin")
        self._sample_box = ui.row().classes("w-full items-center justify-between gap-2 no-wrap")
        self._plan_box = ui.column().classes("w-full gap-0.5 py-1")
        with kit.row(gap="gap-1.5"):
            kit.button("Run Dry", self._dry, color="cyan", flex=True)
            kit.button("Pin Release →", self._open_pin, color="green")
        self._render_controls()

    def _render_controls(self) -> None:
        self._ensure_draft_ready()
        d = runbook.draft
        node_names = {n["no"]: n["name"] for n in d["nodes"]}
        self._rebuild_open_menu()

        if self._binding_box is not None:
            self._binding_box.clear()
            with self._binding_box:
                for n in d["nodes"]:
                    with ui.column().classes("tui-row w-full gap-1 py-1.5 px-1"):
                        kit.tag(f"{n['no']} {n['name']}", "fg", framed=False).classes("font-bold")
                        for key, src in n["in"].items():
                            avail = runbook.available_keys(n["no"])
                            opts = {"": "(select source...)", **{k: _format_source_label(k, node_names) for k in avail}}
                            if src and src not in opts:
                                opts[src] = _format_source_label(src, node_names)
                            with ui.row().classes("w-full items-center gap-1.5 no-wrap pl-2"):
                                kit.tag(f"{key} ←", "muted", framed=False).classes("w-24 truncate")
                                ui.select(
                                    opts,
                                    value=src if src in opts else "",
                                    on_change=lambda e, no=n["no"], k=key: (
                                        runbook.set_binding(no, k, e.value),
                                        self._render_controls(),
                                        self._changed(),
                                    ),
                                ).props(SELECT_PROPS).classes("flex-1 text-xs")
                                if not src:
                                    kit.tag("[UNBOUND]", "red")
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
        with kit.bar():
            self._viewer_tag = kit.tag("showing draft spec", "muted", framed=False)
            kit.button("Show Spec", self._changed, color="muted", flat=True, size="xs")
        self._viewer = RunbookViewer(min_height="460px")
        self._changed()


class RunbookTriggerTab(StudioTab):
    layer = "runbook"
    title = "Trigger"
    left_header = "Pinned Runbooks & Trigger"
    right_header = "Viewer · Execution Trace"
    ratio = 44

    def __init__(self, *, on_state_mutated: Callable[[], None] | None = None) -> None:
        self._on_state_mutated = on_state_mutated
        active = runbook.list_active()
        self.flow: str | None = active[0]["flow"] if active else None
        self.run_no: int | None = None
        self._form: dict[str, Any] = {}
        self._viewer: RunbookViewer | None = None
        self._showing_tag: kit.Tag | None = None
        self._list_box: ui.column | None = None
        self._trig_box: ui.column | None = None

    def focus_flow(self, flow_name: str) -> None:
        self.flow, self.run_no = flow_name, None
        self._sync_form()
        self._render_left()
        self._show()

    def _sync_form(self) -> None:
        t = runbook.triggers.get(self.flow or "", {})
        self._form = {
            "kind": t.get("kind", "manual"),
            "cron": t.get("cron", "0 9 * * 1-5"),
            "source": t.get("source", "webhook"),
            "on": bool(t.get("on", False)),
        }

    def refresh_state(self) -> None:
        active = runbook.list_active()
        if active and (self.flow is None or all(r["flow"] != self.flow for r in active)):
            self.flow = active[0]["flow"]
        self._sync_form()
        self._render_left()
        self._show()

    def _show(self) -> None:
        if self._viewer is None or not self.flow:
            return
        runs = runbook.list_runs(self.flow)
        if runs and not any(r["no"] == self.run_no for r in runs):
            self.run_no = runs[0]["no"]
        cur = next((r for r in runs if r["no"] == self.run_no), None)
        if self._showing_tag is not None:
            lbl = f"showing run #{cur['no']} · [{cur['status'].upper()}]" if cur else f"{self.flow} · (no runs yet)"
            self._showing_tag.set(lbl, "cyan" if cur else "muted")
        self._viewer.show(runbook.run_model(self.flow, run_no=self.run_no))

    def _save_trigger(self) -> None:
        if not self.flow:
            return
        runbook.configure_trigger(self.flow, **self._form)
        ui.notify(f"[OK] saved {self._form['kind']} trigger for '{self.flow}'", type="positive")
        self._render_left()
        if self._on_state_mutated:
            self._on_state_mutated()

    def _fire(self) -> None:
        if not self.flow:
            return
        self.run_no = runbook.fire(self.flow)
        top = (runbook.list_runs(self.flow) or [{"status": "ok", "note": ""}])[0]
        tone = {"ok": "positive", "dry": "warning", "paused": "warning", "fail": "negative"}.get(top["status"], "info")
        ui.notify(f"[{top['status'].upper()}] run #{self.run_no} · {top['note']}", type=tone)
        self._render_left()
        self._show()
        if self._on_state_mutated:
            self._on_state_mutated()

    def _approve(self, run_no: int) -> None:
        if not self.flow:
            return
        try:
            runbook.resume(self.flow, run_no)
        except Exception as exc:
            ui.notify(f"[ERR] {exc}", type="negative")
            return
        self.run_no = run_no
        ui.notify(f"[OK] run #{run_no} approved & resumed", type="positive")
        self._render_left()
        self._show()
        if self._on_state_mutated:
            self._on_state_mutated()

    def compose_left(self) -> None:
        self._sync_form()
        self._list_box = ui.column().classes("w-full gap-1")
        self._trig_box = ui.column().classes("w-full gap-1.5")
        self._render_left()

    def _render_left(self) -> None:
        active = runbook.list_active()
        if self._list_box is not None:
            self._list_box.clear()
            with self._list_box:
                if not active:
                    kit.tag("(no pinned runbooks — pin in [1] Draft first)", "muted", framed=False)
                for r in active:
                    text, tone = STATUS_BADGE.get(r["status"], STATUS_BADGE["none"])
                    with kit.bar() as row:
                        row.classes("cursor-pointer" + (" tui-border-cyan" if r["flow"] == self.flow else ""))
                        row.on("click", lambda _, f=r["flow"]: self.focus_flow(f))
                        with ui.row().classes("items-center gap-1.5 no-wrap"):
                            kit.tag(f"{r['flow']} v{r['version']}", "fg", framed=False).classes("font-bold")
                            kit.tag(r["trigger"], "muted", framed=False)
                            if r["stale"]:
                                kit.tag("[RECOMPILE]", "amber")
                        kit.tag(text, tone, framed=False)

        if self._trig_box is None or not self.flow or self.flow not in runbook.triggers:
            return
        self._trig_box.clear()
        if not self._form:
            self._sync_form()
        t = runbook.triggers[self.flow]
        kind = self._form["kind"]
        runs = runbook.list_runs(self.flow)
        if runs and not any(r["no"] == self.run_no for r in runs):
            self.run_no = runs[0]["no"]
        paused = next((r for r in runs if r["status"] == "paused"), None)

        with self._trig_box:
            kit.section("Trigger Mode")
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                ui.radio(
                    ["manual", "cron", "event"],
                    value=kind,
                    on_change=lambda e: (self._form.__setitem__("kind", e.value), self._render_left()),
                ).props("inline dense dark color=info").classes("text-xs")
                kit.switch("LIVE ON", value=self._form["on"]).on_value_change(
                    lambda e: (self._form.__setitem__("on", e.value), self._save_trigger())
                )

            if kind == "cron":
                with ui.row().classes("w-full items-center gap-1.5 no-wrap"):
                    ui.input(
                        "cron schedule",
                        value=self._form["cron"],
                        on_change=lambda e: self._form.__setitem__("cron", e.value or ""),
                    ).props("dark outlined dense square").classes("flex-1 text-xs font-mono")
                    kit.tag(f"{t['tz']} · next {runbook.next_run(self.flow)}", "muted", framed=False)
            elif kind == "event":
                ui.input(
                    "event source",
                    value=self._form["source"],
                    on_change=lambda e: self._form.__setitem__("source", e.value or "webhook"),
                ).props("dark outlined dense square").classes("w-full text-xs font-mono")

            with kit.row(gap="gap-1.5"):
                if kind == "manual":
                    kit.button("[▶ RUN NOW]", self._fire, color="green", flex=True)
                else:
                    kit.button("[SAVE TRIGGER]", self._save_trigger, color="cyan", flex=True)
                    kit.button("[▷ Test Run]", self._fire, color="muted", outline=True)

            if paused:
                with kit.bar():
                    kit.tag(f"Run #{paused['no']} approval pending", "amber", framed=False)
                    kit.button(f"[✓ APPROVE #{paused['no']}]", lambda n=paused["no"]: self._approve(n), color="amber")

            kit.section("Run History (click to inspect)")
            if not runs:
                kit.tag("(no runs recorded yet)", "muted", framed=False)
            for r in runs[:8]:
                st_txt, st_tone = STATUS_BADGE.get(r["status"], STATUS_BADGE["none"])
                with kit.bar() as r_row:
                    r_row.classes("cursor-pointer" + (" tui-border-cyan" if r["no"] == self.run_no else ""))
                    r_row.on("click", lambda _, n=r["no"]: (setattr(self, "run_no", n), self._render_left(), self._show()))
                    with ui.row().classes("w-full items-center justify-between gap-2 no-wrap text-xs"):
                        kit.tag(f"#{r['no']} · {r['at']} · {r['by']}", "fg", framed=False)
                        kit.tag(st_txt, st_tone, framed=False)

    def compose_right(self) -> None:
        with kit.bar():
            self._showing_tag = kit.tag("showing latest run", "muted", framed=False)
            kit.button("[tui|doc]", lambda: self._viewer and self._viewer.toggle_theme(), color="muted", flat=True, size="xs")
        self._viewer = RunbookViewer(min_height="460px")
        self._show()


def build_tabs() -> list[StudioTab]:
    trig_tab: RunbookTriggerTab
    draft_tab: RunbookDraftTab
    cat_tab = RunbookCatalogTab(
        on_edit_draft=lambda f, v: draft_tab.load_external(f, v),
        on_open_trigger=lambda f: trig_tab.focus_flow(f),
    )
    trig_tab = RunbookTriggerTab(on_state_mutated=cat_tab.refresh_state)
    draft_tab = RunbookDraftTab(on_state_mutated=lambda: (cat_tab.refresh_state(), trig_tab.refresh_state()))
    return [cat_tab, draft_tab, trig_tab]