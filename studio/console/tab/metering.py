# fiber.studio.console.tab.metering
from __future__ import annotations

from pathlib import Path
from nicegui import ui

from fiber.node.account.finops.console import finops_manager
import fiber.studio.console.ui.kit as kit
from fiber.studio.console.ui.kit import StudioTab
from fiber.node.account.state import state, refs, USD_TO_FUEL_RATE
from xphi.node.space.bind.resolver import resolve_path

FINOPS_ACTION_LABELS = {
    "SYNC_QUOTA": "Set to Budget",
    "TOP_UP": "+Balance",
    "REVOKE": "0 Balance",
}

def _resolve_accounts_registry_path() -> Path:
    try:
        from xphi.arch.bound.account.registry import LocalAccountRegistry
        return Path(LocalAccountRegistry.get_registry_path())
    except Exception:
        return resolve_path("anchor") / "sign" / "local_accounts.json"


def _sync_finops_echart() -> None:
    """Re-render the FinOps EChart when f.chart_dirty is set."""
    f = state.finops
    chart = refs.finops_chart
    if chart is None or not getattr(f, "chart_dirty", False):
        return
    try:
        opts = chart.options
        if isinstance(opts.get("xAxis"), dict):
            opts["xAxis"]["data"] = list(f.x_ticks)
        series = opts.get("series")
        if isinstance(series, list):
            if len(series) > 0 and isinstance(series[0], dict):
                series[0]["data"] = list(f.y_fuel)
            if len(series) > 1 and isinstance(series[1], dict):
                series[1]["data"] = list(f.y_core)
        chart.update()
        f.chart_dirty = False
    except Exception:
        pass


class MeteringTab(StudioTab):
    title = "Metering"
    left_header = "Quota & Token Governance"
    right_header = "LLM Telemetry & Circuit Breaker"

    @staticmethod
    def _ensure_telemetry_attrs(s: object) -> None:
        defaults = {
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

    def compose_left(self) -> None:
        f = state.finops
        s = state.security
        self._ensure_telemetry_attrs(s)

        if f.jwt_op not in FINOPS_ACTION_LABELS:
            f.jwt_op = "SYNC_QUOTA"

        # 1. Tenant identity + local account registry link
        with kit.bar():
            kit.tag("Target Account Identity", "fg", framed=False).classes("font-bold shrink-0")
            kit.file_link(
                _resolve_accounts_registry_path,
                label="local_accounts.json",
                tooltip="Open LocalAccountRegistry JSON file",
                create_if_missing=True,
            )

        with kit.row():
            kit.text("Target Tenant ID", f, "tenant_id")

        # 2. Budget allocation & EdDSA mandate controls
        with kit.row(align="items-center justify-between"):
            kit.section("Fuel Balance Control ($1 = 1,000,000 Fuel)", "green")
            kit.tag(
                bind=(
                    f,
                    "jwt_usd",
                    lambda u: (
                        "-> Target Balance: 0 Fuel (REVOKE)"
                        if f.jwt_op == "REVOKE"
                        else (
                            f"-> Add +{int((u or 0) * USD_TO_FUEL_RATE):,} Fuel (New: {f.ledger_balance + int((u or 0) * USD_TO_FUEL_RATE):,})"
                            if f.jwt_op == "TOP_UP"
                            else f"-> Match Balance to {int((u or 0) * USD_TO_FUEL_RATE):,} Fuel"
                        )
                    ),
                ),
                tone="amber",
                framed=False,
            ).classes("font-bold")

        with kit.row():
            kit.select(FINOPS_ACTION_LABELS, "Balance Action", f, "jwt_op", width="w-64")
            kit.number("Budget ($)", f, "jwt_usd", min_val=1.0, max_val=500.0, step=5.0, width="w-24")
            kit.button(
                "Apply",
                finops_manager.issue_account_jwt,
                color="green",
                flex=True,
            ).tooltip("Update AnchorStore Fuel Balance via signed EdDSA mandate")

        with kit.row(gap="gap-1.5"):
            kit.button(
                "Stream Break (10s)",
                finops_manager.temporary_stream_kill,
                color="amber",
                flex=True,
            ).tooltip("Temporarily break active streams for 10s and auto-restore balance")
            kit.button(
                "Stream Kill (Set Zero)",
                finops_manager.revoke_account_kill,
                color="red",
                outline=True,
                flex=True,
            ).tooltip("Immediately burn all remaining fuel to 0 and sever active streams")

        ui.label().bind_text_from(
            f, "last_jwt_preview", lambda j: f"Signed Mandate Token (EdDSA): {j}"
        ).classes(kit.CLS_MONO_LABEL)

        # Remaining fuel gauge + cumulative LLM metering KPIs
        with kit.box(flex=False, width="w-full gap-1.5 mt-1 shrink-0"):
            kit.meter(
                "Remaining Balance vs Quota Baseline",
                f,
                "ledger_balance",
                text=lambda bal: (
                    f"{int(bal):,} / {int(f.fuel_budget):,} Fuel "
                    f"({round((bal / f.fuel_budget) * 100, 1) if f.fuel_budget > 0 else 0.0}%)"
                ),
                value=lambda bal: min(1.0, max(0.0, round(float(bal) / max(float(f.fuel_budget), 1.0), 3))),
                tone="green",
                size="8px",
            )

            # Core metering KPIs (tokens, saved tokens/USD)
            with ui.row().classes("w-full items-center justify-between pt-1 border-t border-[#262b3d]"):
                kit.tag(
                    bind=(
                        s,
                        "total_processed_tokens",
                        lambda tot: f"TOKENS {int(getattr(s, 'live_stream_tokens', 0)):,} live / {int(tot or 0):,} tok",
                    ),
                    tone="amber",
                    framed=False,
                ).classes("font-bold")
                kit.tag(
                    bind=(
                        f,
                        "saved_tokens",
                        lambda st: f"SAVED {int(st or 0):,} tok (${float(getattr(f, 'saved_usd', 0.0)):.2f})",
                    ),
                    tone="green",
                    framed=False,
                ).classes("font-bold")

    def compose_right(self) -> None:
        f = state.finops
        s = state.security
        self._ensure_telemetry_attrs(s)

        # Telemetry bar: active streams, plane event density, circuit breaker status
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
                        "plane_density",
                        lambda d: (
                            f"[! BURST {float(d or 0.0):.1f} ev/s]"
                            if getattr(s, "plane_bursting", False)
                            else f"[PLANE {float(d or 0.0):.1f} ev/s]"
                        ),
                    ),
                    tone="cyan",
                )

            kit.tag(bind=(f, "stream_status_text"), tone="amber").classes("font-bold")

        # Live LLM stream velocity (chunks/s)
        with kit.box(flex=False, width="w-full shrink-0 gap-0.5"):
            kit.meter(
                "LLM STREAM VELOCITY",
                s,
                "throughput_cps",
                text=lambda v: f"{float(v or 0.0):,.1f} chunks/s",
                value=lambda v: min(1.0, round(float(v or 0.0) / 2200.0, 3)),
                tone="cyan",
            )

        # Fuel balance vs quota baseline time series + periodic re-render
        refs.finops_chart = kit.line_chart(
            list(f.x_ticks),
            list(f.y_fuel),
            list(f.y_core),
            redline=f.fuel_budget,
        )
        ui.timer(0.5, _sync_finops_echart)

        # Live stream / circuit breaker event log
        refs.live_stream_log = kit.log(max_lines=60, color="tui-text-green")