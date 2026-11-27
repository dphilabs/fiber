# fiber.studio.console.modal.sim
from __future__ import annotations

from nicegui import ui

from fiber.node.account.state import state, refs
from fiber.studio.console.bridge.sign import oob
from fiber.studio.console.env import get_slack_status
from fiber.studio.console.modal.guide import open_guide_modal
import fiber.studio.console.ui.kit as kit
from fiber.studio.console.ui.kit import ModalTemplate

SIM_TARGET = "postgres-prod-db"
SIM_TOOL = "drop_table"
SIM_ARGS = {"table": "customer_ledgers", "cascade": True}

def _open_hitl_guide() -> None:
    try:
        open_guide_modal("hitl")  # type: ignore[arg-type]
    except TypeError:
        open_guide_modal()

async def _run_console_sim(dialog: ui.dialog) -> None:
    dialog.close()
    await oob.open_hitl_modal(f"{SIM_TARGET}.{SIM_TOOL}", SIM_ARGS)

async def _run_slack_sim() -> None:
    st = get_slack_status()
    if not st["ok"]:
        ui.notify(f"[SLACK] Missing: {', '.join(st['missing'])}", type="warning", position="top")
        _open_hitl_guide()
        return

    state.security.slack_channel = st["channel"]
    await oob.dispatch_slack_oob(SIM_TARGET, SIM_TOOL, SIM_ARGS)


class HitlSimulatorModal(ModalTemplate):
    width_cls = "w-[460px] max-w-[95vw]"
    border_cls = "tui-border-amber"
    persistent = False
    title = "HITL Dry-Run Simulator"

    def compose(self, dialog: ui.dialog) -> None:
        setattr(refs, "hitl_sim_dialog", dialog)
        s = state.security

        with ui.row().classes("w-full justify-end -mt-2"):
            kit.button("[x]", dialog.close, color="muted", flat=True, size="xs")

        kit.tag(f"Payload: {SIM_TARGET}.{SIM_TOOL}({SIM_ARGS})", "muted").classes("w-full truncate")

        with kit.bar():
            kit.tag(bind=(s, "admin_user", lambda u: f"Attester: {u}"), tone="cyan", framed=False)
            kit.switch("Require 6-Digit TOTP", s, "oob_require_totp", color="cyan")

        with ui.row().classes("w-full items-center gap-2 mt-1"):
            kit.button(
                "Console Popup",
                lambda: _run_console_sim(dialog),
                color="amber",
                flex=True,
            )
            kit.button(
                "Slack OOB Test",
                _run_slack_sim,
                color="cyan",
                outline=True,
                flex=True,
            )

def open_hitl_simulator_modal() -> None:
    dlg = getattr(refs, "hitl_sim_dialog", None) or HitlSimulatorModal().mount()
    dlg.open()