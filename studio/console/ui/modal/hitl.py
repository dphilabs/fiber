# fiber.studio.console.ui.modal.hitl
## @lineage: fiber.studio.console.modal.hitl
from __future__ import annotations

from pathlib import Path
import re
from typing import Dict, List
from nicegui import ui
import orjson

from fiber.node.account.state import state, refs
from fiber.studio.console.bridge.sign import oob
from fiber.studio.console.env import get_slack_status
from fiber.studio.console.ui.modal.guide import open_guide_modal
from fiber.studio.console.ui.modal.sim import HitlSimulatorModal, open_hitl_simulator_modal
from fiber.node.bound.guard.preflight import PreflightGuard
from fiber.node.bound.guard.preset import RAW_BUILTIN_PRESETS, PreflightPresetRegistry
import fiber.studio.console.ui.kit as kit
from fiber.studio.console.ui.kit import StudioTab, ModalTemplate
from xphi.node.space.bind.resolver import resolve_path

# Guard mode -> (tag text, tone)
_MODE: dict[str, tuple[str, str]] = {
    "totp": ("[LOCK TOTP]", "amber"),
    "button": ("[1-CLICK]", "cyan"),
    "open": ("[OPEN]", "muted"),
}


def _open_hitl_guide() -> None:
    try:
        open_guide_modal("hitl")  # type: ignore[arg-type]
    except TypeError:
        open_guide_modal()


def _get_anchor_mcp_path() -> Path:
    return resolve_path("anchor") / "mcp.json"


def _load_worker_guard_rows() -> List[Dict[str, str]]:
    p = _get_anchor_mcp_path()
    if not p.is_file():
        return []
    try:
        data = orjson.loads(p.read_bytes())
        servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
        rows: List[Dict[str, str]] = []
        for tid, spec in servers.items():
            if not isinstance(spec, dict) or tid == "fiber-gateway":
                continue
            env_dict = spec.get("env") if isinstance(spec.get("env"), dict) else {}
            raw_pat, _ = PreflightGuard.extract_from_env_dict(env_dict)
            clean_pat, req_totp = PreflightGuard._parse_mode_and_pattern(tid, raw_pat)
            rows.append(
                {
                    "target_id": str(tid),
                    "exec_cmd": str(spec.get("command") or spec.get("url") or ""),
                    "mode": "open" if not clean_pat else ("totp" if req_totp else "button"),
                    "pattern": clean_pat or "",
                }
            )
        return rows
    except Exception:
        return []


def _save_worker_preflight(target_id: str, mode: str, pattern: str) -> bool:
    p = _get_anchor_mcp_path()
    if not p.is_file() or not target_id:
        ui.notify("~/.anchor/mcp.json not found. Sync workers in MCP Gateway tab first.", type="warning")
        return False

    clean_pat = re.sub(
        r"^(totp|button|simple|nototp|1click)\s*:\s*", "", (pattern or "").strip(), flags=re.IGNORECASE
    ).strip()

    if mode != "open" and not clean_pat:
        ui.notify("Enter a regex pattern or click a preset below.", type="warning")
        return False

    if mode != "open" and not clean_pat.startswith("@"):
        try:
            re.compile(clean_pat, re.IGNORECASE)
        except re.error as exc:
            ui.notify(f"Invalid Regex: {exc}", type="negative")
            return False

    try:
        data = orjson.loads(p.read_bytes())
        servers = data.setdefault("mcpServers", {})
        if target_id not in servers or not isinstance(servers[target_id], dict):
            return False

        env_dict = servers[target_id].setdefault("env", {})
        for k in (*PreflightGuard.ENV_KEYS, *PreflightGuard.MODE_ENV_KEYS):
            env_dict.pop(k, None)

        if mode == "button":
            env_dict["FIBER_PREFLIGHT_PATTERN"] = f"button:{clean_pat}"
        elif mode == "totp":
            env_dict["FIBER_PREFLIGHT_PATTERN"] = clean_pat

        p.write_bytes(orjson.dumps(data, option=orjson.OPT_INDENT_2))
        state.security.mcp_topology_dirty = True
        badge = "OPEN (Disarmed)" if mode == "open" else f"{mode.upper()} Guard Armed"
        ui.notify(f"[{target_id}] {badge}", type="positive")
        return True
    except Exception as exc:
        ui.notify(f"Failed to save mcp.json: {exc}", type="negative")
        return False


class ModelCatalogModal(ModalTemplate):
    width_cls = "w-[860px] max-w-[95vw]"
    persistent = False
    title = "LLM Model Pricing & Fuel Catalog"

    def compose(self, dialog: ui.dialog) -> None:
        setattr(refs, "model_catalog_dialog", dialog)
        from fiber.node.account.finops.exchange import default_exchange
        from fiber.node.account.finops.model.filter import CORE_KEYWORDS
        from fiber.node.account.finops.cost.registry import (
            ModelCostRegistry,
            REGISTRY_ROOT,
            DEFAULT_REGISTRY_FILENAME,
        )

        with ui.row().classes("w-full justify-end -mt-2"):
            kit.button("[x]", dialog.close, color="muted", flat=True, size="xs")

        with kit.box(flex=False, width="w-full gap-1"):
            kit.tag("• $1.00 USD = 1,000,000 Fuel  |  $0.000001 (1 μ$) = 1.0 Fuel", "green", framed=False)
            kit.tag("• Rate Par     : Price ($ / 1M Tokens) == Burn Rate (Fuel / Token)", "amber", framed=False)
            kit.tag("• Priced Models: Consumed Fuel = ceil(Billed_USD × 1,000,000)", "fg", framed=False)
            kit.tag("• Local Models : Consumed Fuel = ceil(Total_Tokens × 1.0) [when $0.0]", "cyan", framed=False)

        total_mapped = len(getattr(ModelCostRegistry, "_model_cost", {}) or {})
        usd_rate = getattr(default_exchange, "usd_rate", 1_000_000)

        with kit.bar():
            with ui.row().classes("items-center gap-1.5 no-wrap"):
                kit.tag(f"[MODELS {total_mapped:,}]", "cyan").classes("font-bold")
                kit.tag(f"[PROVIDERS {len(CORE_KEYWORDS)}]", "green").classes("font-bold")
                kit.tag(f"[$1 = {usd_rate:,} FUEL]", "amber")
            kit.file_link(REGISTRY_ROOT / DEFAULT_REGISTRY_FILENAME, label=DEFAULT_REGISTRY_FILENAME, create_if_missing=True)

        columns = [
            {"name": "model", "label": "Model", "field": "model", "align": "left", "sortable": True},
            {"name": "provider", "label": "Provider", "field": "provider", "align": "left", "sortable": True},
            {"name": "in_usd", "label": "In ($/1M)", "field": "input_usd_per_1m", "align": "right", "sortable": True},
            {"name": "out_usd", "label": "Out ($/1M)", "field": "output_usd_per_1m", "align": "right", "sortable": True},
            {"name": "in_tok", "label": "In (Fuel/tok)", "field": "input_fuel_per_tok", "align": "right", "sortable": True},
            {"name": "out_tok", "label": "Out (Fuel/tok)", "field": "output_fuel_per_tok", "align": "right", "sortable": True},
            {"name": "mode", "label": "Mode", "field": "billing_mode", "align": "left"},
        ]
        ui.table(columns=columns, rows=default_exchange.build_catalog_rows(limit=30), row_key="model").props(
            "dense flat bordered dark square virtual-scroll rows-per-page=0"
        ).classes("w-full h-80 text-xs font-mono")


def open_model_catalog_modal() -> None:
    dlg = getattr(refs, "model_catalog_dialog", None) or ModelCatalogModal().mount()
    dlg.open()


class HitlNSettingTab(StudioTab):
    title = "HITL"
    left_header = "MCP Guard - PreFlight"
    right_header = "Slack HITL & Admin Identity"
    ratio = 52

    def __init__(self) -> None:
        self._workers_box: ui.column | None = None
        self._summary_label: kit.Tag | None = None
        self._edit_dialog: ui.dialog | None = None
        self._edit_target_label: kit.Tag | None = None
        self._edit_mode_select: ui.select | None = None
        self._edit_pattern_input: ui.input | None = None
        self._editing_target_id: str = ""

    def _mount_local_dialogs(self) -> None:
        if getattr(refs, "model_catalog_dialog", None) is None:
            ModelCatalogModal().mount()
        if getattr(refs, "hitl_sim_dialog", None) is None:
            HitlSimulatorModal().mount()

        # Transparent shell + inner legend box (q-dialog would clip a legend on the card itself).
        with ui.dialog() as dlg, ui.card().classes("tui-modal-shell w-[500px] max-w-[95vw] p-0 pt-2.5 gap-0"):
            self._edit_dialog = dlg
            with kit.box(title="Pre-Flight Guard", flex=False, width="w-full gap-2.5"):
                with ui.row().classes("w-full items-center justify-between border-b border-[#262b3d] pb-1.5"):
                    self._edit_target_label = kit.tag("target: -", "cyan", framed=False).classes(
                        "text-sm font-bold"
                    )
                    kit.button("[x]", dlg.close, color="muted", flat=True, size="xs")

                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    self._edit_mode_select = ui.select(
                        options={"totp": "TOTP", "button": "1-Click Button", "open": "Open (Disarmed)"},
                        value="totp",
                        label="Worker Auth Mode",
                    ).props("dense outlined dark square options-dense").classes("w-44 text-xs font-mono")

                    self._edit_pattern_input = ui.input(
                        label="Intercept Regex Pattern",
                        placeholder=r"\b(DROP|TRUNCATE|DELETE)\b",
                    ).props("dense outlined dark square").classes("flex-grow text-xs font-mono")

                kit.tag("Presets (Click to populate mode & regex):", "muted", framed=False)
                with ui.row().classes("w-full items-center gap-1.5 flex-wrap"):
                    for p in PreflightPresetRegistry.load_all().values():
                        kit.button(
                            p.label,
                            lambda m=p.mode, pat=p.pattern: self._set_dialog_preset(m, pat),
                            color="amber" if p.mode == "totp" else "cyan",
                            outline=True,
                        ).tooltip(f"{p.description}\nRegex: /{p.pattern}/i")

                with ui.row().classes("w-full justify-between items-center mt-2 pt-2 border-t border-[#262b3d]"):
                    kit.button("Disarm (Set Open)", lambda: self._set_dialog_preset("open", ""), color="red", outline=True)
                    kit.button("Save Guard", self._save_dialog_changes, color="green")

    def _set_dialog_preset(self, mode: str, pattern: str) -> None:
        if self._edit_mode_select:
            self._edit_mode_select.value = mode
        if self._edit_pattern_input:
            self._edit_pattern_input.value = pattern

    def _open_edit_dialog(self, row: Dict[str, str]) -> None:
        self._editing_target_id = row["target_id"]
        if self._edit_target_label:
            self._edit_target_label.set_text(f"target: {row['target_id']}")
        if self._edit_mode_select:
            self._edit_mode_select.value = row["mode"] if row["mode"] != "open" else "totp"
        if self._edit_pattern_input:
            self._edit_pattern_input.value = row["pattern"]
        if self._edit_dialog:
            self._edit_dialog.open()

    def _save_dialog_changes(self) -> None:
        mode = self._edit_mode_select.value if self._edit_mode_select else "totp"
        pat = self._edit_pattern_input.value if self._edit_pattern_input else ""
        if _save_worker_preflight(self._editing_target_id, mode, pat):
            if self._edit_dialog:
                self._edit_dialog.close()
            self._render_worker_guard_list()

    def _render_worker_guard_list(self) -> None:
        if self._workers_box is None:
            return
        rows = _load_worker_guard_rows()
        self._workers_box.clear()

        totp_cnt = sum(1 for r in rows if r["mode"] == "totp")
        btn_cnt = sum(1 for r in rows if r["mode"] == "button")
        open_cnt = sum(1 for r in rows if r["mode"] == "open")

        if self._summary_label:
            self._summary_label.set_text(f"{totp_cnt} TOTP · {btn_cnt} 1-CLICK · {open_cnt} OPEN")

        with self._workers_box:
            if not rows:
                kit.tag(
                    "No MCP workers in ~/.anchor/mcp.json. Sync in 'MCP Gateway' tab first.", "muted", framed=False
                ).classes("whitespace-normal py-2")
                return

            for r in rows:
                mode, pat = r["mode"], r["pattern"]
                badge_txt, tone = _MODE.get(mode, _MODE["open"])

                with kit.box(flex=False, width="w-full gap-1"):
                    with ui.row().classes("w-full items-center justify-between no-wrap"):
                        with ui.row().classes("items-center gap-1.5 overflow-hidden"):
                            kit.tag(r["target_id"], "fg", framed=False, tooltip=r["exec_cmd"]).classes(
                                "font-bold truncate"
                            )
                            kit.tag(badge_txt, tone).classes("text-[10px]")
                        kit.button("Configure", lambda rc=r: self._open_edit_dialog(rc), color="muted", outline=True)

                    if pat:
                        kit.tag(f"/{pat}/i", tone, tooltip=f"/{pat}/i").classes("truncate max-w-full")
                    else:
                        kit.tag("Unarmed (All tool calls pass through)", "muted", framed=False, tooltip="Unarmed")

    def compose_left(self) -> None:
        self._mount_local_dialogs()
        default_rules = orjson.dumps({"presets": RAW_BUILTIN_PRESETS, "disabled_presets": []}, option=orjson.OPT_INDENT_2).decode()

        with kit.bar():
            self._summary_label = kit.tag("0 TOTP · 0 1-CLICK · 0 OPEN", "cyan", framed=False).classes("font-bold")
            with ui.row().classes("items-center gap-1.5"):
                kit.file_link(
                    PreflightPresetRegistry.get_registry_path(),
                    label="preflight_rules.json",
                    default_content=default_rules,
                    create_if_missing=True,
                )
                kit.button("[↻]", self._render_worker_guard_list, color="cyan", flat=True, size="xs")

        self._workers_box = ui.column().classes("w-full gap-1.5 flex-grow max-h-80 overflow-y-auto pr-1")
        self._render_worker_guard_list()

    def compose_right(self) -> None:
        s = state.security
        from fiber.node.account.finops.cost.registry import ModelCostRegistry, REGISTRY_ROOT, DEFAULT_REGISTRY_FILENAME

        slk = get_slack_status()
        if slk["channel"]:
            s.slack_channel = slk["channel"]

        with kit.bar():
            kit.tag(bind=(s, "gateway_health_badge_text"), tone="green", framed=False).classes("font-bold")
            kit.tag(bind=(s, "slack_oob_badge_text"), tone="cyan", framed=False).classes("font-bold")

        kit.section("Slack OOB Approval Bridge", "amber")
        with kit.bar():
            with ui.row().classes("items-center gap-2 overflow-hidden no-wrap"):
                kit.tag("[● LIVE]" if slk["ok"] else "[○ UNSET]", "green" if slk["ok"] else "amber").classes("font-bold")
                kit.tag(slk["meta"], "fg", framed=False).classes("truncate")
            kit.button("Setup Guide", _open_hitl_guide, color="amber", outline=True)

        kit.section("Admin Identity & TOTP Keychain").classes("mt-1")
        with kit.box(flex=False, width="w-full gap-2"):
            kit.text("Mapped SPIFFE ID", s, "admin_user", width="w-full")
            with kit.row():
                kit.button("Key Init", oob.init_node_keys, color="muted", outline=True)
                kit.button("Enroll TOTP", oob.enroll_admin_totp, color="cyan", flex=True)
                kit.button("Verify TOTP", oob.verify_admin_totp, color="green", outline=True, flex=True)
                kit.button("Revoke", oob.revoke_admin_totp, color="red", outline=True)

        total_mapped = len(getattr(ModelCostRegistry, "_model_cost", {}) or {})
        with kit.bar():
            with ui.row().classes("items-center gap-1.5"):
                kit.tag("LLM Model Catalog", "muted", framed=False)
                kit.tag(f"[{total_mapped:,} MODELS]", "cyan")
            with ui.row().classes("items-center gap-1.5"):
                kit.button("Pricing Table", open_model_catalog_modal, color="cyan", outline=True)

        with kit.footer():
            kit.button(
                "Open HITL Simulator (Console / Slack)",
                open_hitl_simulator_modal,
                color="amber",
                flex=True,
            )

    def flush(self) -> None:
        if getattr(state.security, "mcp_topology_dirty", False):
            self._render_worker_guard_list()


class HitlSettingsModal(ModalTemplate):
    """HITL settings opened from the header [HITL] key (replaces the former HITL tab).

    Mount after HitlSimulatorModal so the tab reuses refs.hitl_sim_dialog instead of creating a second one.
    """
    width_cls = "w-[1280px] max-w-[96vw]"
    title = "HITL · MCP Guard & Admin Identity"

    def __init__(self) -> None:
        self._tab = HitlNSettingTab()

    def mount(self) -> ui.dialog:
        dialog = super().mount()
        setattr(refs, "hitl_settings_dialog", dialog)
        # Re-read guard rows on every open (no tab flush() is driven for modals).
        dialog.on_value_change(lambda e: self._tab._render_worker_guard_list() if e.value else None)
        return dialog

    def compose(self, dialog: ui.dialog) -> None:
        with ui.row().classes("w-full justify-end -mt-2"):
            kit.button("[x]", dialog.close, color="muted", flat=True, size="xs")
        self._tab.render()