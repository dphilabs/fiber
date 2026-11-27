# fiber.studio.console.tab.catalog
from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
from typing import Any

from nicegui import ui

from fiber.studio.console.runbook.bridge import runbook
import fiber.studio.console.ui.kit as kit
from fiber.studio.console.ui.kit import StudioTab

_VIEWER_PATH = Path(__file__).resolve().parents[1] / "ui" / "component" / "viewer.html"
_VIEWER_HTML = _VIEWER_PATH.read_text(encoding="utf-8") if _VIEWER_PATH.is_file() else ""

STATUS_BADGE: dict[str, tuple[str, str]] = {
    "ok": ("[OK]", "green"),
    "fail": ("[FAIL]", "red"),
    "paused": ("[PAUSED]", "amber"),
    "dry": ("[DRY]", "amber"),
    "run": ("[~ RUN]", "cyan"),
    "none": ("[ - ]", "muted"),
}
SELECT_PROPS = "dark outlined dense square options-dense"
TPL_VIEW = "__tpl__"


def _normalize_viewer_model(model: dict[str, Any]) -> dict[str, Any]:
    if model.get("stage") == "pin":
        return {**model, "stage": "pinned"}
    return model


def switch_console_tab(tab_frame: ui.element | None, target_title: str) -> None:
    """Switches sibling NiceGUI/Quasar tab by visible label without coupling to shell internals."""
    client = getattr(tab_frame, "client", None) if tab_frame is not None else None
    if client is None:
        return
    title_js = json.dumps(target_title.lower())
    client.run_javascript(
        f"for (const el of document.querySelectorAll('.q-tab')) {{"
        f"  if ((el.innerText || '').trim().toLowerCase() === {title_js}) {{ el.click(); break; }}"
        f"}}"
    )


def merged_catalog_rows() -> list[dict[str, Any]]:
    rows = list(runbook.list_catalog())
    seen = {r["flow"] for r in rows}
    for t in runbook.list_templates():
        fname = t["name"]
        if fname not in seen:
            vers = runbook.list_versions(fname)
            rows.append({
                "flow": fname,
                "desc": t.get("desc", ""),
                "src": True,
                "seeds": len(runbook.repo.samples(fname)),
                "fixture": bool(runbook.repo.mock_outputs(fname)),
                "pin": vers[-1] if vers else None,
                "trigger": runbook.triggers.get(fname, {}).get("kind", "") if vers else "",
                "status": "",
                "note": "",
            })
    return rows


class RunbookViewer:
    """Iframe-isolated Runbook viewer using `frame.client.run_javascript` (safe across parent `.clear()`)."""

    def __init__(self, *, min_height: str = "420px") -> None:
        self.theme = "tui"
        self.model: dict[str, Any] | None = None
        self._mounted_doc = False
        self._seq = 0
        self.frame = (
            ui.element("iframe")
            .classes("w-full flex-1 min-h-0 border-0")
            .style(f"min-height: {min_height}")
        )

    def _run_js(self, code: str) -> None:
        client = getattr(self.frame, "client", None)
        if client is not None:
            client.run_javascript(code)

    def _build_srcdoc(self, model: dict[str, Any] | None) -> str:
        norm = _normalize_viewer_model(model) if model is not None else None
        data = json.dumps(norm, ensure_ascii=False).replace("</", "<\\/")
        return (
            _VIEWER_HTML.replace('data-theme="doc"', f'data-theme="{self.theme}"', 1)
            .replace("/*__MODEL__*/null", data, 1)
        )

    def show(self, model: dict[str, Any]) -> None:
        norm = _normalize_viewer_model(model)
        self.model = norm
        self._seq += 1
        self.frame._props["srcdoc"] = self._build_srcdoc(norm)
        if not self._mounted_doc:
            self.frame.update()
            self._mounted_doc = True
            return
        msg = json.dumps({"type": "model", "seq": self._seq, "payload": norm}, ensure_ascii=False)
        self._run_js(
            f"const el = getHtmlElement({self.frame.id});"
            f"if (el && el.contentWindow) el.contentWindow.postMessage({msg}, '*');"
        )

    def toggle_theme(self) -> None:
        self.theme = "doc" if self.theme == "tui" else "tui"
        self._seq += 1
        if self.model is not None:
            self.frame._props["srcdoc"] = self._build_srcdoc(self.model)
        msg = json.dumps({"type": "theme", "seq": self._seq, "payload": self.theme})
        self._run_js(
            f"const el = getHtmlElement({self.frame.id});"
            f"if (el && el.contentWindow) el.contentWindow.postMessage({msg}, '*');"
        )

    def print(self) -> None:
        self._run_js(
            f"const el = getHtmlElement({self.frame.id});"
            f"if (el && el.contentWindow) el.contentWindow.print();"
        )


class RunbookCatalogTab(StudioTab):
    """Unified Catalog & Inspector tab combining flow list, version inspection, export/PDF, and Draft jump."""

    layer = "runbook"
    title = "Catalog"
    left_header = "Flows & Releases"
    right_header = "Inspector · Spec / Release"
    ratio = 44

    def __init__(
        self,
        *,
        on_edit_draft: Callable[[str, int | None], None] | None = None,
        on_open_trigger: Callable[[str], None] | None = None,
    ) -> None:
        self.mode = "ALL"
        self.query = ""
        self.flow: str | None = None
        self.view_target: str = TPL_VIEW
        self._on_edit_draft = on_edit_draft
        self._on_open_trigger = on_open_trigger

        self._status: kit.Tag | None = None
        self._action_box: ui.row | None = None
        self._list_box: ui.column | None = None
        self._ver_select: ui.select | None = None
        self._showing_tag: kit.Tag | None = None
        self._viewer: RunbookViewer | None = None

    def _rows(self) -> list[dict[str, Any]]:
        q = self.query.strip().lower()
        rows = merged_catalog_rows()
        if self.mode == "PINNED":
            rows = [r for r in rows if r["pin"] is not None]
        elif self.mode == "TEMPLATE":
            rows = [r for r in rows if r["src"] and r["pin"] is None]
        return [r for r in rows if not q or q in r["flow"].lower() or q in r["desc"].lower()]

    def _view_options(self) -> dict[str, str]:
        if not self.flow:
            return {}
        opts = {str(v): f"pinned v{v}" for v in reversed(runbook.list_versions(self.flow))}
        if runbook.repo.flow(self.flow) is not None or not opts:
            opts[TPL_VIEW] = "template"
        return opts

    def refresh_state(self) -> None:
        if self._status is not None:
            self._status.set(*runbook.repo_status())
        rows = self._rows()
        if rows and (self.flow is None or all(r["flow"] != self.flow for r in rows)):
            self._set_flow(rows[0]["flow"])
            return
        self._sync_view_options()
        self._render_actions()
        self._render_list()
        self._show()

    def _set_flow(self, flow: str) -> None:
        self.flow = flow
        vers = runbook.list_versions(flow)
        self.view_target = str(vers[-1]) if (vers and self.mode != "TEMPLATE") else TPL_VIEW
        self._sync_view_options()
        self._render_actions()
        self._render_list()
        self._show()

    def _sync_view_options(self) -> None:
        opts = self._view_options()
        if opts and self.view_target not in opts:
            self.view_target = next(iter(opts))
        if self._ver_select is not None:
            self._ver_select.set_options(opts, value=self.view_target if opts else None)
            self._ver_select.set_enabled(len(opts) > 1)

    def _reload(self) -> None:
        runbook.repo.reload()
        self.refresh_state()
        ui.notify("[OK] catalog reloaded", type="positive")

    def _jump_draft(self) -> None:
        if not self.flow or self._on_edit_draft is None:
            return
        ver = int(self.view_target) if self.view_target.isdigit() else None
        self._on_edit_draft(self.flow, ver)
        if self._viewer is not None:
            switch_console_tab(self._viewer.frame, "Draft")

    def _jump_trigger(self) -> None:
        if not self.flow or self._on_open_trigger is None:
            return
        self._on_open_trigger(self.flow)
        if self._viewer is not None:
            switch_console_tab(self._viewer.frame, "Trigger")

    def _export(self) -> None:
        if not self.flow or not self.view_target.isdigit():
            ui.notify("[INFO] select a pinned version (vN) to export package", type="warning")
            return
        ver = int(self.view_target)
        ui.download(runbook.export_package(self.flow, ver), f"{self.flow}.v{ver}.runbook.json")

    def _show(self) -> None:
        if self._viewer is None or not self.flow:
            return
        if self.view_target.isdigit():
            ver = int(self.view_target)
            if self._showing_tag is not None:
                self._showing_tag.set(f"showing {self.flow} · pinned v{ver}", "cyan")
            self._viewer.show(runbook.project(self.flow, ver))
        else:
            if self._showing_tag is not None:
                self._showing_tag.set(f"showing {self.flow} · template", "muted")
            self._viewer.show(runbook.template_model(self.flow))

    def _render_actions(self) -> None:
        if self._action_box is None:
            return
        self._action_box.clear()
        if not self.flow:
            return
        is_ver = self.view_target.isdigit()
        has_pin = bool(runbook.list_versions(self.flow))
        with self._action_box:
            kit.tag(self.flow, "fg", framed=False).classes("font-bold truncate mr-auto")
            btn_label = f"[EDIT v{self.view_target} IN DRAFT →]" if is_ver else "[OPEN IN DRAFT →]"
            kit.button(btn_label, self._jump_draft, color="cyan", outline=True)
            if has_pin:
                kit.button("[TRIGGER →]", self._jump_trigger, color="green", outline=True)

    def _render_list(self) -> None:
        if self._list_box is None:
            return
        self._list_box.clear()
        rows = self._rows()
        with self._list_box:
            if not rows:
                kit.tag("(no matching flows)", "muted", framed=False).classes("px-2")
            for r in rows:
                text, tone = STATUS_BADGE.get(r["status"], STATUS_BADGE["none"]) if r["status"] else ("-", "muted")
                with kit.bar() as row:
                    row.classes("cursor-pointer" + (" tui-border-cyan" if r["flow"] == self.flow else ""))
                    row.on("click", lambda _, f=r["flow"]: self._set_flow(f))
                    with ui.row().classes("w-full items-center justify-between gap-2 no-wrap"):
                        with ui.column().classes("gap-0 min-w-0 flex-1"):
                            with ui.row().classes("items-center gap-1.5 no-wrap"):
                                kit.tag(r["flow"], "fg", framed=False).classes("font-bold truncate")
                                kit.tag(f"v{r['pin']}" if r["pin"] else "tpl", "cyan" if r["pin"] else "muted")
                                if r["trigger"]:
                                    kit.tag(r["trigger"], "muted", framed=False)
                            if r["desc"]:
                                kit.tag(r["desc"], "muted", framed=False).classes("truncate text-xs")
                        kit.tag(text, tone, framed=False)

    def compose_left(self) -> None:
        with kit.bar():
            ui.toggle(
                ["ALL", "PINNED", "TEMPLATE"],
                value=self.mode,
                on_change=lambda e: (setattr(self, "mode", e.value), self.refresh_state()),
            ).props("dense flat square no-caps toggle-color=info text-color=grey-6").classes("text-xs")
            with ui.row().classes("items-center gap-1.5 no-wrap"):
                self._status = kit.tag(*runbook.repo_status())
                kit.button("[RELOAD]", self._reload, color="cyan", outline=True)
        ui.input(
            placeholder="Filter flow or description...",
            on_change=lambda e: (setattr(self, "query", e.value or ""), self._render_list()),
        ).props("dark outlined dense square clearable").classes("w-full text-xs")
        self._action_box = ui.row().classes("w-full items-center gap-1.5 py-1 no-wrap")
        self._list_box = ui.column().classes("w-full gap-1")
        self.refresh_state()

    def compose_right(self) -> None:
        opts = self._view_options()
        with kit.bar():
            with ui.row().classes("items-center gap-2 no-wrap"):
                self._ver_select = (
                    ui.select(
                        opts,
                        value=self.view_target if opts else None,
                        label="Inspect",
                        on_change=lambda e: (setattr(self, "view_target", e.value or TPL_VIEW), self._render_actions(), self._show()),
                    )
                    .props(SELECT_PROPS)
                    .classes("w-40 text-xs")
                )
                self._showing_tag = kit.tag("", "muted", framed=False)
            with ui.row().classes("items-center gap-1.5 no-wrap"):
                kit.button("[tui|doc]", lambda: self._viewer and self._viewer.toggle_theme(), color="muted", outline=True)
                kit.button("[EXPORT]", self._export, color="cyan", outline=True)
                kit.button("[PDF]", lambda: self._viewer and self._viewer.print(), color="cyan", outline=True)
        self._viewer = RunbookViewer(min_height="460px")
        self._sync_view_options()
        self._show()


RunbookListTab = RunbookCatalogTab