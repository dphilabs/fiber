# fiber.studio.console.tab.mcp
from __future__ import annotations

import asyncio
from pathlib import Path

from nicegui import ui
import orjson

from fiber.gateway.bridge.action.connect import run_connect_copy
from fiber.gateway.bridge.console import run_fiber_cli
from fiber.node.account.state import refs, state
from fiber.node.mcp.mapper.builtin import list_builtin_entries
from fiber.node.mcp.mapper.spec import ANCHOR_MCP_PATH
from fiber.studio.console.bridge.mcp import mcp
from fiber.studio.console.bridge.sign import oob
import fiber.studio.console.ui.kit as kit
from fiber.studio.console.ui.kit import ModalTemplate, StudioTab

DEFAULT_ANCHOR_MCP_JSON = '{\n  "mcpServers": {}\n}\n'

# Worker status -> (tag text, tone, tooltip). Tooltip may use {mode}.
_STATUS: dict[str, tuple[str, str, str]] = {
    "running": ("[● RUN]", "green", "Process running in McpWorkerDaemon (Mode: {mode})"),
    "standby": ("[~ STBY]", "cyan", "Mounted in Daemon (Ephemeral Zero-Spawn: boots on tool call)"),
    "config": ("[○ CFG]", "amber", "Registered in ~/.anchor/mcp.json (Waiting for daemon or run standalone)"),
}

def _read_anchor_mcp_text() -> str:
    if not ANCHOR_MCP_PATH.is_file():
        return DEFAULT_ANCHOR_MCP_JSON
    try:
        return ANCHOR_MCP_PATH.read_text(encoding="utf-8")
    except Exception:
        return DEFAULT_ANCHOR_MCP_JSON


def _load_anchor_specs_map() -> dict[str, dict]:
    """Read raw mcpServers dictionary from ~/.anchor/mcp.json."""
    if not ANCHOR_MCP_PATH.is_file():
        return {}
    try:
        data = orjson.loads(ANCHOR_MCP_PATH.read_bytes())
        servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
        return servers if isinstance(servers, dict) else {}
    except Exception:
        return {}


class HitlModal(ModalTemplate):
    width_cls = "w-[520px]"
    border_cls = "tui-border-amber"
    persistent = True
    title = "HTTP 202 · HITL Cryptographic Gate"

    def mount(self) -> ui.dialog:
        refs.hitl_dialog = super().mount()
        return refs.hitl_dialog

    def compose(self, dialog: ui.dialog) -> None:
        kit.tag(
            bind=(
                state.security,
                "admin_user",
                lambda u: f"High-risk MCP tool call parked. Requires RFC-6238 TOTP + Ed25519 attestation ({u}):",
            ),
            tone="fg",
            framed=False,
        ).classes("whitespace-normal")
        refs.hitl_code = ui.code("{}", language="json").classes("w-full text-xs")
        kit.text(
            "6-Digit Admin TOTP Code (Leave blank for Auto-Derived Self-Verify)",
            state.security,
            "totp_input_code",
            flex=False,
            width="w-full mt-1",
        )
        with ui.row().classes("w-full justify-end gap-2 mt-3"):
            kit.button(
                "Reject & Rollback",
                lambda: (
                    dialog.close(),
                    refs.push_pubsub("mcp:hitl", "[REJECT] Transaction rolled back"),
                    ui.notify("Rejected & Rolled back", type="warning"),
                ),
                color="red",
                outline=True,
            )
            kit.button("Verify TOTP & Approve", oob.approve_hitl, color="green")


class QrModal(ModalTemplate):
    width_cls = "w-[440px] items-center"
    border_cls = "tui-border-cyan"
    title = "Admin TOTP QR · Google Authenticator / Authy"

    def mount(self) -> ui.dialog:
        refs.qr_dialog = super().mount()
        return refs.qr_dialog

    def compose(self, dialog: ui.dialog) -> None:
        kit.tag(bind=(state.security, "admin_user", lambda u: f"SPIFFE ID: {u}"), tone="fg", framed=False)
        refs.qr_code_box = ui.code("", language="text").classes("text-[10px] leading-none font-mono p-3")
        kit.tag(
            bind=(state.security, "totp_secret", lambda s: f"Base32 Secret: {s}"),
            tone="amber",
            framed=False,
        )
        kit.button("Close", dialog.close, color="cyan", outline=True).classes("w-full mt-2")


# Tab 1: MCP Gateway
class McpGatewayTab(StudioTab):
    title = "MCP Gateway"
    left_header = "MCP Server Switchboard"
    right_header = "MCP Config"
    ratio = 52

    def __init__(self) -> None:
        self._configs_box: ui.row | None = None
        self._workers_box: ui.column | None = None
        self._roster_summary: kit.Tag | None = None
        self._editor: ui.codemirror | None = None
        self._editor_status: kit.Tag | None = None

    def _trigger_topology_scan(self, silent: bool = True) -> None:
        try:
            asyncio.create_task(mcp.scan_mcp_topology_and_health(silent=silent))
        except RuntimeError:
            pass

    def _copy_worker_to_anchor(
        self,
        agent: str | None = None,
        target: str | None = None,
        exec_cmd: str | None = None,
        mode: str | None = None,
        preflight: str | None = None,
    ) -> None:
        """Upsert an explicit source ('builtin', 'all', 'claude', etc.) or single target into ~/.anchor/mcp.json."""
        try:
            effective_agent = agent or ("all" if (target is None and exec_cmd is None) else None)
            run_connect_copy(
                agent=effective_agent,
                exec_cmd=exec_cmd,
                target=target,
                mode=mode,
                preflight=preflight or None,
            )
            self._reload_editor_from_disk(notify=False)
            self._render_dynamic_sections()
            self._trigger_topology_scan(silent=True)
            label = target or (f"'{effective_agent}' targets" if effective_agent else "MCP target")
            ui.notify(f"Synced {label} into mcp.json", type="positive")
        except BaseException as exc:
            ui.notify(f"Failed to sync MCP config: {exc}", type="negative")

    def compose_left(self) -> None:
        s = state.security
        with kit.bar():
            kit.tag(bind=(s, "gateway_health_badge_text"), tone="green", framed=False).classes("font-bold")
            with ui.row().classes("items-center gap-1.5"):
                self._roster_summary = kit.tag("", "muted", framed=False)
                kit.button(
                    "[↻]",
                    lambda: (
                        self._trigger_topology_scan(silent=False),
                        self._reload_editor_from_disk(notify=False),
                    ),
                    color="cyan",
                    flat=True,
                    size="xs",
                ).tooltip("Rescan MCP configs & daemon status")

        self._workers_box = ui.column().classes("w-full gap-1.5 flex-grow max-h-80 overflow-y-auto pr-1")
        self._render_dynamic_sections()

    def compose_right(self) -> None:
        with kit.row(align="items-center justify-between"):
            kit.section("Discover MCP Sources")
            with ui.row().classes("items-center gap-1"):
                self._configs_box = ui.row().classes("items-center gap-1 no-wrap")
                kit.button(
                    "Copy Setting",
                    lambda: self._copy_worker_to_anchor(agent="all"),
                    color="cyan",
                    outline=True,
                ).tooltip("Sync built-in workers & discovered IDE configs into ~/.anchor/mcp.json (fiber connect all)")

        with kit.row(align="items-center justify-between"):
            kit.tag(f"Editor — {ANCHOR_MCP_PATH}", "muted", framed=False).classes("truncate")
            self._editor_status = kit.tag("Ready", "green", framed=False)

        self._editor = ui.codemirror(
            value=_read_anchor_mcp_text(),
            language="JSON",
            theme="oneDark",
        ).classes("w-full flex-grow min-h-[210px] text-xs font-mono border border-[#262b3d]")

        with kit.footer():
            kit.button(
                "Reload",
                lambda: self._reload_editor_from_disk(notify=True),
                color="muted",
                outline=True,
            ).tooltip("Reload ~/.anchor/mcp.json from disk")
            kit.button(
                "Format JSON",
                self._format_editor_json,
                color="cyan",
                outline=True,
            ).tooltip("Validate & pretty-print JSON")
            kit.button(
                "Save & Rescan",
                self._save_editor_to_disk,
                color="green",
                flex=True,
            ).tooltip("Save to ~/.anchor/mcp.json and rescan MCP worker topology")

        self._render_dynamic_sections()

    def _set_editor_status(self, message: str, tone: str) -> None:
        if self._editor_status is not None:
            self._editor_status.set(message, tone)

    def _reload_editor_from_disk(self, notify: bool = False) -> None:
        if self._editor is None:
            return
        self._editor.value = _read_anchor_mcp_text()
        self._set_editor_status("Loaded from disk", "cyan")
        if notify:
            ui.notify("Reloaded ~/.anchor/mcp.json", type="info")

    def _format_editor_json(self) -> bool:
        if self._editor is None:
            return False
        raw = (self._editor.value or "").strip() or DEFAULT_ANCHOR_MCP_JSON
        try:
            parsed = orjson.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("Root JSON must be an object")
            if "mcpServers" not in parsed or not isinstance(parsed.get("mcpServers"), dict):
                parsed.setdefault("mcpServers", {})
            formatted = orjson.dumps(parsed, option=orjson.OPT_INDENT_2).decode("utf-8") + "\n"
            self._editor.value = formatted
            self._set_editor_status(f"Valid JSON ({len(parsed.get('mcpServers', {}))} servers)", "green")
            return True
        except Exception as exc:
            self._set_editor_status(f"Invalid JSON: {exc}", "red")
            ui.notify(f"JSON Syntax Error: {exc}", type="negative")
            return False

    def _save_editor_to_disk(self) -> None:
        if self._editor is None:
            return
        if not self._format_editor_json():
            return
        try:
            ANCHOR_MCP_PATH.parent.mkdir(parents=True, exist_ok=True)
            ANCHOR_MCP_PATH.write_text(self._editor.value, encoding="utf-8")
            self._set_editor_status("Saved", "green")
            ui.notify(f"Saved {ANCHOR_MCP_PATH.name} & rescanning topology...", type="positive")
            self._render_dynamic_sections()
            self._trigger_topology_scan(silent=True)
        except Exception as exc:
            ui.notify(f"Failed to save mcp.json: {exc}", type="negative")

    def _render_dynamic_sections(self) -> None:
        s = state.security
        anchor_specs = _load_anchor_specs_map()
        active_set = set(s.active_target_ids or [])
        public_builtins = list_builtin_entries(include_internal=False)
        synced_builtin_cnt = sum(1 for b in public_builtins if b.canonical_id in anchor_specs)

        default_mcp_tpl = (
            '{\n  "mcpServers": {\n'
            '    "fiber-gateway": {\n      "url": "http://127.0.0.1:8356/v1/mcp-gateway/invoke"\n    }\n'
            "  }\n}\n"
        )

        # 1. Source tags (canonical fiber + builtin catalog + external IDEs)
        if self._configs_box is not None:
            self._configs_box.clear()
            resolved_anchor = ANCHOR_MCP_PATH.resolve()
            found_cfgs = [
                c for c in s.discovered_configs
                if c.get("exists") and Path(c.get("path", "")).resolve() != resolved_anchor
            ]
            missing_cfgs = [
                c for c in s.discovered_configs
                if not c.get("exists") and Path(c.get("path", "")).resolve() != resolved_anchor
            ]

            with self._configs_box:
                anchor_exists = ANCHOR_MCP_PATH.is_file()
                with ui.row().classes("items-center gap-0.5 no-wrap"):
                    kit.tag(
                        "[● fiber]" if anchor_exists else "[○ fiber]",
                        "cyan" if anchor_exists else "amber",
                        tooltip=f"Fiber MCP Config ({len(anchor_specs)} targets) — {ANCHOR_MCP_PATH}",
                    )
                    kit.file_link(
                        str(ANCHOR_MCP_PATH),
                        label="mcp.json",
                        tooltip=f"Open {ANCHOR_MCP_PATH} in external editor",
                        default_content=DEFAULT_ANCHOR_MCP_JSON,
                        create_if_missing=True,
                    )

                if public_builtins:
                    all_synced = synced_builtin_cnt == len(public_builtins)
                    builtin_ids = ", ".join(b.canonical_id for b in public_builtins)
                    kit.tag(
                        f"[+ builtin {synced_builtin_cnt}/{len(public_builtins)}]",
                        "cyan" if all_synced else "muted",
                        on_click=lambda: self._copy_worker_to_anchor(agent="builtin"),
                        tooltip=f"Click to sync public built-in workers into mcp.json ({builtin_ids})",
                    )

                for cfg in found_cfgs:
                    ag = cfg["agent"]
                    p_str = cfg["path"]
                    with ui.row().classes("items-center gap-0.5 no-wrap"):
                        kit.tag(
                            f"[● {ag}]",
                            "green",
                            on_click=lambda a=ag: self._copy_worker_to_anchor(agent=a),
                            tooltip=f"Click to sync '{ag}' ({cfg['name']}) into mcp.json — {p_str}",
                        )
                        kit.file_link(
                            p_str,
                            label="",
                            tooltip=f"Edit {p_str}",
                            default_content=default_mcp_tpl,
                            create_if_missing=False,
                        )

                if missing_cfgs:
                    with kit.button(f"[+ INIT {len(missing_cfgs)}]", lambda: None, color="muted", flat=True, size="xs"):
                        with ui.menu():
                            for cfg in missing_cfgs:
                                p_str = cfg["path"]
                                with ui.menu_item().classes("py-1 px-2"):
                                    with ui.row().classes("items-center justify-between w-44 no-wrap"):
                                        kit.tag(f"[  {cfg['agent']}]", "muted", framed=False)
                                        kit.file_link(
                                            p_str,
                                            label="Create",
                                            tooltip=f"Create {p_str}",
                                            default_content=default_mcp_tpl,
                                            create_if_missing=True,
                                        )

        # 2. Left switchboard (targets registered in ~/.anchor/mcp.json)
        if self._workers_box is not None:
            self._workers_box.clear()
            counts = {"running": 0, "standby": 0, "config": 0}

            with self._workers_box:
                if not s.discovered_workers:
                    kit.tag(
                        "No MCP workers in mcp.json yet. Click [+ builtin], [ Copy Setting ] or an IDE tag to sync.",
                        "muted",
                        framed=False,
                    ).classes("whitespace-normal py-1")

                for w in s.discovered_workers:
                    tid = w["target_id"]
                    source = str(w.get("source", "fiber"))
                    transport = str(w.get("transport", "stdio")).lower()
                    exec_cmd = w.get("exec_cmd", "")

                    anchor_env = (
                        anchor_specs.get(tid, {}).get("env", {})
                        if isinstance(anchor_specs.get(tid), dict)
                        else {}
                    )
                    raw_mode = str(w.get("mode", "")).lower()
                    if anchor_env.get("FIBER_MODE"):
                        mode = str(anchor_env["FIBER_MODE"]).lower()
                    elif raw_mode in ("multiplex", "linear", "ephemeral"):
                        mode = raw_mode
                    else:
                        mode = "ephemeral"

                    preflight = w.get("preflight") or anchor_env.get("FIBER_PREFLIGHT_PATTERN") or ""
                    is_mounted = bool(w.get("is_live")) or (tid in active_set)

                    if is_mounted and mode != "ephemeral":
                        status_key = "running"
                    elif is_mounted:
                        status_key = "standby"
                    else:
                        status_key = "config"
                    counts[status_key] += 1
                    status_text, status_tone, status_tip = _STATUS[status_key]

                    with kit.bar():
                        with ui.row().classes("items-center gap-1.5 no-wrap overflow-hidden"):
                            kit.tag(
                                status_text, status_tone, framed=False, tooltip=status_tip.format(mode=mode.upper())
                            ).classes("font-bold")
                            kit.tag(tid, "fg", framed=False, tooltip=exec_cmd).classes("font-bold truncate")
                            kit.tag(f"[{source}|{transport}:{mode}]", "cyan", framed=False)

                        with ui.row().classes("items-center gap-1.5 shrink-0"):
                            kit.tag(
                                f"[LOCK {preflight[:12]}]" if preflight else "[OPEN]",
                                "amber" if preflight else "muted",
                                tooltip=f"Pre-Flight Pattern: {preflight or 'None'}",
                            )

                            if status_key == "running":
                                kit.tag("[ACTIVE]", "green")
                            elif status_key == "standby":
                                kit.tag("[ZERO-SPAWN]", "cyan")
                            else:
                                kit.button(
                                    "[▶ RUN]",
                                    lambda t=tid, c=exec_cmd: run_fiber_cli("connect", "--standalone", "-t", t, "-e", c),
                                    color="amber",
                                    outline=True,
                                    size="xs",
                                ).tooltip("Daemon is offline; run this worker in standalone mode")

            if self._roster_summary is not None:
                self._roster_summary.set(
                    f"● {counts['running']} RUN · ~ {counts['standby']} STBY · ○ {counts['config']} CFG"
                )

    def flush(self) -> None:
        if state.security.mcp_topology_dirty:
            state.security.mcp_topology_dirty = False
            self._render_dynamic_sections()
            self._reload_editor_from_disk(notify=False)