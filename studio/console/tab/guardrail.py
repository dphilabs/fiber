# fiber.studio.console.tab.guardrail
from __future__ import annotations

import re
from nicegui import ui

from fiber.node.account.state import state, refs
from fiber.studio.console.bridge.pipeline import pipeline
from fiber.llm.compat.secret.registry import (
    BASE_SECRET_KEYS,
    DEFAULT_REDACT_FILENAME,
    RedactRegistryIO,
)
import fiber.llm.compat.secret.redact as _redact_mod

import fiber.studio.console.ui.kit as kit
from fiber.studio.console.ui.kit import ModalTemplate, StudioTab
from fiber.studio.console.ui.modal.guide import GuideModal
from fiber.studio.console.tab.mcp import HitlModal, QrModal, McpGatewayTab
from fiber.studio.console.tab.vcr import VcrTab
from fiber.studio.console.tab.metering import MeteringTab

class SecretRulesModal:
    """Single dialog for built-in + custom regex rules (redact_rules.json) and secret keys."""

    def __init__(self, on_saved=None) -> None:
        self.on_saved = on_saved
        self.dialog = ui.dialog()
        self.disabled_set: set[str] = set()
        self.custom_patterns: dict[str, str] = {}
        self.extra_keys: list[str] = []

    def open(self, initial_tab: str = "rules") -> None:
        schema = RedactRegistryIO.load_schema()
        self.disabled_set = set(schema.disabled_rules)
        self.custom_patterns = dict(schema.patterns)
        self.extra_keys = [k.upper() for k in schema.extra_secret_keys]
        base_keys = sorted(BASE_SECRET_KEYS)

        def _merged_rules() -> dict[str, str]:
            return {**self.custom_patterns, **_redact_mod.BASE_SECRET_RULES}

        self.dialog.clear()
        # Transparent shell + inner legend box (q-dialog would clip a legend on the card itself).
        with self.dialog, ui.card().classes("tui-modal-shell w-[740px] max-w-[94vw] p-0 pt-2.5 gap-0"):
            with kit.box(title="Secret Registry", flex=False, width="w-full gap-2"):
                with ui.row().classes("w-full items-center justify-between border-b border-[#262b3d] pb-1"):
                    with ui.tabs().props("dense no-caps align=left") as tabs:
                        tab_rules = ui.tab("rules", label="[1] Regex Rules (Built-in & Custom)")
                        tab_keys = ui.tab("keys", label="[2] Secret Keys (Dict & URL)")
                    self.status_label = kit.tag("", "muted", framed=False)

                def _refresh_status() -> None:
                    all_r = _merged_rules()
                    active_r = len(all_r) - len(self.disabled_set & set(all_r.keys()))
                    total_k = len(set(base_keys) | set(self.extra_keys))
                    self.status_label.set_text(
                        f"Rules: {active_r}/{len(all_r)} active (Custom: {len(self.custom_patterns)})  |  Keys: {total_k}"
                    )

                _refresh_status()

                with ui.tab_panels(tabs, value=tab_keys if initial_tab == "keys" else tab_rules).classes(
                    "w-full bg-transparent p-0"
                ):
                    # 1. Regex rules: built-in toggles + custom pattern add/remove
                    with ui.tab_panel(tab_rules).classes("p-0 gap-2 flex flex-col"):
                        # Custom pattern input (maps to 'patterns' in redact_rules.json)
                        with kit.bar():
                            new_id_input = (
                                ui.input(placeholder="New rule_id (e.g. corp_token)")
                                .props("dense outlined dark square")
                                .classes("w-48 text-xs font-mono")
                            )
                            new_regex_input = (
                                ui.input(placeholder=r"Regex pattern (e.g. CORP-[0-9]{6})")
                                .props("dense outlined dark square")
                                .classes("flex-grow text-xs font-mono")
                            )

                            def _add_custom_pattern() -> None:
                                rid = (new_id_input.value or "").strip()
                                pat = (new_regex_input.value or "").strip()
                                if not rid or not pat:
                                    ui.notify("Both rule_id and regex pattern are required.", type="warning")
                                    return
                                try:
                                    re.compile(pat)
                                except re.error as err:
                                    ui.notify(f"Invalid regex: {err}", type="negative")
                                    return
                                self.custom_patterns[rid] = pat
                                self.disabled_set.discard(rid)
                                new_id_input.value = ""
                                new_regex_input.value = ""
                                _refresh_status()
                                _render_rows(search_input.value)

                            new_regex_input.on("keydown.enter", _add_custom_pattern)
                            kit.button("[+ ADD RULE]", _add_custom_pattern, color="cyan").classes("shrink-0")

                        with ui.row().classes("w-full items-center justify-between gap-2"):
                            search_input = (
                                ui.input(placeholder="Filter by rule_id or regex...")
                                .props("dense outlined dark square clearable")
                                .classes("flex-grow text-xs font-mono")
                            )
                            kit.button("[ENABLE ALL]", lambda: _set_all(True), color="green", flat=True, size="xs")
                            kit.button("[DISABLE ALL]", lambda: _set_all(False), color="amber", flat=True, size="xs")

                        with kit.box(scroll=True, height="h-72", black=True, flex=False, width="w-full") as list_container:
                            pass

                        def _render_rows(query: str = "") -> None:
                            q = (query or "").strip().lower()
                            all_r = _merged_rules()
                            list_container.clear()
                            with list_container:
                                for rule_id, pattern in all_r.items():
                                    if q and q not in rule_id.lower() and q not in pattern.lower():
                                        continue
                                    is_enabled = rule_id not in self.disabled_set
                                    is_custom = rule_id in self.custom_patterns

                                    with ui.row().classes("tui-row w-full items-center justify-start px-2 py-1 no-wrap gap-2.5"):
                                        def _toggle(e, rid=rule_id) -> None:
                                            if e.value:
                                                self.disabled_set.discard(rid)
                                            else:
                                                self.disabled_set.add(rid)
                                            _refresh_status()

                                        ui.switch(value=is_enabled, on_change=_toggle).props("dense color=info").classes(
                                            "shrink-0"
                                        )

                                        with ui.column().classes("gap-0 overflow-hidden flex-grow"):
                                            with ui.row().classes("items-center gap-1.5 no-wrap"):
                                                kit.tag(rule_id, "fg", framed=False).classes("font-bold shrink-0")
                                                if is_custom:
                                                    kit.tag("[CUSTOM]", "cyan").classes("text-[9px]")
                                            kit.tag(pattern, "muted", framed=False, tooltip=pattern).classes(
                                                "text-[10px] truncate w-full"
                                            )

                                        if is_custom:
                                            kit.button(
                                                "[x]",
                                                lambda rid=rule_id: (
                                                    self.custom_patterns.pop(rid, None),
                                                    self.disabled_set.discard(rid),
                                                    _refresh_status(),
                                                    _render_rows(search_input.value),
                                                ),
                                                color="red",
                                                flat=True,
                                                size="xs",
                                            ).tooltip("Remove custom rule")

                        def _set_all(enable: bool) -> None:
                            if enable:
                                self.disabled_set.clear()
                            else:
                                self.disabled_set = set(_merged_rules().keys())
                            _refresh_status()
                            _render_rows(search_input.value)

                        search_input.on_value_change(lambda e: _render_rows(e.value))
                        _render_rows()

                    # 2. Secret keys
                    with ui.tab_panel(tab_keys).classes("p-0 gap-2 flex flex-col"):
                        kit.tag(
                            "Matches dictionary keys & URL query parameters containing these tokens "
                            "(case-insensitive) in PRE_TRANSLATE.",
                            "muted",
                            framed=False,
                        ).classes("whitespace-normal")

                        with ui.row().classes("w-full items-center gap-2"):
                            new_key_input = (
                                ui.input(placeholder="Add extra secret key (min 3 chars, e.g. CORP_PIN)...")
                                .props("dense outlined dark square")
                                .classes("flex-grow text-xs font-mono")
                            )

                            def _add_extra_key() -> None:
                                val = (new_key_input.value or "").strip().upper()
                                if len(val) < 3:
                                    ui.notify("Key must be at least 3 characters.", type="warning")
                                    return
                                if val not in base_keys and val not in self.extra_keys:
                                    self.extra_keys.append(val)
                                new_key_input.value = ""
                                _refresh_status()
                                _render_keys()

                            new_key_input.on("keydown.enter", _add_extra_key)
                            kit.button("[+ ADD KEY]", _add_extra_key, color="green")

                        with kit.box(scroll=True, height="h-72", black=True, flex=False, width="w-full") as keys_container:
                            pass

                        def _render_keys() -> None:
                            keys_container.clear()
                            with keys_container:
                                kit.section("Extra Secret Keys (saved to redact_rules.json)", "green").classes("mb-1")
                                with ui.row().classes("w-full flex-wrap gap-1.5 mb-3"):
                                    if not self.extra_keys:
                                        kit.tag("No extra keys configured.", "muted", framed=False).classes("italic")
                                    for k in sorted(self.extra_keys):
                                        with ui.row().classes("items-center gap-0 no-wrap"):
                                            kit.tag(k, "green")
                                            kit.button(
                                                "[x]",
                                                lambda target=k: (
                                                    self.extra_keys.remove(target),
                                                    _refresh_status(),
                                                    _render_keys(),
                                                ),
                                                color="green",
                                                flat=True,
                                                size="xs",
                                            )

                                kit.section("Built-in Secret Keys (14 Core Patterns)", "muted").classes("mb-1")
                                with ui.row().classes("w-full flex-wrap gap-1.5"):
                                    for k in base_keys:
                                        kit.tag(k, "muted")

                        _render_keys()

                with ui.row().classes("w-full justify-end gap-2 pt-1"):
                    kit.button("Cancel", self.dialog.close, color="muted", flat=True)

                    def _save() -> None:
                        RedactRegistryIO.save_preferences(
                            disabled_rules=sorted(self.disabled_set),
                            extra_secret_keys=self.extra_keys,
                            patterns=self.custom_patterns,
                        )
                        if self.on_saved:
                            self.on_saved()
                        ui.notify(
                            f"Saved to {DEFAULT_REDACT_FILENAME} "
                            f"(Custom: {len(self.custom_patterns)}, Disabled: {len(self.disabled_set)}, +Keys: {len(self.extra_keys)})",
                            type="positive",
                        )
                        self.dialog.close()

                    kit.button("Save & Apply", _save, color="cyan")

        self.dialog.open()


class LLMGuardrailTab(StudioTab):
    title = "Guardrail"
    left_header = "Rule Registry & Test"
    right_header = "Pipeline X-Ray"

    @staticmethod
    def _ensure_telemetry_attrs(s: object) -> None:
        defaults = {
            "guardrail_mask": True,
            "guardrail_block": True,
            "active_streams": 0,
            "throughput_cps": 0.0,
            "live_stream_tokens": 0,
            "total_processed_tokens": 0,
            "plane_density": 0.0,
            "plane_acceleration": 0.0,
            "plane_bursting": False,
        }
        for k, v in defaults.items():
            if not hasattr(s, k):
                setattr(s, k, v)

    @staticmethod
    def _sync_legacy_guardrail_attrs(s: object) -> None:
        """Keeps legacy `block_api_keys` and `guardrail_mode` attributes aligned for bridge compatibility."""
        mask_on = bool(getattr(s, "guardrail_mask", False))
        block_on = bool(getattr(s, "guardrail_block", False))
        setattr(s, "block_api_keys", mask_on or block_on)
        if block_on:
            setattr(s, "guardrail_mode", "Strict Block (PermissionError)")
        elif mask_on:
            setattr(s, "guardrail_mode", "Redact (Mask with ***)")

    def compose_left(self) -> None:
        s = state.security
        self._ensure_telemetry_attrs(s)
        self._sync_legacy_guardrail_attrs(s)
        # Clear one-off custom_regex so it does not interfere with built-in/registry rule toggling tests
        s.custom_regex = ""

        active_rules_cnt = len(getattr(_redact_mod, "ACTIVE_SECRET_RULES", {}) or {})
        active_keys_cnt = len(getattr(_redact_mod, "SECRET_KEY_PATTERNS", {}) or {})
        registry_file_path = RedactRegistryIO.ensure_default_file()

        rules_badge_ref = None
        keys_badge_ref = None

        def _refresh_rule_badges() -> None:
            r_cnt = len(getattr(_redact_mod, "ACTIVE_SECRET_RULES", {}) or {})
            k_cnt = len(getattr(_redact_mod, "SECRET_KEY_PATTERNS", {}) or {})
            if rules_badge_ref is not None:
                rules_badge_ref.set(f"[RULES {r_cnt}]")
            if keys_badge_ref is not None:
                keys_badge_ref.set(f"[KEYS {k_cnt}]")

        def _reload_from_disk() -> None:
            RedactRegistryIO.initialize_and_inject()
            _refresh_rule_badges()
            ui.notify(f"Reloaded {DEFAULT_REDACT_FILENAME} into runtime engine.", type="info")

        rules_modal = SecretRulesModal(on_saved=_refresh_rule_badges)

        with kit.bar():
            with ui.row().classes("items-center gap-1 no-wrap"):
                rules_badge_ref = kit.tag(
                    f"[RULES {active_rules_cnt}]",
                    "cyan",
                    on_click=lambda: rules_modal.open("rules"),
                    tooltip="Click to manage Built-in & Custom Regex Rules",
                ).classes("font-bold")
                keys_badge_ref = kit.tag(
                    f"[KEYS {active_keys_cnt}]",
                    "green",
                    on_click=lambda: rules_modal.open("keys"),
                    tooltip="Click to inspect & add Secret Key Patterns",
                ).classes("font-bold")

            with ui.row().classes("items-center gap-1 no-wrap"):
                kit.file_link(
                    registry_file_path,
                    label=DEFAULT_REDACT_FILENAME,
                    tooltip="Open declarative secret & PII registry JSON",
                    create_if_missing=True,
                )
                kit.button("[↻]", _reload_from_disk, color="muted", flat=True, size="xs").tooltip(
                    "Reload redact_rules.json from disk"
                )

        kit.section("Runtime Slot Enforcement (state.json)", "muted")
        kit.switch(
            "PRE_TRANSLATE : In-Place Masking (Mutate with ***)",
            s,
            "guardrail_mask",
            color="cyan",
        )
        kit.switch(
            "POST_TRANSLATE : Strict Secret Block (PermissionError)",
            s,
            "guardrail_block",
            color="red",
        )

        async def _run_xray() -> None:
            self._sync_legacy_guardrail_attrs(s)
            await pipeline.run_pipeline_xray(s.test_prompt)

        kit.section("Test Prompt").classes("mt-1")
        kit.text("Input Prompt", s, "test_prompt", flex=False, width="w-full")
        with kit.row(gap="gap-1.5"):
            kit.button("Test Prompt", _run_xray, color="red", flex=True)

    def compose_right(self) -> None:
        s = state.security
        self._ensure_telemetry_attrs(s)
        with kit.bar():
            with ui.row().classes("items-center gap-2 no-wrap"):
                kit.tag(
                    bind=(
                        s,
                        "active_streams",
                        lambda n: f"[● STREAMS {int(n or 0)}]" if (n or 0) > 0 else "[  IDLE]",
                    ),
                    tone="green",
                ).classes("font-bold")
                kit.tag(
                    bind=(
                        s,
                        "total_processed_tokens",
                        lambda tot: f"TOKENS {int(getattr(s, 'live_stream_tokens', 0)):,} live / {int(tot or 0):,} total",
                    ),
                    tone="amber",
                    framed=False,
                ).classes("font-bold")

            kit.tag(
                bind=(
                    s,
                    "plane_density",
                    lambda d: (
                        f"[! BURST {float(d or 0.0):.1f} ev/s]"
                        if getattr(s, "plane_bursting", False)
                        else f"[PLANE {float(d or 0.0):.1f} ev/s]"
                    ),
                ),
                tone="cyan",
            )

        with kit.box(flex=False, width="w-full shrink-0 gap-0.5"):
            kit.meter(
                "VELOCITY",
                s,
                "throughput_cps",
                text=lambda v: f"{float(v or 0.0):,.1f} chunks/s",
                value=lambda v: min(1.0, round(float(v or 0.0) / 2200.0, 3)),
                tone="cyan",
            )

        with kit.bar():
            for key, title, slot in [
                ("cache", "EdgeCache", "PRE_TRANSLATE"),
                ("router", "ONNX Router", "PRE_TRANSLATE"),
                ("guardrail", "Secret / PII Guard", "PRE & POST"),
                ("fuel", "StreamBreaker", "STREAM_IO"),
            ]:
                # Element type and refs.slot_cards are kept: the pipeline bridge may restyle these cards.
                with ui.card().classes("bg-[#0d0f17] border border-[#262b3d] p-2 w-36 text-center") as c:
                    refs.slot_cards[key] = c
                    kit.tag(title, "fg", framed=False).classes("text-xs font-bold")
                    kit.tag(slot, "muted", framed=False).classes("text-[10px]")

        kit.section("Slot Overhead (ms)", "muted").classes("mt-0.5")
        refs.xray_chart = kit.bar_chart(
            categories=["StreamBreaker", "SecretGuard", "ONNX Router", "EdgeCache"],
            data=list(s.xray_latencies),
            x_max=1.0,
        )