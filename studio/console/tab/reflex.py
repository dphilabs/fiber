# fiber.studio.console.tab.reflex
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nicegui import ui

from fiber.studio.console.runbook.bridge import runbook
import fiber.studio.console.ui.kit as kit
from fiber.studio.console.ui.kit import StudioTab

_VIEWER_PATH = Path(__file__).resolve().parents[1] / "ui" / "component" / "viewer.html"
_VIEWER_HTML = _VIEWER_PATH.read_text(encoding="utf-8") if _VIEWER_PATH.is_file() else ""

_STATUS: dict[str, tuple[str, str]] = {
    "ok": ("[OK]", "green"),
    "fail": ("[FAIL]", "red"),
    "paused": ("[PAUSED]", "amber"),
    "run": ("[~ RUN]", "cyan"),
    "none": ("[ - ]", "muted"),
}
_SOURCES = ("LIVE", "FIXTURE", "REPLAY", "DRY")
_SELECT_PROPS = "dark outlined dense square options-dense"
_TPL = "tpl:"


class _Viewer:
    def __init__(self) -> None:
        self.theme = "tui"
        self.model: dict[str, Any] | None = None
        self.frame = ui.element("iframe").classes("w-full flex-1 min-h-0 border-0").style("min-height: 360px")

    def show(self, model: dict[str, Any]) -> None:
        self.model = model
        data = json.dumps(model, ensure_ascii=False).replace("</", "<\\/")
        html = (
            _VIEWER_HTML.replace('data-theme="doc"', f'data-theme="{self.theme}"', 1)
            .replace("/*__MODEL__*/null", data, 1)
        )
        self.frame._props["srcdoc"] = html
        self.frame.update()

    def toggle_theme(self) -> None:
        self.theme = "doc" if self.theme == "tui" else "tui"
        if self.model is not None:
            self.show(self.model)

    def print(self) -> None:
        ui.run_javascript(f"getHtmlElement({self.frame.id}).contentWindow.print()")


class RunbookListTab(StudioTab):
    layer = "runbook"
    title = "List"

    _COLS = (
        ("flow", "w-48"),
        ("desc", "flex-1"),
        ("seed", "w-12"),
        ("fixture", "w-16"),
        ("pin", "w-12"),
        ("trig", "w-16"),
        ("last", "w-20"),
    )

    def __init__(self) -> None:
        self.mode = "ALL"
        self.query = ""
        self.flow: str | None = None
        self._list_box: ui.column | None = None
        self._status: kit.Tag | None = None
        self._viewer: _Viewer | None = None

    def compose_left(self) -> None:  # full-width tab: render() is overridden
        pass

    def compose_right(self) -> None:
        pass

    def _rows(self) -> list[dict[str, Any]]:
        q = self.query.strip().lower()
        rows = runbook.list_catalog()
        if self.mode == "PINNED":
            rows = [r for r in rows if r["pin"] is not None]
        elif self.mode == "TEMPLATE":
            rows = [r for r in rows if r["src"] and r["pin"] is None]
        return [r for r in rows if not q or q in r["flow"].lower() or q in r["desc"].lower()]

    def _reload(self) -> None:
        runbook.repo.reload()
        if self._status is not None:
            self._status.set(*runbook.repo_status())
        self._render_list()
        self._show()
        ui.notify("[OK] catalog reloaded", type="positive")

    def _select(self, flow: str) -> None:
        self.flow = flow
        self._render_list()
        self._show()

    def _show(self) -> None:
        if self._viewer is None or self.flow is None:
            return
        is_pinned = bool(runbook.list_versions(self.flow))
        if self.mode != "TEMPLATE" and is_pinned:
            self._viewer.show(runbook.run_model(self.flow))
        elif runbook.repo.flow(self.flow) is not None:
            self._viewer.show(runbook.template_model(self.flow))
        else:
            self._viewer.show(runbook.run_model(self.flow))

    def _render_list(self) -> None:
        if self._list_box is None:
            return
        self._list_box.clear()
        with self._list_box:
            with ui.row().classes("w-full items-center gap-2 no-wrap px-2"):
                for name, width in self._COLS:
                    kit.tag(name, "muted", framed=False).classes(width)
            rows = self._rows()
            if not rows:
                kit.tag("(no flows)", "muted", framed=False).classes("px-2")
            for r in rows:
                text, tone = _STATUS.get(r["status"], _STATUS["none"]) if r["status"] else ("-", "muted")
                with kit.bar() as row:
                    row.classes("cursor-pointer" + (" tui-border-cyan" if r["flow"] == self.flow else ""))
                    row.on("click", lambda _, f=r["flow"]: self._select(f))
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        with ui.row().classes("w-48 items-center gap-1 no-wrap"):
                            kit.tag(r["flow"], "fg", framed=False).classes("font-bold truncate")
                            if not r["src"]:
                                kit.tag("[NO SRC]", "amber")
                        kit.tag(r["desc"] or "-", "muted", framed=False).classes("flex-1 truncate min-w-0")
                        kit.tag(str(r["seeds"]) if r["seeds"] else "-", "fg", framed=False).classes("w-12")
                        kit.tag("Y" if r["fixture"] else "-", "green" if r["fixture"] else "muted", framed=False).classes("w-16")
                        kit.tag(f"v{r['pin']}" if r["pin"] else "-", "cyan" if r["pin"] else "muted", framed=False).classes("w-12")
                        kit.tag(r["trigger"] or "-", "muted", framed=False).classes("w-16")
                        kit.tag(text, tone, framed=False).classes("w-20")
                if r["status"] == "fail" and r["note"]:
                    kit.tag(f"└ {r['note']}", "red", framed=False).classes("pl-5 -mt-1")

    def render(self) -> ui.column:
        with ui.column().classes("w-full gap-2") as col:
            with kit.bar():
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.toggle(
                        ["ALL", "PINNED", "TEMPLATE"],
                        value=self.mode,
                        on_change=lambda e: (setattr(self, "mode", e.value), self._render_list(), self._show()),
                    ).props("dense flat square no-caps toggle-color=info text-color=grey-6").classes("text-xs")
                    ui.input(
                        "filter",
                        on_change=lambda e: (setattr(self, "query", e.value or ""), self._render_list()),
                    ).props("dark outlined dense square clearable").classes("w-56 text-xs")
                with ui.row().classes("items-center gap-1.5 no-wrap"):
                    self._status = kit.tag(*runbook.repo_status())
                    kit.button("[RELOAD]", self._reload, color="cyan", outline=True)
            self._list_box = ui.column().classes("w-full gap-1")
            with ui.column().classes("w-full gap-0").style("height: 380px"):
                self._viewer = _Viewer()
        self._render_list()
        return col


class RunbookViewTab(StudioTab):
    layer = "runbook"
    title = "View"

    def __init__(self) -> None:
        opts = self._flow_options()
        self.flow: str | None = next(iter(opts), None)
        self.version: int | None = None
        self._viewer: _Viewer | None = None
        self._flow_select: ui.select | None = None
        self._ver_select: ui.select | None = None

    def compose_left(self) -> None:  # full-width tab: render() is overridden
        pass

    def compose_right(self) -> None:
        pass

    def _versions(self) -> dict[int, str]:
        if not self.flow or self.flow.startswith(_TPL):
            return {}
        return {v: f"v{v}" for v in runbook.list_versions(self.flow)}

    @staticmethod
    def _flow_options() -> dict[str, str]:
        opts = {r["flow"]: r["flow"] for r in runbook.list_active()}
        if runbook.repo.enabled:
            for t in runbook.list_templates():
                opts[_TPL + t["name"]] = f"tpl · {t['name']}"
        return opts

    def _on_flow(self, flow: str | None) -> None:
        self.flow = flow
        versions = self._versions()
        self.version = max(versions) if versions else None
        if self._ver_select is not None:
            self._ver_select.set_options(versions, value=self.version)
            self._ver_select.set_enabled(bool(versions))
        self._refresh()

    def _on_version(self, version: int | None) -> None:
        self.version = version
        self._refresh()

    def _refresh(self) -> None:
        if self._viewer is None or not self.flow:
            return
        if self.flow.startswith(_TPL):
            self._viewer.show(runbook.template_model(self.flow[len(_TPL) :]))
        else:
            ver = self.version or (max(v) if (v := self._versions()) else None)
            self._viewer.show(runbook.project(self.flow, ver))

    def _export(self) -> None:
        if not self.flow or self.flow.startswith(_TPL):
            ui.notify("[INFO] templates are read-only: create a draft, then export")
            return
        versions = runbook.list_versions(self.flow)
        if not versions:
            ui.notify("[INFO] no pinned version available to export")
            return
        ver = self.version or max(versions)
        ui.download(runbook.export_package(self.flow, ver), f"{self.flow}.v{ver}.runbook.json")

    def render(self) -> ui.column:
        opts = self._flow_options()
        if self.flow not in opts:
            self.flow = next(iter(opts), None)
        versions = self._versions()
        self.version = max(versions) if versions else None

        with ui.column().classes("w-full gap-2") as col:
            with kit.bar():
                with ui.row().classes("items-center gap-2 no-wrap"):
                    self._flow_select = (
                        ui.select(opts, value=self.flow, label="Flow", on_change=lambda e: self._on_flow(e.value))
                        .props(_SELECT_PROPS)
                        .classes("w-56 text-xs")
                    )
                    self._ver_select = (
                        ui.select(versions, value=self.version, label="Version", on_change=lambda e: self._on_version(e.value))
                        .props(_SELECT_PROPS)
                        .classes("w-24 text-xs")
                    )
                    self._ver_select.set_enabled(bool(versions))
                    kit.tag(*runbook.repo_status())
                with ui.row().classes("items-center gap-1.5 no-wrap"):
                    kit.button("[tui|doc]", lambda: self._viewer and self._viewer.toggle_theme(), color="muted", flat=True, size="xs")
                    kit.button("[EXPORT]", self._export, color="cyan", outline=True)
                    kit.button("[PDF]", lambda: self._viewer and self._viewer.print(), color="cyan", outline=True)
            with ui.column().classes("w-full gap-0").style("height: 560px"):
                self._viewer = _Viewer()
        self._refresh()
        return col


class RunbookDraftTab(StudioTab):
    layer = "runbook"
    title = "Draft"
    left_header = "Draft Controls"
    right_header = "Viewer · Draft"
    ratio = 42

    _DEFAULT_SOURCES = {"01": "FIXTURE", "04": "REPLAY", "05": "DRY"}

    def __init__(self) -> None:
        samples = runbook.list_samples(runbook.draft["flow"])
        self.sample: str = samples[0] if samples else ""
        self._sources: dict[str, str] = {}
        self._sync_node_sources()

        self._viewer: _Viewer | None = None
        self._name: kit.Tag | None = None
        self._dirty: kit.Tag | None = None
        self._new_menu: ui.menu | None = None
        self._binding_box: ui.column | None = None
        self._inputs_box: ui.column | None = None
        self._sample_box: ui.row | None = None
        self._sources_box: ui.row | None = None
        self._pin_dlg: ui.dialog | None = None

    def _sync_node_sources(self) -> None:
        nodes = [n["no"] for n in runbook.draft.get("nodes", []) if n.get("kind") != "gate"]
        self._sources = {
            no: self._sources.get(no, self._DEFAULT_SOURCES.get(no, "LIVE"))
            for no in nodes
        }

    # actions
    def _changed(self) -> None:
        self._sync_header()
        if self._viewer is not None:
            self._viewer.show(runbook.project(runbook.draft["flow"]))

    def _sync_header(self) -> None:
        d = runbook.draft
        if self._name is not None:
            self._name.set(f"DRAFT {d['flow']} v{d['version']}")
        if self._dirty is not None:
            self._dirty.set("[*]" if runbook.dirty else "", "amber")

    def _rebuild_new_menu(self) -> None:
        if self._new_menu is None:
            return
        self._new_menu.clear()
        with self._new_menu:
            for t in runbook.list_templates():
                ui.menu_item(f"from template · {t['name']}", on_click=lambda n=t["name"]: self._new(from_template=n))
            for v in runbook.list_versions(runbook.draft["flow"]):
                ui.menu_item(f"from version · {runbook.draft['flow']} v{v}", on_click=lambda v=v: self._new(from_version=v))

    def _new(self, **origin: Any) -> None:
        runbook.new_draft(**origin)
        self._sources.clear()
        self._sync_node_sources()
        self._render_controls()
        self._changed()

    def _import(self) -> None:
        res = runbook.import_package(json.dumps({"flow": "imported_runbook"}))
        ui.notify(
            f"[MOCK] import -> draft · compile {res['compile']} · {res['stale_units']} unit changed · triggers {res['triggers']}"
        )
        self._sync_node_sources()
        self._render_controls()
        self._changed()

    def _save(self) -> None:
        runbook.save_draft()
        ui.notify("[MOCK] draft saved", type="positive")
        self._changed()

    def _dry(self) -> None:
        if self._viewer is not None:
            self._viewer.show(runbook.dryrun(self.sample, self._sources))

    def _open_pin(self) -> None:
        try:
            receipt = runbook.pin()
        except ValueError as exc:
            ui.notify(f"[ERR] cannot pin: {exc}", type="negative")
            return
        assert self._pin_dlg is not None
        self._pin_dlg.clear()
        with self._pin_dlg, ui.card().classes("tui-modal-shell w-[420px] p-0 pt-2.5 gap-0"):
            with kit.box(title=f"Pin v{receipt['version']}", flex=False, width="w-full gap-2"):
                with ui.row().classes("items-center gap-1.5"):
                    kit.tag("[OK] compile", "green")
                    kit.tag("[OK] fixture", "green")
                kit.tag(receipt["summary"], "fg", framed=False)
                kit.tag(receipt["keep"], "muted", framed=False)
                with ui.row().classes("w-full justify-end gap-2"):
                    kit.button("Cancel", self._pin_dlg.close, color="muted", flat=True)
                    kit.button("Pin", lambda r=receipt: self._confirm_pin(r), color="green")
        self._pin_dlg.open()

    def _confirm_pin(self, receipt: dict[str, Any]) -> None:
        runbook.confirm_pin(receipt)
        if self._pin_dlg is not None:
            self._pin_dlg.close()
        self._rebuild_new_menu()
        ui.notify(f"[MOCK] pinned v{receipt['version']}", type="positive")
        self._changed()

    # layout
    def compose_left(self) -> None:
        self._pin_dlg = ui.dialog()
        with kit.bar():
            with ui.row().classes("items-center gap-1 no-wrap"):
                with kit.button("[NEW ▾]", lambda: None, color="cyan", flat=True, size="xs"):
                    self._new_menu = ui.menu()
                kit.button("[IMPORT]", self._import, color="cyan", flat=True, size="xs")
                kit.button("[SAVE]", self._save, color="green", flat=True, size="xs")
            with ui.row().classes("items-center gap-1 no-wrap"):
                self._name = kit.tag("", "fg", framed=False).classes("font-bold")
                self._dirty = kit.tag("", "amber", framed=False)
                kit.tag(*runbook.repo_status())

        kit.section("Binding")
        self._binding_box = ui.column().classes("w-full gap-1")
        kit.section("$input")
        self._inputs_box = ui.column().classes("w-full gap-1")

        kit.section("Dry Run")
        self._sample_box = ui.row().classes("w-full gap-2 items-center shrink-0")
        self._sources_box = ui.row().classes("w-full items-center gap-1.5 wrap")
        with kit.row(gap="gap-1.5"):
            kit.button("Run Dry", self._dry, color="cyan", flex=True)
            kit.button("Save Fixture", lambda: ui.notify("[MOCK] fixture saved"), color="cyan", outline=True)
            kit.button("Pin", self._open_pin, color="green")
        self._render_controls()

    def _render_controls(self) -> None:
        d = runbook.draft
        self._sync_node_sources()
        self._rebuild_new_menu()

        if self._binding_box is not None:
            self._binding_box.clear()
            with self._binding_box:
                for n in d["nodes"]:
                    with ui.column().classes("tui-row w-full gap-0.5 py-1"):
                        kit.tag(f"{n['no']} {n['name']}", "fg", framed=False).classes("font-bold")
                        for key, src in n["in"].items():
                            opts = {"": "(unbound)", **{k: k for k in runbook.available_keys(n["no"])}}
                            with ui.row().classes("w-full items-center gap-1.5 no-wrap pl-3"):
                                kit.tag(f"{key} <-", "muted", framed=False).classes("w-20")
                                ui.select(
                                    opts,
                                    value=src if src in opts else "",
                                    on_change=lambda e, no=n["no"], k=key: (runbook.set_binding(no, k, e.value), self._changed()),
                                ).props(_SELECT_PROPS).classes("flex-1 text-xs")
                        for cfg_key, cfg_val in n.get("cfg", {}).items():
                            with ui.row().classes("w-full items-center gap-1.5 no-wrap pl-3"):
                                kit.tag(f"{cfg_key} =", "muted", framed=False).classes("w-20")
                                if cfg_key == "model":
                                    ui.select(
                                        ["gemini-3.1-flash", "gemini-3.1-flash-lite", "local/gemma-3"],
                                        value=cfg_val,
                                        on_change=lambda e, no=n["no"], ck=cfg_key: (
                                            runbook.patch(no, cfg={ck: e.value}),
                                            self._changed(),
                                        ),
                                    ).props(_SELECT_PROPS).classes("flex-1 text-xs")
                                else:
                                    ui.input(
                                        value=str(cfg_val),
                                        on_change=lambda e, no=n["no"], ck=cfg_key: (
                                            runbook.patch(no, cfg={ck: e.value}),
                                            self._changed(),
                                        ),
                                    ).props("dark outlined dense square").classes("flex-1 text-xs font-mono")

        if self._sample_box is not None:
            samples = runbook.list_samples(d["flow"])
            if samples and self.sample not in samples:
                self.sample = samples[0]
            self._sample_box.clear()
            if samples:
                with self._sample_box:
                    kit.select(samples, "Sample", self, "sample")

        if self._sources_box is not None:
            self._sources_box.clear()
            with self._sources_box:
                for no, mode in self._sources.items():
                    ui.select(
                        list(_SOURCES),
                        value=mode,
                        label=no,
                        on_change=lambda e, no=no: self._sources.__setitem__(no, e.value),
                    ).props(_SELECT_PROPS).classes("w-28 text-xs")

        if self._inputs_box is not None:
            self._inputs_box.clear()
            with self._inputs_box:
                for key, spec in d["inputs"].items():
                    with ui.row().classes("w-full items-center gap-1.5 no-wrap"):
                        kit.tag(f"{key} : {spec['shape']}", "fg", framed=False).classes("flex-1")
                        ui.select(
                            ["runtime", "fixed"],
                            value=spec["mode"],
                            on_change=lambda e, k=key: (runbook.configure_manifest(k, e.value), self._changed()),
                        ).props(_SELECT_PROPS).classes("w-28 text-xs")
        self._sync_header()

    def compose_right(self) -> None:
        self._viewer = _Viewer()
        self._changed()


class RunbookTriggerTab(StudioTab):
    layer = "runbook"
    title = "Trigger"
    left_header = "Runbooks / Trigger"
    right_header = "Viewer · Run"
    ratio = 42

    def __init__(self) -> None:
        active = runbook.list_active()
        self.flow: str | None = active[0]["flow"] if active else None
        self.run_no: int | None = None
        self._viewer: _Viewer | None = None
        self._list_box: ui.column | None = None
        self._trig_box: ui.column | None = None

    def _select(self, flow: str) -> None:
        self.flow, self.run_no = flow, None
        self._render_left()
        self._show()

    def _show(self) -> None:
        if self._viewer is not None and self.flow is not None:
            self._viewer.show(runbook.run_model(self.flow, run_no=self.run_no))

    def _fire(self) -> None:
        if self.flow is None:
            return
        self.run_no = runbook.fire(self.flow)
        ui.notify(f"[MOCK] run #{self.run_no} started (parked for approval)")
        self._render_left()
        self._show()

    def _approve(self, run_no: int) -> None:
        if self.flow is None:
            return
        runbook.resume(self.flow, run_no)
        ui.notify(f"[MOCK] run #{run_no} approved", type="positive")
        self._render_left()
        self._show()

    def compose_left(self) -> None:
        self._list_box = ui.column().classes("w-full gap-1.5")
        self._trig_box = ui.column().classes("w-full gap-1.5")
        self._render_left()

    def _render_left(self) -> None:
        active = runbook.list_active()
        if self.flow is None and active:
            self.flow = active[0]["flow"]

        if self._list_box is not None:
            self._list_box.clear()
            with self._list_box:
                if not active:
                    kit.tag("(no pinned runbooks)", "muted", framed=False)
                for r in active:
                    text, tone = _STATUS.get(r["status"], _STATUS["none"])
                    with kit.bar() as row:
                        row.classes("cursor-pointer" + (" tui-border-cyan" if r["flow"] == self.flow else ""))
                        row.on("click", lambda _, f=r["flow"]: self._select(f))
                        with ui.row().classes("items-center gap-1.5 no-wrap"):
                            kit.tag(f"{r['flow']} v{r['version']}", "fg", framed=False).classes("font-bold")
                            kit.tag(r["trigger"], "muted", framed=False)
                            if r["stale"]:
                                kit.tag("[RECOMPILE]", "amber")
                        kit.tag(text, tone, framed=False)
                    if r["status"] == "fail" and r["note"]:
                        kit.tag(f"└ {r['note']}", "red", framed=False).classes("pl-3 -mt-1")

        if self._trig_box is None:
            return
        self._trig_box.clear()
        if self.flow is None or self.flow not in runbook.triggers:
            return

        t = runbook.triggers[self.flow]
        runs = runbook.list_runs(self.flow)
        paused = next((r for r in runs if r["status"] == "paused"), None)
        with self._trig_box:
            kit.section("Trigger")
            ui.radio(
                ["manual", "cron", "event"],
                value=t["kind"],
                on_change=lambda e, f=self.flow: (runbook.configure_trigger(f, kind=e.value), self._render_left()),
            ).props("inline dense dark color=info").classes("text-xs")
            if t["kind"] == "cron":
                with ui.row().classes("w-full items-center gap-1.5 no-wrap"):
                    ui.input(
                        "cron",
                        value=t["cron"],
                        on_change=lambda e, f=self.flow: (runbook.configure_trigger(f, cron=e.value), self._render_left()),
                    ).props("dark outlined dense square").classes("flex-1 text-xs font-mono")
                    kit.tag(t["tz"], "muted", framed=False)
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                kit.tag(f"next {runbook.next_run(self.flow)}", "muted", framed=False)
                kit.switch("ON", value=t["on"]).on_value_change(
                    lambda e, f=self.flow: (runbook.configure_trigger(f, on=e.value), self._render_left())
                )

            kit.section("Trigger -> $input")
            for key, src in (t["map"] or {"(none)": "-"}).items():
                with ui.row().classes("w-full items-center gap-1.5 no-wrap"):
                    kit.tag(f"{key} <-", "muted", framed=False).classes("w-24")
                    kit.tag(src, "cyan", framed=False)
            if t["kind"] == "cron":
                kit.tag(
                    "[INFO] cron has no payload: runtime inputs need a source or fixed value",
                    "muted",
                    framed=False,
                ).classes("whitespace-normal")

            with kit.row(gap="gap-1.5"):
                kit.button("[▶ RUN NOW]", self._fire, color="green")
                if paused:
                    kit.button(f"[APPROVE #{paused['no']}]", lambda n=paused["no"]: self._approve(n), color="amber", outline=True)
            if runs:
                selected_run = self.run_no if any(r["no"] == self.run_no for r in runs) else runs[0]["no"]
                ui.select(
                    {r["no"]: f"#{r['no']} {r['status']}" for r in runs},
                    value=selected_run,
                    label="Run",
                    on_change=lambda e: (setattr(self, "run_no", e.value), self._show()),
                ).props(_SELECT_PROPS).classes("w-40 text-xs")

    def compose_right(self) -> None:
        self._viewer = _Viewer()
        self._show()

def build_tabs() -> list[StudioTab]:
    return [RunbookListTab(), RunbookViewTab(), RunbookDraftTab(), RunbookTriggerTab()]