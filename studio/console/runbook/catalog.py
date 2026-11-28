# fiber.studio.console.runbook.catalog
from __future__ import annotations

from collections.abc import Callable
import inspect
import json
from typing import Any

from nicegui import ui

from fiber.studio.console.bridge.runbook import runbook
import fiber.studio.console.ui.kit as kit
from fiber.studio.console.ui.kit import StudioTab
from fiber.node.reflex.runbook.spec import is_write_unit
from fiber.studio.console.runbook.frame.loader import DocViewer

STATUS_BADGE: dict[str, tuple[str, str]] = {
    "ok": ("[OK]", "green"),
    "fail": ("[FAIL]", "red"),
    "paused": ("[PAUSED]", "amber"),
    "dry": ("[DRY]", "amber"),
    "run": ("[~ RUN]", "cyan"),
    "none": ("[ - ]", "muted"),
}
NOTIFY_TYPE = {"ok": "positive", "dry": "warning", "paused": "warning", "fail": "negative"}
SELECT_PROPS = "dark outlined dense square options-dense"
TPL_VIEW = "__tpl__"
RUN_VIEW = "run:"
INSPECT_RECENT_RUNS = 8  # same count as the former Trigger tab's Run History
EXAMPLE_FLOW = "example_refund"

# Catalog list order: what needs the user first. Rows with equal rank keep repository order.
_STATUS_RANK = {"paused": 0, "fail": 1, "run": 2, "ok": 3, "dry": 3}
_RANK_PINNED_NO_RUN = 4
_RANK_TEMPLATE = 5

def current_actor() -> str:
    """The `by` value the bridge records for runs started from this console session."""
    return str(getattr(runbook, "actor", "") or "me")

def by_label(by: str) -> str:
    return "me" if by == current_actor() else by

def fire(flow: str, *, dry: bool = False) -> int:
    """Starts a run. A dry run blocks write steps and is still recorded as a run."""
    if not dry:
        return runbook.fire(flow)
    if "dry" not in inspect.signature(runbook.fire).parameters:
        raise NotImplementedError("runbook.fire() has no dry option")
    return runbook.fire(flow, dry=True)

def notify_run(flow: str, run_no: int) -> None:
    runs = runbook.list_runs(flow)
    cur = next((r for r in runs if r["no"] == run_no), None) or {"status": "ok", "note": ""}
    ui.notify(
        f"[{cur['status'].upper()}] {flow} run #{run_no} · {cur.get('note', '')}",
        type=NOTIFY_TYPE.get(cur["status"], "info"),
    )

def write_steps(flow: str, version: int) -> list[str] | None:
    """Write steps of a pinned version, or None when the spec model carries no node list."""
    nodes = runbook.project(flow, version).get("nodes")
    if not isinstance(nodes, list):
        return None
    return [
        f"{n.get('no', '')} {n.get('name', '')}".strip()
        for n in nodes
        if n.get("kind") != "gate" and is_write_unit(n.get("kind", ""), n.get("name", ""))
    ]

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


def catalog_rows() -> list[dict[str, Any]]:
    """Catalog rows with last run, paused run, `mine` and cron `next`, sorted by what needs the user."""
    me = current_actor()
    rows = merged_catalog_rows()
    for r in rows:
        runs = runbook.list_runs(r["flow"]) if r["pin"] is not None else []
        r["last_run"] = runs[0] if runs else None
        r["paused_run"] = next((x for x in runs if x["status"] == "paused"), None)
        r["mine"] = any(x.get("by") == me for x in runs)
        r["badge"] = "paused" if r["paused_run"] else (runs[0]["status"] if runs else r.get("status", ""))
        r["next"] = (
            runbook.next_run(r["flow"])
            if r["trigger"] == "cron" and r["flow"] in runbook.triggers
            else ""
        )
        if r["pin"] is None:
            r["rank"] = _RANK_TEMPLATE
        elif not runs:
            r["rank"] = _RANK_PINNED_NO_RUN
        else:
            r["rank"] = _STATUS_RANK.get(r["badge"], _RANK_PINNED_NO_RUN)
    return sorted(rows, key=lambda r: r["rank"])

class RunbookCatalogTab(StudioTab):
    """Entry screen: every runbook sorted by what needs the user, with a one-line guide on top."""

    layer = "runbook"
    title = "Catalog"
    left_header = "Runbooks"
    right_header = "Viewer · Spec / Run"
    ratio = 44

    def __init__(
        self,
        *,
        on_edit_draft: Callable[[str, int | None], None] | None = None,
        on_state_mutated: Callable[[], None] | None = None,
    ) -> None:
        self.mode = "ALL"
        self.query = ""
        self.flow: str | None = None
        self.view_target: str = TPL_VIEW
        self.guide_closed = False
        self._on_edit_draft = on_edit_draft
        self._on_state_mutated = on_state_mutated

        self._status: kit.Tag | None = None
        self._guide_box: ui.column | None = None
        self._action_box: ui.row | None = None
        self._list_box: ui.column | None = None
        self._ver_select: ui.select | None = None
        self._showing_tag: kit.Tag | None = None
        self._viewer: DocViewer | None = None
        self._trigger_dlg: Any = None  # TriggerDialog, created in compose_left

    # rows & view targets

    def _rows(self, rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        q = self.query.strip().lower()
        rows = catalog_rows() if rows is None else rows
        if self.mode == "MINE":
            rows = [r for r in rows if r["mine"]]
        return [r for r in rows if not q or q in r["flow"].lower() or q in r["desc"].lower()]

    def _view_options(self) -> dict[str, str]:
        if not self.flow:
            return {}
        opts: dict[str, str] = {}
        vers = runbook.list_versions(self.flow)
        runs = runbook.list_runs(self.flow) if vers else []
        paused = next((r for r in runs if r["status"] == "paused"), None)
        for r in ([paused] if paused else []) + runs[:INSPECT_RECENT_RUNS]:
            opts.setdefault(f"{RUN_VIEW}{r['no']}", f"run #{r['no']} {STATUS_BADGE.get(r['status'], STATUS_BADGE['none'])[0]}")
        opts.update({str(v): f"pinned v{v}" for v in reversed(vers)})
        if runbook.repo.flow(self.flow) is not None or not opts:
            opts[TPL_VIEW] = "template"
        return opts

    def _default_view(self, flow: str) -> str:
        vers = runbook.list_versions(flow)
        runs = runbook.list_runs(flow) if vers else []
        paused = next((r for r in runs if r["status"] == "paused"), None)
        if paused:
            return f"{RUN_VIEW}{paused['no']}"
        if runs and runs[0]["status"] in ("fail", "run"):
            return f"{RUN_VIEW}{runs[0]['no']}"
        return str(vers[-1]) if vers else TPL_VIEW

    def _pinned_target(self) -> int | None:
        """Version used by Draft/Export: the selected pinned version, or the latest one when a run is shown."""
        if self.view_target.isdigit():
            return int(self.view_target)
        if self.view_target.startswith(RUN_VIEW) and self.flow:
            vers = runbook.list_versions(self.flow)
            return vers[-1] if vers else None
        return None

    # state changes

    def refresh_state(self) -> None:
        if self._status is not None:
            self._status.set(*runbook.repo_status())
        rows = self._rows()
        if rows and (self.flow is None or all(r["flow"] != self.flow for r in rows)):
            self._set_flow(rows[0]["flow"])
            return
        self._render_all()

    def _set_flow(self, flow: str, view: str | None = None) -> None:
        self.flow = flow
        self.view_target = view or self._default_view(flow)
        self._render_all()

    def _render_all(self) -> None:
        rows = catalog_rows()
        self._sync_view_options()
        self._render_guide(rows)
        self._render_actions()
        self._render_list(rows)
        self._show()

    def _mutated(self) -> None:
        if self._on_state_mutated:
            self._on_state_mutated()

    def _sync_view_options(self) -> None:
        opts = self._view_options()
        if opts and self.view_target not in opts:
            self.view_target = next(iter(opts))
        if self._ver_select is not None:
            self._ver_select.set_options(opts, value=self.view_target if opts else None)
            self._ver_select.set_enabled(len(opts) > 1)

    # actions

    def _reload(self) -> None:
        runbook.repo.reload()
        self.refresh_state()
        ui.notify("[OK] catalog reloaded", type="positive")

    def _approve(self, flow: str, run_no: int) -> None:
        try:
            runbook.resume(flow, run_no)
        except Exception as exc:
            ui.notify(f"[ERR] {exc}", type="negative")
            return
        ui.notify(f"[OK] {flow} run #{run_no} approved & resumed", type="positive")
        self._set_flow(flow, f"{RUN_VIEW}{run_no}")
        self._mutated()

    def _fire(self, flow: str, *, dry: bool) -> None:
        try:
            run_no = fire(flow, dry=dry)
        except NotImplementedError as exc:
            ui.notify(f"[INFO] {exc}", type="warning")
            return
        notify_run(flow, run_no)
        self._set_flow(flow, f"{RUN_VIEW}{run_no}")
        self._mutated()

    def _close_guide(self) -> None:
        self.guide_closed = True
        if self._guide_box is not None:
            self._guide_box.clear()

    def _jump_draft(self) -> None:
        if not self.flow or self._on_edit_draft is None:
            return
        self._on_edit_draft(self.flow, self._pinned_target())
        if self._viewer is not None:
            switch_console_tab(self._viewer.frame, "Draft")

    def _open_trigger(self) -> None:
        if not self.flow or self._trigger_dlg is None:
            return
        self._trigger_dlg.open_for(self.flow)

    def _trigger_fired(self, flow: str, run_no: int) -> None:
        notify_run(flow, run_no)
        self._set_flow(flow, f"{RUN_VIEW}{run_no}")
        self._mutated()

    def _trigger_saved(self, flow: str) -> None:
        self._set_flow(flow, self.view_target if flow == self.flow else None)
        self._mutated()

    def _export(self) -> None:
        ver = self._pinned_target()
        if not self.flow or ver is None:
            ui.notify("[INFO] Pin된 버전이 있어야 내보낼 수 있습니다", type="warning")
            return
        ui.download(runbook.export_package(self.flow, ver), f"{self.flow}.v{ver}.runbook.json")

    def _show(self) -> None:
        if self._viewer is None or not self.flow:
            return
        t = self.view_target
        if t.startswith(RUN_VIEW):
            run_no = int(t.removeprefix(RUN_VIEW))
            cur = next((r for r in runbook.list_runs(self.flow) if r["no"] == run_no), None)
            badge = STATUS_BADGE.get(cur["status"], STATUS_BADGE["none"])[0] if cur else ""
            if self._showing_tag is not None:
                self._showing_tag.set(f"showing {self.flow} · run #{run_no} {badge}", "cyan")
            self._viewer.show(runbook.run_model(self.flow, run_no=run_no))
        elif t.isdigit():
            if self._showing_tag is not None:
                self._showing_tag.set(f"showing {self.flow} · pinned v{t}", "cyan")
            self._viewer.show(runbook.project(self.flow, int(t)))
        else:
            if self._showing_tag is not None:
                self._showing_tag.set(f"showing {self.flow} · template", "muted")
            self._viewer.show(runbook.template_model(self.flow))

    # guide line

    def _guide(self, rows: list[dict[str, Any]]) -> tuple[str, str | None, Callable[[], None] | None] | None:
        """(text, button label, handler) for the next step, or None when the user no longer needs it."""
        if self.guide_closed or any(r["mine"] and r["flow"] != EXAMPLE_FLOW for r in rows):
            return None
        if not runbook.list_versions(EXAMPLE_FLOW):
            if not any(r["pin"] is not None for r in rows):
                return ("Pin된 런북이 없습니다. tpl 행을 고르고 [OPEN IN DRAFT →]에서 Pin하세요.", None, None)
            if not any(r["mine"] for r in rows):
                return ("아직 내 run이 없습니다. 런북을 고르고 [TRIGGER →]에서 실행해 보세요.", None, None)
            return None
        me = current_actor()
        mine = [r for r in runbook.list_runs(EXAMPLE_FLOW) if r.get("by") == me]
        status = mine[0]["status"] if mine else ""
        ex = EXAMPLE_FLOW
        if not mine:
            return ("아직 내 run이 없습니다. 예제를 Dry로 실행해 보세요. 외부에 쓰지 않습니다.",
                    "[▶ RUN DRY]", lambda: self._fire(ex, dry=True))
        if status == "paused":
            n = mine[0]["no"]
            return ("무엇을 쓰는지 오른쪽에서 확인하고 승인하세요.",
                    f"[✓ APPROVE #{n}]", lambda: self._approve(ex, n))
        if status == "run":
            return ("예제가 실행 중입니다. 오른쪽에서 진행 상황을 볼 수 있습니다.", None, None)
        if status == "ok":
            return ("예제를 끝까지 실행했습니다. 직접 만들려면 tpl 행을 고르고 [OPEN IN DRAFT →]를 누르세요.", None, None)
        return ("이번엔 실제로 실행해 보세요. 쓰기 단계 앞에서 멈추고 승인을 기다립니다.",
                "[▶ RUN]", lambda: self._fire(ex, dry=False))

    def _render_guide(self, rows: list[dict[str, Any]]) -> None:
        if self._guide_box is None:
            return
        self._guide_box.clear()
        step = self._guide(rows)
        if step is None:
            return
        text, label, handler = step
        with self._guide_box, kit.bar() as box:
            box.classes("tui-border-cyan")
            with ui.column().classes("gap-0.5 min-w-0 flex-1"):
                kit.tag(text, "cyan", framed=False).classes("whitespace-normal text-xs")
                if label and runbook.list_versions(EXAMPLE_FLOW):
                    kit.tag(f"{EXAMPLE_FLOW} v{runbook.list_versions(EXAMPLE_FLOW)[-1]}", "fg", framed=False).classes("font-bold")
            with ui.row().classes("items-center gap-1.5 no-wrap"):
                if label and handler:
                    kit.button(label, handler, color="cyan")
                kit.button("[✕]", self._close_guide, color="muted", flat=True, size="xs")

    def _render_actions(self) -> None:
        if self._action_box is None:
            return
        self._action_box.clear()
        if not self.flow:
            return
        ver = self._pinned_target()
        has_pin = bool(runbook.list_versions(self.flow))
        with self._action_box:
            kit.tag(self.flow, "fg", framed=False).classes("font-bold truncate mr-auto")
            kit.button(f"[EDIT v{ver} IN DRAFT →]" if ver else "[OPEN IN DRAFT →]", self._jump_draft, color="cyan", outline=True)
            if has_pin:
                kit.button("[TRIGGER →]", self._open_trigger, color="green", outline=True)

    @staticmethod
    def _row_detail(r: dict[str, Any]) -> str:
        parts: list[str] = []
        if r["last_run"]:
            last = r["paused_run"] or r["last_run"]
            parts.append(f"run #{last['no']} · by {by_label(last.get('by', ''))}")
        elif r["pin"] is not None:
            parts.append("(no run)")
        if r["next"]:
            parts.append(f"next {r['next']}")
        return " · ".join(parts)

    def _render_list(self, rows: list[dict[str, Any]] | None = None) -> None:
        if self._list_box is None:
            return
        self._list_box.clear()
        rows = self._rows(rows)
        with self._list_box:
            if not rows:
                hint = "(내 run이 있는 런북이 없습니다)" if self.mode == "MINE" else "(no matching runbooks)"
                kit.tag(hint, "muted", framed=False).classes("px-2")
            for r in rows:
                text, tone = STATUS_BADGE.get(r["badge"], STATUS_BADGE["none"]) if r["badge"] else ("-", "muted")
                with kit.bar() as row:
                    row.classes("cursor-pointer" + (" tui-border-cyan" if r["flow"] == self.flow else ""))
                    row.on("click", lambda _, f=r["flow"]: self._set_flow(f))
                    if r["desc"]:
                        row.tooltip(r["desc"])
                    with ui.row().classes("w-full items-center justify-between gap-2 no-wrap"):
                        with ui.column().classes("gap-0 min-w-0 flex-1"):
                            with ui.row().classes("items-center gap-1.5 no-wrap"):
                                kit.tag(r["flow"], "fg", framed=False).classes("font-bold truncate")
                                kit.tag(f"v{r['pin']}" if r["pin"] else "tpl", "cyan" if r["pin"] else "muted")
                                if r["trigger"]:
                                    kit.tag(r["trigger"], "muted", framed=False)
                            detail = self._row_detail(r)
                            if detail:
                                kit.tag(detail, "muted", framed=False).classes("truncate text-xs")
                        kit.tag(text, tone, framed=False)
                if r["paused_run"]:
                    n = r["paused_run"]["no"]
                    with ui.row().classes("w-full justify-end"):
                        kit.button(f"[✓ APPROVE #{n}]", lambda f=r["flow"], n=n: self._approve(f, n), color="amber")

    # compose

    def compose_left(self) -> None:
        from fiber.studio.console.runbook.trigger import TriggerDialog

        self._trigger_dlg = TriggerDialog(on_fired=self._trigger_fired, on_saved=self._trigger_saved)
        with kit.bar():
            ui.toggle(
                ["ALL", "MINE"],
                value=self.mode,
                on_change=lambda e: (setattr(self, "mode", e.value), self.refresh_state()),
            ).props("dense flat square no-caps toggle-color=info text-color=grey-6").classes("text-xs")
            with ui.row().classes("items-center gap-1.5 no-wrap"):
                self._status = kit.tag(*runbook.repo_status())
                kit.button("[RELOAD]", self._reload, color="cyan", outline=True)
        ui.input(
            placeholder="Filter runbook or description...",
            on_change=lambda e: (setattr(self, "query", e.value or ""), self._render_list()),
        ).props("dark outlined dense square clearable").classes("w-full text-xs")
        self._guide_box = ui.column().classes("w-full gap-1")
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
        self._viewer = DocViewer(min_height="0px")
        self._sync_view_options()
        self._show()

RunbookListTab = RunbookCatalogTab