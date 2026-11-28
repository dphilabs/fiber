# fiber.studio.console.ui.kit
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence

from nicegui import ui

from fiber.studio.console.ui.component.layout import (
    bar as bar,
    bar_chart as bar_chart,
    box as box,
    card as card,  # transitional alias of box
    dual_drawer as dual_drawer,
    footer as footer,
    line_chart as line_chart,
    meter as meter,
    row as row,
    split_studio as split_studio,
)
from fiber.studio.console.ui.component.widget import (
    CLS_MONO_LABEL as CLS_MONO_LABEL,
    CLS_SECTION_HEADER as CLS_SECTION_HEADER,
    Tag as Tag,
    button as button,
    editor as editor,
    file_link as file_link,
    log as log,
    number as number,
    open_local_file as open_local_file,
    section as section,
    select as select,
    switch as switch,
    tag as tag,
    text as text,
)
from fiber.studio.console.ui.theme import apply_webtui_theme, strip_emoji


def trigger_viewport_resize() -> None:
    ui.run_javascript("setTimeout(() => window.dispatchEvent(new Event('resize')), 30)")


class StudioTab(ABC):
    """Base template for domain studio tabs (MCP, LLM Live, FinOps, VCR)."""
    title: str = "Tab"
    left_header: str = "Configuration"
    right_header: str = "Telemetry & Proof"
    ratio: int = 42
    layer: str = "gateway"  # top-level group; a second layer (e.g. "runbook") adds a layer tab bar

    @abstractmethod
    def compose_left(self) -> None:
        """Build left configuration pane."""

    @abstractmethod
    def compose_right(self) -> None:
        """Build right telemetry/chart pane."""

    def flush(self) -> None:
        """100ms ringbuffer sync hook (override if needed)."""

    def render(self) -> ui.splitter:
        """Render left and right panes inside `split_studio`."""
        return split_studio(
            self.left_header,
            self.compose_left,
            self.right_header,
            self.compose_right,
            ratio=self.ratio,
        )


class ModalTemplate(ABC):
    """Base template for modal dialogs (HITL 202 gate, TOTP QR)."""
    width_cls: str = "w-[480px]"
    border_cls: str = "border border-[#262b3d]"
    persistent: bool = False
    title: str = ""  # optional legend title on the top border

    @abstractmethod
    def compose(self, dialog: ui.dialog) -> None:
        """Build modal card content."""

    def mount(self) -> ui.dialog:
        props = "persistent" if self.persistent else ""
        # Transparent shell with top padding keeps the legend inside q-dialog's scroll box.
        with ui.dialog().props(props) as dialog, ui.card().classes(
            f"tui-modal-shell {self.width_cls} p-0 pt-2.5 gap-0"
        ), ui.column().classes(f"tui-legend-box {self.border_cls} w-full p-5 gap-4"):
            if self.title:
                ui.label(f" {strip_emoji(self.title)} ").classes("tui-legend-title")
            self.compose(dialog)
        return dialog


class ConsoleTemplate:
    def __init__(
        self,
        tabs: Sequence[StudioTab],
        modals: Sequence[ModalTemplate] = (),
        *,
        header_builder: Callable[[], None] | None = None,
        drawer_builder: Callable[[], None] | None = None,
        on_tick: Callable[[Sequence[StudioTab]], None] | None = None,
        tick_interval: float = 0.1,
    ) -> None:
        self.tabs = list(tabs)
        self.modals = list(modals)
        self.header_builder = header_builder
        self.drawer_builder = drawer_builder
        self.on_tick = on_tick
        self.tick_interval = tick_interval

    def build_view(self) -> None:
        apply_webtui_theme()
        ui.query("body").classes("bg-[#090a0f] text-[#d8dee9] select-none")
        # Viewport-filling shell: tab panels take the height left by header, tab bars and drawer (theme.py).
        ui.query(".q-page-container").classes("tui-shell-container")
        ui.query(".q-page").classes("tui-shell")
        ui.query(".nicegui-content").classes("p-0 gap-0")

        for modal in self.modals:
            modal.mount()

        if self.header_builder:
            self.header_builder()

        if self.tabs:
            layers: dict[str, list[StudioTab]] = {}
            for t in self.tabs:
                layers.setdefault(t.layer, []).append(t)
            if len(layers) == 1:
                # Single layer: no layer bar, identical to the flat tab bar.
                self._build_tab_set(self.tabs)
            else:
                with (
                    ui.tabs()
                    .props("dense no-caps align=left indicator-color=transparent")
                    .classes("tui-layer-tabs w-full") as layer_bar
                ):
                    layer_handles = [ui.tab(name) for name in layers]
                with (
                    ui.tab_panels(layer_bar, value=layer_handles[0], on_change=lambda _: trigger_viewport_resize())
                    .props("keep-alive animated=false")
                    .classes("w-full p-0 tui-fill")
                ):
                    for handle, layer_tabs in zip(layer_handles, layers.values()):
                        with ui.tab_panel(handle).classes("p-0 gap-0"):
                            self._build_tab_set(layer_tabs)

        if self.drawer_builder:
            self.drawer_builder()

        if self.on_tick:
            ui.timer(self.tick_interval, lambda: self.on_tick(self.tabs))

    @staticmethod
    def _build_tab_set(tabs: Sequence[StudioTab]) -> None:
        """Tab bar + panels for one layer; numbering restarts per layer ([1], [2], ...)."""
        with (
            ui.tabs()
            .props("dense no-caps align=left indicator-color=transparent")
            .classes("w-full bg-[#0d0f17] border-b border-[#262b3d]") as tab_bar
        ):
            tab_handles = [ui.tab(f"[{idx + 1}] {strip_emoji(t.title)}") for idx, t in enumerate(tabs)]

        with (
            ui.tab_panels(tab_bar, value=tab_handles[0], on_change=lambda _: trigger_viewport_resize())
            .props("keep-alive animated=false")
            .classes("w-full bg-[#090a0f] p-3 tui-fill")
        ):
            for tab_obj, handle in zip(tabs, tab_handles):
                with ui.tab_panel(handle).classes("p-0"):
                    tab_obj.render()

    def mount_page(self, path: str = "/", *, shared: bool = True) -> None:
        if shared:
            self.build_view()
        else:
            @ui.page(path)
            def _page_entry() -> None:
                self.build_view()