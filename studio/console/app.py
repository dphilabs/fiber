# fiber.studio.console.app
from __future__ import annotations

import asyncio
import importlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Optional, Sequence
import webbrowser
from nicegui import app, ui

from fiber.node.account.state import (
    STATE_FILE_PATH,
    load_or_init_state,
    refs,
    save_state_to_disk,
    state,
)
from fiber.node.account.finops.console import finops_manager
from fiber.studio.console.bridge.sign import oob
from fiber.studio.console.bridge.mcp import mcp

from fiber.studio.console.tab.mcp import McpGatewayTab, HitlModal, QrModal
from fiber.studio.console.tab.guardrail import LLMGuardrailTab
from fiber.studio.console.tab.metering import MeteringTab
from fiber.studio.console.tab.vcr import VcrTab
from fiber.studio.console.ui.modal.hitl import HitlSettingsModal

from fiber.studio.console.ui.modal.guide import GuideModal
from fiber.studio.console.ui.modal.sim import HitlSimulatorModal
import fiber.studio.console.observer.plane as plane
import fiber.studio.console.ui.diag as diag
import fiber.studio.console.ui.header as header
import fiber.studio.console.ui.kit as kit
from fiber.studio.console.ui.kit import ConsoleTemplate, StudioTab, ModalTemplate
from xphi.node.space.bind.resolver import resolve_path

DAEMON_PORT: int = 8356
_tick_counter: int = 0
_bg_tasks: list[asyncio.Task] = []
_managed_daemon_proc: Optional[subprocess.Popen] = None

async def _sync_initial_node_and_account_state() -> None:
    await oob.refresh_sign_status(silent=True)
    await finops_manager.refresh_account_status(silent=True)
    await mcp.scan_mcp_topology_and_health(silent=True)

def _flush_100ms_ringbuffer() -> None:
    f, s, p = state.finops, state.security, state.pipeline

    if refs.cli_buffer and refs.cli_log:
        refs.cli_log.push("\n".join(refs.cli_buffer))
        refs.cli_buffer.clear()

    if refs.pubsub_buffer and refs.pubsub_log:
        refs.pubsub_log.push("\n".join(refs.pubsub_buffer))
        refs.pubsub_buffer.clear()

    if f.chart_dirty and refs.finops_chart:
        if not f.x_ticks:
            f.x_ticks.append(time.strftime("%H:%M:%S"))
            f.y_fuel.append(f.ledger_balance)
            f.y_core.append(f.fuel_budget)
        opts = refs.finops_chart.options
        if isinstance(opts.get("xAxis"), dict):
            opts["xAxis"]["data"] = list(f.x_ticks)
        series = opts.get("series")
        if isinstance(series, list) and len(series) >= 2:
            series[0]["data"] = list(f.y_fuel)
            mark_line = series[0].get("markLine")
            if isinstance(mark_line, dict) and isinstance(mark_line.get("data"), list) and mark_line["data"]:
                mark_line["data"][0]["yAxis"] = f.fuel_budget
            series[1]["data"] = list(f.y_core)
        refs.finops_chart.update()
        f.chart_dirty = False

    if s.xray_chart_dirty and refs.xray_chart:
        refs.xray_chart.options["series"][0]["data"] = list(s.xray_latencies)
        refs.xray_chart.update()
        s.xray_chart_dirty = False

    if p.vcr_chart_dirty and refs.vcr_chart:
        refs.vcr_chart.options["series"][0]["data"][0]["value"] = p.vcr_bench_replay_ms
        refs.vcr_chart.options["series"][0]["data"][1]["value"] = p.vcr_bench_live_ms
        refs.vcr_chart.update()
        p.vcr_chart_dirty = False

def _open_app_window(url: str) -> None:
    for path in (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    ):
        try:
            subprocess.Popen([path, f"--app={url}", "--window-size=1400,900"])
            return
        except FileNotFoundError:
            continue
    webbrowser.open(url)

def _is_port_active(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0

def _ensure_local_daemon(env_file: Optional[str] = None) -> None:
    """Spawns a managed background daemon (:8356) if not already running via brew services or CLI."""
    global _managed_daemon_proc
    if _is_port_active(DAEMON_PORT):
        diag.info("Attached to running Fiber Gateway Daemon", port=DAEMON_PORT)
        return

    try:
        log_dir = Path(str(resolve_path("log")))
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "daemon.log"
        cmd = [sys.executable, "-m", "fiber.studio.cli.main", "daemon", "-s", "all"]
        if env_file:
            cmd.extend(["-f", env_file])

        child_env = {k: v for k, v in os.environ.items() if k != "FIBER_CONSOLE_ACTIVE"}
        out = open(log_file, "a", encoding="utf-8")
        _managed_daemon_proc = subprocess.Popen(cmd, stdout=out, stderr=out, env=child_env)
        diag.info("Auto-spawned managed Fiber Gateway Daemon for Console", pid=_managed_daemon_proc.pid, port=DAEMON_PORT)
    except Exception as exc:
        diag.exception("Failed to auto-spawn Fiber Gateway Daemon", exc=exc)

def _build_drawer() -> None:
    """Dual log terminals: CLI stdout (left) and control-plane event bus (right)."""
    with kit.dual_drawer() as (left_col, right_col):
        with left_col:
            ui.label(" stdout // cli ").classes("tui-legend-title")
            with ui.row().classes("tui-legend-actions"):
                kit.button(
                    "[CLEAR]",
                    lambda: refs.cli_log.clear() if refs.cli_log else None,
                    color="grey-6",
                    flat=True,
                ).classes("px-1")
            refs.cli_log = kit.log(max_lines=250, color="tui-text-fg", height="h-64", bordered=False)

        with right_col:
            ui.label(" event_bus // finops:* mcp:* sign:* ").classes("tui-legend-title tui-text-amber")
            with ui.row().classes("tui-legend-actions"):
                ui.label().bind_text_from(state.pipeline, "pubsub_eps", backward=lambda n: f"[EVT {n}]").classes(
                    "tui-tag tui-text-amber"
                )
                kit.button(
                    "[CLEAR]",
                    lambda: refs.pubsub_log.clear() if refs.pubsub_log else None,
                    color="grey-6",
                    flat=True,
                ).classes("px-1")
            refs.pubsub_log = kit.log(max_lines=250, color="tui-text-amber", height="h-64", bordered=False)

    refs.push_cli(
        f"Fiber-Console initialized (State file: {STATE_FILE_PATH}). Syncing local keys, accounts, and MCP topology..."
    )

def _on_tick(tabs: Sequence[StudioTab]) -> None:
    """Flush 100ms ringbuffers, trigger dirty tab re-renders, and auto-sync state.json every 5s (50 ticks)."""
    global _tick_counter
    try:
        _flush_100ms_ringbuffer()
        for t in tabs:
            try:
                t.flush()
            except Exception as tab_exc:
                diag.exception(f"Tab flush error in '{getattr(t, 'title', 'unknown')}'", exc=tab_exc)

        _tick_counter = (_tick_counter + 1) % 50
        if _tick_counter == 0:
            if save_state_to_disk(force=False):
                diag.debug("Auto-synced modified console state to disk", path=str(STATE_FILE_PATH))
    except Exception as exc:
        diag.exception("100ms tick flush error", exc=exc)

def build_runbook_tabs() -> list[StudioTab]:
    from fiber.studio.console.runbook.catalog import RunbookCatalogTab
    from fiber.studio.console.runbook.draft import RunbookDraftTab

    draft_tab: RunbookDraftTab
    cat_tab = RunbookCatalogTab(on_edit_draft=lambda f, v: draft_tab.load_external(f, v))
    draft_tab = RunbookDraftTab(on_state_mutated=cat_tab.refresh_state)
    return [cat_tab, draft_tab]

# def build_runbook_tabs() -> list[StudioTab]:
#     from fiber.studio.console.runbook.catalog import RunbookCatalogTab
#     from fiber.studio.console.runbook.draft import RunbookDraftTab
#     from fiber.studio.console.runbook.trigger import RunbookTriggerTab

#     trig_tab: RunbookTriggerTab
#     draft_tab: RunbookDraftTab
#     cat_tab = RunbookCatalogTab(
#         on_edit_draft=lambda f, v: draft_tab.load_external(f, v),
#         on_open_trigger=lambda f: trig_tab.focus_flow(f),
#         on_state_mutated=lambda: trig_tab.refresh_state(),
#     )
#     trig_tab = RunbookTriggerTab(on_state_mutated=cat_tab.refresh_state)
#     draft_tab = RunbookDraftTab(on_state_mutated=lambda: (cat_tab.refresh_state(), trig_tab.refresh_state()))
#     return [cat_tab, trig_tab, draft_tab]

def build_tabs(*, runbook: bool = False) -> list[StudioTab]:
    tabs: list[StudioTab] = [McpGatewayTab(), LLMGuardrailTab(), VcrTab(), MeteringTab()]
    if runbook:
        tabs += build_runbook_tabs()
    return tabs

def build_modals() -> list[ModalTemplate]:
    # HitlSettingsModal last: its tab reuses dialogs mounted above (refs.hitl_sim_dialog).
    return [HitlModal(), QrModal(), GuideModal(), HitlSimulatorModal(), HitlSettingsModal()]

def build_console_shell(*, runbook: bool = False) -> ConsoleTemplate:
    shell = ConsoleTemplate(
        tabs=build_tabs(runbook=runbook),
        modals=build_modals(),
        header_builder=header.build_header,
        drawer_builder=_build_drawer,
        on_tick=_on_tick,
        tick_interval=0.1,
    )
    shell.mount_page("/", shared=False)
    diag.info("Console UI shell mounted", tabs=len(shell.tabs), modals=len(shell.modals))
    return shell

async def _delayed_open_window(url: str) -> None:
    """Wait briefly for Uvicorn socket binding before opening the app window."""
    await asyncio.sleep(0.4)
    _open_app_window(url)

async def _on_startup(url: str) -> None:
    """Initialize node/account/MCP state and spawn non-blocking background workers."""
    await _sync_initial_node_and_account_state()
    _bg_tasks.append(asyncio.create_task(header.ensure_console_compats(silent=True), name="fiber-compats-sync"))
    _bg_tasks.append(asyncio.create_task(plane.tunnel_pubsub_worker(), name="fiber-tunnel-pubsub"))
    _bg_tasks.append(asyncio.create_task(mcp.mcp_gateway_health_worker(), name="fiber-mcp-health"))
    _bg_tasks.append(asyncio.create_task(_delayed_open_window(url), name="fiber-open-window"))

async def _on_shutdown() -> None:
    """Cancel background polling tasks cleanly, stop managed daemon if spawned, and persist final state to disk."""
    global _managed_daemon_proc
    for task in _bg_tasks:
        if not task.done():
            task.cancel()
    if _bg_tasks:
        await asyncio.gather(*_bg_tasks, return_exceptions=True)
    _bg_tasks.clear()

    if _managed_daemon_proc is not None and _managed_daemon_proc.poll() is None:
        _managed_daemon_proc.terminate()
        _managed_daemon_proc = None

    save_state_to_disk(force=False)

def launch_console(
    port: int = 8357,
    auto_daemon: bool = True,
    env_file: Optional[str] = None,
    runbook: bool = False,
) -> None:
    if os.environ.get("FIBER_CONSOLE_ACTIVE") == "1":
        return

    url = f"http://127.0.0.1:{port}"
    if _is_port_active(port):
        print(f"Attached to running Fiber instance at {url}")
        diag.info("Attached to existing Fiber Console instance", url=url)
        _open_app_window(url)
        return

    if auto_daemon:
        _ensure_local_daemon(env_file=env_file)

    os.environ["FIBER_CONSOLE_ACTIVE"] = "1"

    loaded = load_or_init_state()
    diag.info("Console state initialized", path=str(STATE_FILE_PATH), loaded_existing=loaded)

    build_console_shell(runbook=runbook)
    app.on_exception(lambda exc: diag.exception("Unhandled NiceGUI server exception", exc=exc))
    app.on_startup(lambda: _on_startup(url))
    app.on_shutdown(_on_shutdown)
    diag.info("Launching Fiber Control Studio server", host="127.0.0.1", port=port)
    ui.run(host="127.0.0.1", port=port, title="Fiber Control Studio", show=False, reload=False)

if __name__ == "__main__":
    launch_console(port=8357)