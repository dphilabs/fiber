# fiber.studio.console.ui.component.widget
"""Single UI elements (inputs, buttons, tags, logs, file links)."""
from __future__ import annotations

from collections.abc import Callable, Sequence
import inspect
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from nicegui import Client, ui

import fiber.studio.console.ui.diag as diag
from fiber.studio.console.ui.theme import strip_emoji, tui_button_label, tui_text

CLS_MONO_LABEL = (
    "text-[11px] font-mono text-[#9aa5ce] bg-[#050608] px-2.5 py-1.5 "
    "border border-[#262b3d] w-full truncate"
)
CLS_SECTION_HEADER = "text-xs font-bold tracking-wider uppercase text-[#7dcfff] shrink-0"

TONE_TEXT: dict[str, str] = {
    "green": "tui-text-green",
    "cyan": "tui-text-cyan",
    "amber": "tui-text-amber",
    "red": "tui-text-red",
    "muted": "tui-text-muted",
    "fg": "tui-text-fg",
}

# Tone and legacy color names -> Quasar brand colors
_COLOR_ALIAS: dict[str, str] = {
    "green": "positive",
    "emerald": "positive",
    "cyan": "info",
    "red": "negative",
    "amber": "warning",
    "muted": "grey-6",
    "fg": "grey-4",
    "slate-4": "grey-6",
    "slate-8": "grey-9",
}

def q_color(name: str) -> str:
    """Tone or legacy color name -> Quasar color prop value."""
    return _COLOR_ALIAS.get(name, name)

def tone_class(tone: str) -> str:
    return TONE_TEXT.get(tone, TONE_TEXT["fg"])

_orig_notify = ui.notify

def _context_safe_notify(message: Any, **kwargs: Any) -> None:
    if isinstance(message, str):
        message = tui_text(message)  # strip emoji globally: '💾 Saved' -> '[SAVE] Saved'
    try:
        _orig_notify(message, **kwargs)
    except RuntimeError:
        for client in list(Client.instances.values()):
            if getattr(client, "has_socket_connection", False):
                with client:
                    _orig_notify(message, **kwargs)
                return

ui.notify = _context_safe_notify

def wrap_async(handler: Callable[[], Any]) -> Callable[[], Any]:
    """Run sync/async handler; log exceptions to diag instead of breaking the UI."""
    async def _runner() -> None:
        try:
            res = handler()
            if inspect.iscoroutine(res):
                await res
        except Exception as exc:
            diag.exception(
                "UI button handler failed",
                exc=exc,
                handler=getattr(handler, "__name__", "lambda"),
            )

    return _runner

def open_local_file(
    path: str | Path,
    *,
    default_content: str = "{}\n",
    create_if_missing: bool = False,
) -> None:
    target = Path(path).expanduser().resolve()
    try:
        if not target.exists():
            if not create_if_missing:
                ui.notify(
                    f"[WARN] File does not exist yet: {target.name} (Use '+ Init' to create it)",
                    type="warning",
                )
                return
            if target.suffix:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(default_content, encoding="utf-8")
        elif target.is_file() and target.stat().st_size == 0 and default_content:
            target.write_text(default_content, encoding="utf-8")

        editor_cmd = os.environ.get("FIBER_EDITOR") or os.environ.get("EDITOR")
        if editor_cmd:
            subprocess.Popen([editor_cmd, str(target)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-t", str(target)] if target.is_file() else ["open", str(target)])
        elif sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", str(target)])
        else:
            os.startfile(str(target))  # type: ignore[attr-defined]

        ui.notify(f"[OPEN] External editor: {target.name}", type="info")
    except Exception as exc:
        diag.exception("Failed to open local file in external tool", exc=exc, path=str(target))
        ui.notify(f"[ERR] Cannot open {target.name}: {exc}", type="warning")

def file_link(
    path: str | Path | Callable[[], str | Path],
    *,
    label: str = "",
    tooltip: str = "",
    default_content: str = "{}\n",
    create_if_missing: bool = False,
) -> ui.row:
    """Inline file label + [EDIT] / [+ INIT] key that opens an external editor."""
    def _resolve() -> Path:
        raw = path() if callable(path) else path
        return Path(raw).expanduser()

    initial_path = _resolve()
    disp_text = label or initial_path.name
    tip_text = tooltip or f"Open externally: {initial_path}"
    btn_text = "[+ INIT]" if create_if_missing and not initial_path.exists() else "[EDIT]"

    with ui.row().classes("items-center gap-1 no-wrap shrink-0") as container:
        if label != "":
            ui.label(disp_text).classes("text-[11px] font-mono text-[#9aa5ce] underline decoration-[#262b3d]")
        ui.button(
            btn_text,
            on_click=lambda: open_local_file(
                _resolve(),
                default_content=default_content,
                create_if_missing=create_if_missing,
            ),
        ).props("flat dense no-caps size=sm color=info").classes("text-[11px] px-1").tooltip(tip_text)
    return container

def text(
    label: str, target: Any, attr: str, *, flex: bool = True, mono: bool = True, width: str = ""
) -> ui.input:
    w_cls = width or ("flex-1" if flex else "")
    m_cls = "font-mono" if mono else ""
    return (
        ui.input(label)
        .bind_value(target, attr)
        .props("dark outlined dense square")
        .classes(f"{w_cls} {m_cls} text-xs".strip())
    )

def number(
    label: str,
    target: Any,
    attr: str,
    *,
    min_val: float = 0,
    max_val: float = 100,
    step: float = 1,
    width: str = "w-24",
    as_int: bool = False,
) -> ui.number:
    forward_fn = int if as_int else lambda x: x
    return (
        ui.number(label, min=min_val, max=max_val, step=step)
        .bind_value(target, attr, forward=forward_fn)
        .props("dark outlined dense square")
        .classes(f"{width} font-mono text-xs")
    )

def select(
    options: Sequence[str], label: str, target: Any, attr: str, *, width: str = "flex-1"
) -> ui.select:
    return (
        ui.select(list(options), label=label)
        .bind_value(target, attr)
        .props("dark outlined dense square")
        .classes(f"{width} text-xs")
    )

def switch(
    label: str, target: Any | None = None, attr: str = "", *, color: str = "green", value: bool = False
) -> ui.switch:
    sw = ui.switch(label, value=value).props(f"color={q_color(color)} dense").classes("text-xs")
    if target is not None and attr:
        sw.bind_value(target, attr)
    return sw

def button(
    label: str,
    action: Callable[[], Any],
    *,
    color: str = "green",
    outline: bool = False,
    flat: bool = False,
    flex: bool = False,
    text_color: str = "",
    bracket: bool = True,
    size: str = "sm",
) -> ui.button:
    """TUI keycap button '[ LABEL ]' (emoji stripped).
    - color: tone or legacy name. Filled buttons use inverted (dark) text.
    - size: 'sm' (default) | 'xs' (inline key, e.g. '[↻]' next to a label).
    """
    props = f"color={q_color(color)} dense no-caps unelevated"
    if flat:
        props += " flat"
    elif outline:
        props += " outline"
    elif not text_color:
        text_color = "dark"
    if text_color:
        props += f" text-color={q_color(text_color)}"
    if size == "xs":
        props += " size=xs"
        cls = "px-1 text-[10px]"
    else:
        cls = f"{'flex-1 font-bold' if flex else 'px-2.5'} text-xs tracking-wide"
    label_text = tui_button_label(label) if bracket else label
    return ui.button(label_text, on_click=wrap_async(action)).props(props).classes(cls)

def editor(target: Any, attr: str, *, lang: str = "json") -> ui.codemirror:
    return (
        ui.codemirror(language=lang, theme="oneDark")
        .bind_value(target, attr)
        .classes("w-full flex-1 min-h-0 text-xs border border-[#262b3d]")
    )

class Tag(ui.label):
    """Status tag / badge / plain status text with an in-place tone switch."""

    def __init__(self, content: str = "", *, tone: str = "fg", framed: bool = True) -> None:
        super().__init__(strip_emoji(content))
        self.classes("tui-tag" if framed else "tui-plain")
        self._tone = ""
        self.set_tone(tone)

    def set_tone(self, tone: str) -> Tag:
        if self._tone:
            self.classes(remove=tone_class(self._tone))
        self._tone = tone if tone in TONE_TEXT else "fg"
        self.classes(add=tone_class(self._tone))
        return self

    def set(self, content: str | None = None, tone: str | None = None) -> Tag:
        """Update text and/or tone, e.g. status.set('Saved', 'green')."""
        if content is not None:
            self.set_text(strip_emoji(content))
        if tone is not None:
            self.set_tone(tone)
        return self

def tag(
    content: str = "",
    tone: str = "fg",
    *,
    bind: tuple[Any, str] | tuple[Any, str, Callable[[Any], Any]] | None = None,
    framed: bool = True,
    on_click: Callable[[], Any] | None = None,
    tooltip: str = "",
) -> Tag:
    """Replaces ui.badge / chip labels / status labels. Emoji are always stripped.
    - bind=(target, attr[, fmt]) binds text from state.
    - framed=False renders plain colored text (status line) instead of a boxed tag.
    """
    t = Tag(content, tone=tone, framed=framed)
    if bind is not None:
        target, attr, *fmt = bind
        fmt_fn = fmt[0] if fmt else str
        t.bind_text_from(target, attr, backward=lambda v: strip_emoji(fmt_fn(v)))
    if on_click is not None:
        t.classes("cursor-pointer").on("click", wrap_async(on_click))
    if tooltip:
        t.tooltip(tooltip)
    return t

def section(title: str, tone: str = "cyan") -> ui.label:
    """Section heading inside a pane."""
    return ui.label(strip_emoji(title)).classes(
        f"text-xs font-bold tracking-wider uppercase shrink-0 {tone_class(tone)}"
    )

def log(
    max_lines: int = 100,
    *,
    color: str = "tui-text-fg",
    height: str = "flex-1 min-h-0",
    bordered: bool = True,
) -> ui.log:
    """Terminal log pane. Use bordered=False inside a legend box to avoid a double frame."""
    border_cls = "border border-[#262b3d] p-2" if bordered else "border-0 px-0.5 py-1"
    return ui.log(max_lines=max_lines).classes(
        f"w-full {height} bg-[#050608] {border_cls} font-mono text-xs {color}"
    )