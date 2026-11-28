# fiber.studio.console.runbook.trigger
"""Trigger modal for one flow, opened from Catalog's [TRIGGER →] (former Trigger tab).

Only settings and run live here. The runbook list, run history (Catalog Inspect),
viewer and approval stay in Catalog.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nicegui import ui

from fiber.studio.console.bridge.runbook import runbook
from fiber.studio.console.runbook.catalog import fire, write_steps
import fiber.studio.console.ui.kit as kit

class TriggerDialog:
    """Trigger settings (kind, cron / source, ON) and run-now for the latest pinned version of a flow.

    Changes are applied only on [저장]; the ON switch does not save by itself.
    A flow without a registered trigger opens with manual defaults.
    """

    def __init__(
        self,
        *,
        on_fired: Callable[[str, int], None] | None = None,
        on_saved: Callable[[str], None] | None = None,
    ) -> None:
        self._on_fired = on_fired
        self._on_saved = on_saved
        self.flow: str | None = None
        self.version: int | None = None
        self._form: dict[str, Any] = {}
        self._saved_on = False
        self._dlg = ui.dialog()

    # open / form

    def open_for(self, flow: str) -> None:
        self.flow = flow
        active = runbook.list_active()
        self.version = next((r["version"] for r in active if r["flow"] == flow), None)
        if self.version is None:
            vers = runbook.list_versions(flow)
            self.version = vers[-1] if vers else None
        self._sync_form()
        self._render()
        self._dlg.open()

    def _sync_form(self) -> None:
        t = runbook.triggers.get(self.flow or "", {})
        self._form = {
            "kind": t.get("kind", "manual"),
            "cron": t.get("cron", "0 9 * * 1-5"),
            "source": t.get("source", "webhook"),
            "on": bool(t.get("on", False)),
        }
        self._saved_on = self._form["on"]

    def _set(self, key: str, value: Any, *, rerender: bool = False) -> None:
        self._form[key] = value
        if rerender:
            self._render()

    # actions

    def _save(self) -> None:
        if not self.flow:
            return
        try:
            runbook.configure_trigger(self.flow, **self._form)
        except Exception as exc:
            ui.notify(f"[ERR] trigger not saved: {exc}", type="negative")
            return
        ui.notify(f"[OK] saved {self._form['kind']} trigger for '{self.flow}'", type="positive")
        self._dlg.close()
        if self._on_saved:
            self._on_saved(self.flow)

    def _fire(self) -> None:
        if not self.flow:
            return
        try:
            run_no = fire(self.flow)
        except Exception as exc:
            ui.notify(f"[ERR] run not started: {exc}", type="negative")
            return
        self._dlg.close()
        if self._on_fired:
            self._on_fired(self.flow, run_no)

    # render

    def _render_write_risk(self) -> None:
        """Whether a run writes outside (same text as the former Trigger tab)."""
        if self.flow is None or self.version is None:
            return
        steps = write_steps(self.flow, self.version)
        if steps is None:
            return
        if steps:
            kit.tag(f"쓰기 단계 {len(steps)}개: {', '.join(steps)} · 실행 중 승인을 기다릴 수 있음", "amber", framed=False).classes("text-xs whitespace-normal")
        else:
            kit.tag("쓰기 단계 없음 · 외부에 쓰지 않음", "muted", framed=False).classes("text-xs")

    def _next_run(self) -> str:
        if self.flow in runbook.triggers and self._form["kind"] == "cron":
            return str(runbook.next_run(self.flow))
        return ""

    def _render(self) -> None:
        if not self.flow:
            return
        form = self._form
        kind = form["kind"]
        tz = runbook.triggers.get(self.flow, {}).get("tz", "")
        title = f"Trigger · {self.flow}" + (f" v{self.version}" if self.version is not None else "")

        self._dlg.clear()
        with self._dlg, ui.card().classes("tui-modal-shell w-[460px] p-0 pt-2.5 gap-0"):
            with kit.box(title=title, flex=False, width="w-full gap-2"):
                ui.radio(
                    ["manual", "cron", "event"],
                    value=kind,
                    on_change=lambda e: self._set("kind", e.value, rerender=True),
                ).props("inline dense dark color=info").classes("text-xs")

                if kind == "cron":
                    with ui.row().classes("w-full items-center gap-1.5 no-wrap"):
                        ui.input(
                            "cron schedule",
                            value=form["cron"],
                            on_change=lambda e: self._set("cron", e.value or ""),
                        ).props("dark outlined dense square").classes("flex-1 text-xs font-mono")
                        if tz:
                            kit.tag(tz, "muted", framed=False)
                    nxt = self._next_run()
                    if nxt:
                        kit.tag(f"next {nxt}", "muted", framed=False).classes("text-xs")
                elif kind == "event":
                    ui.input(
                        "event source",
                        value=form["source"],
                        on_change=lambda e: self._set("source", e.value or "webhook"),
                    ).props("dark outlined dense square").classes("w-full text-xs font-mono")

                with ui.row().classes("w-full items-center gap-1.5 no-wrap"):
                    kit.tag("운영", "muted", framed=False)
                    kit.switch("LIVE ON", value=form["on"]).on_value_change(
                        lambda e: self._set("on", e.value, rerender=True)
                    )

                self._render_write_risk()

                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    kit.button("[▶ 지금 실행]", self._fire, color="green", outline=True)
                    ui.space()
                    kit.button("취소", self._dlg.close, color="muted", flat=True)
                    kit.button("저장", self._save, color="cyan")

                if form["on"] and not self._saved_on and kind != "manual":
                    nxt = self._next_run()
                    notice = f"저장하면 {nxt}부터 자동 실행됩니다." if nxt else "저장하면 트리거가 켜집니다."
                    kit.tag(notice, "amber", framed=False).classes("text-xs")