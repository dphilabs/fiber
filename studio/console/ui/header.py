# fiber.studio.console.ui.header
from __future__ import annotations

import asyncio
import io
from pathlib import Path
import tarfile
import urllib.request
from nicegui import ui

from fiber.node.account.state import (
    STATE_FILE_PATH,
    load_or_init_state,
    refs,
    save_state_to_disk,
    state,
)
from fiber.node.account.finops.console import finops_manager
import fiber.studio.console.ui.diag as diag
from fiber.studio.console.modal.guide import open_guide_modal
import fiber.studio.console.ui.kit as kit
from fiber.studio.console.ui.theme import strip_emoji, tui_tag
from xphi.node.space.bind.resolver import resolve_path

DIAG_LOG_PATH = resolve_path("log") / "ui_diag.log"
COMPATS_REPO = "dphilabs/fiber-compats"
COMPATS_TARBALL_URL = f"https://github.com/{COMPATS_REPO}/archive/refs/heads/main.tar.gz"
COMPATS_LOCAL_DIR = Path(str(resolve_path("compats")))

_sync_lock = asyncio.Lock()


def has_console_fixtures() -> bool:
    """True if console fixtures exist in the local compats dir."""
    console_dir = COMPATS_LOCAL_DIR / "console"
    return console_dir.exists() and any(console_dir.rglob("*.json"))


def _update_compats_badge_state(status: str) -> None:
    setattr(state.pipeline, "compats_badge_text", status)


def _download_and_extract_compats() -> int:
    """Download the fiber-compats tarball and extract it into the local compats dir."""
    COMPATS_LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(COMPATS_TARBALL_URL, headers={"User-Agent": "Fiber-Control-Studio"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw_bytes = resp.read()

    extracted_count = 0
    with tarfile.open(fileobj=io.BytesIO(raw_bytes), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or "/" not in member.name:
                continue
            # Strip the top-level archive dir (fiber-compats-main/)
            rel_path = member.name.split("/", 1)[1]
            if not rel_path or ".." in Path(rel_path).parts:
                continue
            dest_path = COMPATS_LOCAL_DIR / rel_path
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            if f_obj := tar.extractfile(member):
                dest_path.write_bytes(f_obj.read())
                if dest_path.suffix == ".json":
                    extracted_count += 1
    return extracted_count


async def ensure_console_compats(*, force: bool = False, silent: bool = False) -> bool:
    """Fetch fiber-compats console fixtures if missing.

    silent: quiet prefetch on boot. force: re-sync on header tag click.
    """
    if not hasattr(state.pipeline, "compats_badge_text"):
        _update_compats_badge_state("● Compats: Ready" if has_console_fixtures() else "○ Compats: Missing")

    if not force and has_console_fixtures():
        _update_compats_badge_state("● Compats: Ready")
        return True

    if _sync_lock.locked():
        return False

    async with _sync_lock:
        _update_compats_badge_state("~ Compats: Syncing")
        if not silent:
            ui.notify(f"[SYNC] Syncing fixtures from {COMPATS_REPO}...", type="info")
        refs.push_cli(f"$ fetch https://github.com/{COMPATS_REPO} -> {COMPATS_LOCAL_DIR}")

        try:
            count = await asyncio.to_thread(_download_and_extract_compats)
            _update_compats_badge_state(f"● Compats: Ready ({count})")
            refs.push_cli(f"  [OK] fiber-compats: synced {count} fixture/compat JSON files into {COMPATS_LOCAL_DIR}")
            if not silent:
                ui.notify(f"[OK] Synced {count} fixtures from fiber-compats", type="positive")
            return True
        except Exception as exc:
            _update_compats_badge_state("! Compats: Offline (click to retry)")
            refs.push_cli(f"  [WARN] fiber-compats: sync failed: {exc}")
            diag.exception("Failed to sync fiber-compats", exc=exc)
            if not silent:
                ui.notify(f"[WARN] Failed to download fiber-compats: {exc}", type="warning")
            return False


def act_manual_save_state() -> None:
    """Persist current console state to disk immediately."""
    ok = save_state_to_disk(force=True)
    if ok:
        refs.push_cli(f"[SAVE] Saved state to {STATE_FILE_PATH} (at {state.last_saved_at})")
        diag.info("state manually saved", path=str(STATE_FILE_PATH))
        ui.notify(f"[SAVE] Saved to {STATE_FILE_PATH.name}", type="positive")
    else:
        diag.error("Failed to save state", path=str(STATE_FILE_PATH))
        ui.notify("[ERR] Failed to save state.json", type="negative")


def act_reload_state_from_disk() -> None:
    """Reload state.json after external editor modifications."""
    if load_or_init_state(STATE_FILE_PATH):
        state.finops.chart_dirty = True
        refs.push_cli(f"[RELOAD] Reloaded state from {STATE_FILE_PATH}")
        diag.info("State reloaded from disk", path=str(STATE_FILE_PATH))
        ui.notify(f"[RELOAD] Reloaded {STATE_FILE_PATH.name} from disk", type="info")
    else:
        ui.notify("[WARN] Failed to reload state file", type="warning")


def _open_hitl_settings() -> None:
    """Open the HITL settings modal mounted by the console shell (refs only; no tab import)."""
    dlg = getattr(refs, "hitl_settings_dialog", None)
    if dlg is None:
        ui.notify("[WARN] HITL settings are not mounted", type="warning")
        return
    dlg.open()


def build_header() -> None:
    """Two-row tmux-style statusline.

    Row 1: identity + status tags | HITL settings + guide + emergency stream break.
    Row 2: live KPIs | state file controls | diag controls.
    """
    diag.install_browser_hooks()
    if not hasattr(state.pipeline, "compats_badge_text"):
        _update_compats_badge_state("● Compats: Ready" if has_console_fixtures() else "○ Compats: Sync")

    with ui.header().classes("tui-header px-4 py-1.5"):
        with ui.column().classes("w-full gap-1"):
            # Row 1: identity & emergency
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                with ui.row().classes("items-center gap-2 no-wrap"):
                    with ui.row().classes("items-center gap-0 no-wrap"):
                        ui.label("▌").classes("tui-title tui-text-green")
                        ui.label("Fiber Control Plane").classes("tui-title")
                    # Tags are formatted at render time; legacy emoji state values are normalized by tui_tag.
                    ui.label().bind_text_from(state, "tunnel_badge_text", backward=tui_tag).classes(
                        "tui-tag tui-text-green"
                    )
                    ui.label().bind_text_from(state.security, "ssh_badge_text", backward=tui_tag).classes(
                        "tui-tag tui-text-cyan"
                    )
                    # fiber-compats status; click to force re-sync
                    compats_badge = (
                        ui.label()
                        .bind_text_from(state.pipeline, "compats_badge_text", backward=tui_tag)
                        .classes("tui-tag tui-text-amber cursor-pointer")
                        .tooltip(f"Click to sync latest model fixtures from github.com/{COMPATS_REPO}")
                    )
                    compats_badge.on("click", lambda: asyncio.create_task(ensure_console_compats(force=True, silent=False)))

                with ui.row().classes("items-center gap-2 no-wrap"):
                    kit.button(
                        "[HITL]",
                        _open_hitl_settings,
                        color="amber",
                        outline=True,
                    ).tooltip("MCP pre-flight guards, Slack OOB approval & admin TOTP identity")

                    kit.button(
                        "[? GUIDE]",
                        open_guide_modal,
                        color="info",
                        outline=True,
                    ).tooltip("IDE LLM Gateway & MCP Gateway configuration guide")

                    kit.button(
                        "[! STREAM BREAK 10s]",
                        finops_manager.temporary_stream_kill,
                        color="negative",
                    ).classes("font-bold px-3").tooltip(
                        "Sever active streams immediately (0 Fuel) and auto-restore previous balance after 10s"
                    )

            # Row 2: telemetry strip
            with ui.row().classes("tui-statusline w-full items-center justify-between no-wrap"):
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.label().bind_text_from(state, "fuel_kpi_text", backward=strip_emoji).classes(
                        "text-xs font-bold tui-text-green"
                    )
                    ui.label("│").classes("tui-sep")
                    ui.label().bind_text_from(state, "saved_kpi_text", backward=strip_emoji).classes(
                        "text-xs font-bold tui-text-cyan"
                    )

                with ui.row().classes("items-center gap-1.5 no-wrap"):
                    ui.label().bind_text_from(
                        state,
                        "last_saved_at",
                        backward=lambda ts: f"STATE {STATE_FILE_PATH.name} @ {ts}",
                    ).classes("text-[11px] tui-text-muted")
                    kit.file_link(
                        STATE_FILE_PATH,
                        label="",
                        tooltip=f"Edit {STATE_FILE_PATH} in external editor",
                    )
                    kit.button("[RELOAD]", act_reload_state_from_disk, color="info", flat=True).classes(
                        "px-1"
                    ).tooltip(f"Reload modified {STATE_FILE_PATH.name} from disk")
                    kit.button("[SAVE]", act_manual_save_state, color="positive", outline=True).classes("px-2")

                    ui.label("│").classes("tui-sep mx-1")
                    ui.label(f"DIAG {DIAG_LOG_PATH.name}").classes("text-[11px] tui-text-muted")
                    kit.file_link(
                        DIAG_LOG_PATH,
                        label="",
                        tooltip=f"Open UI diagnostic log ({DIAG_LOG_PATH})",
                        default_content="",
                    )
                    kit.button(
                        "[SNAP]",
                        lambda: diag.snapshot("header_btn"),
                        color="info",
                        outline=True,
                    ).classes("px-2")