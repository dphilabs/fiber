# fiber.studio.console.ui.component.layout
"""Containers, panes and charts that arrange widgets.

Imported only by `ui.kit` (facade). Tabs use `kit.*`.
Legend boxes: the title sits on the top border, so a box must not clip its own
overflow; scrolling always lives in an inner column with top padding.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from nicegui import ui

from fiber.studio.console.ui.component.widget import q_color, tag
from fiber.studio.console.ui.theme import (
    TUI_AMBER,
    TUI_BLACK,
    TUI_BORDER,
    TUI_BORDER_ACTIVE,
    TUI_CYAN,
    TUI_FG,
    TUI_FONT_STACK,
    TUI_GREEN,
    TUI_MUTED,
    TUI_RED,
    strip_emoji,
)

@contextmanager
def row(*, gap: str = "gap-2", align: str = "items-center") -> Iterator[ui.row]:
    """Full-width horizontal flex row."""
    with ui.row().classes(f"w-full {gap} {align} shrink-0") as r:
        yield r

@contextmanager
def bar() -> Iterator[ui.row]:
    """Compact TUI status bar or list item row."""
    with ui.row().classes(
        "w-full justify-between items-center bg-[#050608] px-3 py-1.5 border border-[#262b3d] shrink-0"
    ) as r:
        yield r

@contextmanager
def box(
    *,
    title: str = "",
    flex: bool = True,
    width: str = "",
    scroll: bool = False,
    height: str = "",
    black: bool = False,
) -> Iterator[ui.element]:
    """WebTUI box with optional legend title.
    - scroll=True yields an inner scroll column (use `height`, e.g. 'h-72').
    - black=True uses the terminal-black surface (lists, logs).
    """
    w_cls = width or ("flex-1" if flex else "")
    if title:
        box_cls = "tui-legend-box" + (" tui-legend-box-black" if black else "")
        pad_cls = "pt-3.5 px-2.5 pb-2.5"
    else:
        box_cls = "bg-[#050608] border border-[#262b3d]"
        pad_cls = "p-2.5"
    outer_h = "" if scroll else height
    with ui.card().classes(f"{w_cls} {box_cls} {pad_cls} {outer_h}".strip()) as c:
        if title:
            ui.label(f" {strip_emoji(title)} ").classes("tui-legend-title")
        if scroll:
            with ui.column().classes(f"w-full {height or 'flex-1'} min-h-0 gap-0 overflow-y-auto") as inner:
                yield inner
        else:
            yield c

# Transitional alias: existing tabs call kit.card(title=..., flex=..., width=...).
card = box

@contextmanager
def footer() -> Iterator[ui.row]:
    """Bottom-anchored action bar (`mt-auto`)."""
    with ui.row().classes("w-full gap-2 mt-auto pt-1.5 border-t border-[#262b3d] shrink-0") as r:
        yield r

def meter(
    label: str,
    target: Any,
    attr: str,
    *,
    text: Callable[[Any], str],
    value: Callable[[Any], float],
    tone: str = "cyan",
    size: str = "6px",
) -> ui.linear_progress:
    """Label + bound value text + progress bar (value() must return 0.0-1.0).

    Wrap in box() for a framed panel; extra rows can follow inside the same box.
    """
    with ui.row().classes("w-full items-center justify-between no-wrap"):
        tag(label, "muted", framed=False)
        tag(bind=(target, attr, text), tone=tone, framed=False).classes("font-extrabold")
    return (
        ui.linear_progress(value=0.0, show_value=False, size=size, color=q_color(tone))
        .bind_value_from(target, attr, value)
        .classes("w-full")
    )

def split_studio(
    left_title: str,
    left_builder: Callable[[], None],
    right_title: str,
    right_builder: Callable[[], None],
    *,
    ratio: int = 42,
) -> ui.splitter:
    """Two-pane split view with legend-bordered panes.

    q-splitter panels are `overflow: auto`, so the legend box keeps a top margin and
    never clips; scrolling lives in an inner column whose top padding leaves room for
    nested `box(title=...)` legends.
    """
    box_cls = "tui-legend-box mt-2 pt-3 px-3 pb-3 gap-0 w-full min-h-0"
    box_style = "height: calc(100% - 0.5rem)"
    with ui.splitter(value=ratio).classes("w-full h-[450px] bg-[#090a0f]") as sp:
        with sp.before:
            with ui.column().classes(box_cls).style(box_style):
                ui.label(f" {strip_emoji(left_title)} ").classes("tui-legend-title")
                with ui.column().classes("w-full flex-1 min-h-0 pt-2 gap-2 overflow-y-auto"):
                    left_builder()
        with sp.after:
            with ui.column().classes(box_cls).style(box_style):
                ui.label(f" {strip_emoji(right_title)} ").classes("tui-legend-title")
                with ui.column().classes("w-full flex-1 min-h-0 pt-2 gap-2 overflow-hidden"):
                    right_builder()
    return sp

@contextmanager
def dual_drawer() -> Iterator[tuple[ui.column, ui.column]]:
    """Bottom dual-terminal drawer; callers add `.tui-legend-title` / `.tui-legend-actions`."""
    with ui.row().classes("w-full px-3 pt-2 pb-3 gap-2.5 no-wrap"):
        left_col = ui.column().classes("w-1/2 tui-legend-box tui-legend-box-black pt-4 px-2.5 pb-2 gap-1")
        right_col = ui.column().classes("w-1/2 tui-legend-box tui-legend-box-black pt-4 px-2.5 pb-2 gap-1")
        yield left_col, right_col

def _axis_label(color: str = TUI_MUTED) -> dict[str, Any]:
    # Fresh dict per chart: option dicts are mutated in place by flush code.
    return {"color": color, "fontSize": 10, "fontFamily": TUI_FONT_STACK}

def _split_line() -> dict[str, Any]:
    return {"lineStyle": {"color": TUI_BORDER, "type": "dashed"}}

def _tooltip(border: str) -> dict[str, Any]:
    return {
        "trigger": "axis",
        "backgroundColor": TUI_BLACK,
        "borderColor": border,
        "borderWidth": 1,
        "textStyle": {"color": TUI_FG, "fontSize": 11, "fontFamily": TUI_FONT_STACK},
    }

def bar_chart(
    categories: list[str],
    data: list[Any],
    *,
    x_max: float | None = None,
    formatter: str = "{c}",
) -> ui.echart:
    """Horizontal bar chart."""
    x_axis: dict[str, Any] = {"type": "value", "splitLine": _split_line(), "axisLabel": _axis_label()}
    if x_max is not None:
        x_axis["max"] = x_max

    return ui.echart({
        "tooltip": _tooltip(TUI_CYAN),
        "grid": {"top": 12, "bottom": 25, "left": 95, "right": 45},
        "xAxis": x_axis,
        "yAxis": {"type": "category", "data": categories, "axisLabel": _axis_label(TUI_FG)},
        "series": [{
            "type": "bar",
            "data": data,
            "itemStyle": {"color": TUI_CYAN},
            "label": {
                "show": True,
                "position": "right",
                "formatter": formatter,
                "color": TUI_FG,
                "fontSize": 10,
                "fontFamily": TUI_FONT_STACK,
            },
        }],
    }).classes("w-full flex-1 min-h-0")

def line_chart(
    x_ticks: list[str],
    y_primary: list[int | float],
    y_secondary: list[int | float],
    *,
    redline: int | float = 0,
    y_max: int = 75000,
) -> ui.echart:
    """Dual-series line chart for FinOps ledger telemetry."""
    return ui.echart({
        "tooltip": _tooltip(TUI_BORDER_ACTIVE),
        "legend": {
            "data": ["Active Quota (FinOps)", "Ledger Balance (PTA)"],
            "textStyle": _axis_label(),
            "top": 0,
        },
        "grid": {"top": 26, "bottom": 20, "left": 55, "right": 20},
        "xAxis": {"type": "category", "data": x_ticks, "axisLabel": _axis_label()},
        "yAxis": {"type": "value", "min": 0, "max": y_max, "splitLine": _split_line(), "axisLabel": _axis_label()},
        "series": [
            {
                "name": "Active Quota (FinOps)",
                "type": "line",
                "data": y_primary,
                "smooth": False,
                "itemStyle": {"color": TUI_GREEN},
                "lineStyle": {"color": TUI_GREEN, "width": 2},
                "areaStyle": {"color": "rgba(80, 250, 123, 0.10)"},
                "markLine": {
                    "silent": True,
                    "lineStyle": {"color": TUI_RED, "width": 1.5, "type": "dashed"},
                    "data": [{"yAxis": redline}],
                },
            },
            {
                "name": "Ledger Balance (PTA)",
                "type": "line",
                "data": y_secondary,
                "step": "end",
                "lineStyle": {"color": TUI_AMBER, "width": 1.5, "type": "dotted"},
                "itemStyle": {"color": TUI_AMBER},
            },
        ],
    }).classes("w-full h-40 shrink-0")