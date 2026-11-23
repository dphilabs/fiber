# fiber.node.runbook.trigger.base
"""
[Unified Tri-Modal Trigger Core & Runtime: Manual | Cron | Event]
Consolidates `TriggerSpec`, `TriggerEnvelope`, `TriggerGate`, and `TriggerRuntime`
on top of `reflexor.workflow.trigger.cron` with zero circular imports to `viewport`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import time
from typing import TYPE_CHECKING, Any, Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fiber.node.runbook.trigger.cron import compile_cron
from xphi.arch.contract.reflex.fsm import extract_json_path

if TYPE_CHECKING:
    from fiber.node.runbook.viewport.projector import ViewportSnapshot

TriggerKind = Literal["manual", "cron", "event"]
MissedPolicy = Literal["skip", "run_once"]


class TriggerTargetWorkspace(Protocol):
    """Structural protocol avoiding circular runtime imports with `viewport.workspace`."""

    @property
    def session(self) -> Any: ...

    def fire_trigger(
        self,
        flow_name: str,
        kind: TriggerKind = "manual",
        payload: dict[str, Any] | None = None,
        *,
        source: str | None = None,
        scheduled_slot_epoch: int | None = None,
        dry_run: bool = False,
        require_published: bool = True,
    ) -> tuple[ViewportSnapshot, dict[str, Any]] | None: ...


# =====================================================================
# 1. Trigger Specification & Normalized Execution Envelope
# =====================================================================


@dataclass(frozen=True, slots=True)
class TriggerSpec:
    kind: TriggerKind = "manual"
    # Cron fields
    cron_expr: str | None = None
    tz: str = "UTC"
    missed_policy: MissedPolicy = "skip"
    grace_sec: int = 300
    # Event fields (Push Webhook & Pull Polling)
    source: str | None = None
    dedup_path: str | None = None
    map_in: dict[str, str] = field(default_factory=dict)

    def validate(self, flow_name: str = "") -> None:
        prefix = f"[{flow_name}] " if flow_name else ""
        if self.kind not in ("manual", "cron", "event"):
            raise ValueError(f"{prefix}Unsupported trigger kind: {self.kind!r}")

        if self.kind == "cron":
            if not self.cron_expr or not self.cron_expr.strip():
                raise ValueError(f"{prefix}Cron trigger requires a non-empty 'cron_expr'.")
            try:
                compile_cron(self.cron_expr)
            except ValueError as exc:
                raise ValueError(f"{prefix}Invalid cron_expr {self.cron_expr!r}: {exc}") from exc

            if self.tz and self.tz.upper() != "UTC":
                try:
                    ZoneInfo(self.tz)
                except (ZoneInfoNotFoundError, KeyError) as exc:
                    raise ValueError(f"{prefix}Unknown IANA timezone {self.tz!r}.") from exc

            if self.grace_sec < 0:
                raise ValueError(f"{prefix}Cron 'grace_sec' must be >= 0, got {self.grace_sec}.")

        elif self.kind == "event":
            if not (self.source and self.source.strip()):
                raise ValueError(f"{prefix}Event trigger requires a non-empty 'source' identifier.")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind}
        if self.kind == "cron":
            out.update({
                "cron_expr": self.cron_expr,
                "tz": self.tz,
                "missed_policy": self.missed_policy,
                "grace_sec": self.grace_sec,
            })
        elif self.kind == "event":
            out.update({
                "source": self.source,
                "dedup_path": self.dedup_path,
                "map_in": dict(self.map_in),
            })
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TriggerSpec:
        policy = data.get("missed_policy", "skip")
        return cls(
            kind=data.get("kind", "manual"),
            cron_expr=data.get("cron_expr"),
            tz=data.get("tz", "UTC"),
            missed_policy="run_once" if policy == "run_once" else "skip",
            grace_sec=int(data.get("grace_sec", 300)),
            source=data.get("source"),
            dedup_path=data.get("dedup_path"),
            map_in=dict(data.get("map_in") or {}),
        )


DEFAULT_TRIGGERS: tuple[TriggerSpec, ...] = (TriggerSpec(kind="manual"),)


@dataclass(frozen=True, slots=True)
class TriggerEnvelope:
    """Normalized execution envelope emitted when a trigger passes validation and deduplication."""

    flow_name: str
    kind: TriggerKind
    dedup_key: str
    initial_context: dict[str, Any]
    triggered_at: float = field(default_factory=time.time)


# =====================================================================
# 2. Stateful Idempotency & Context Projection Gate
# =====================================================================


class TriggerGate:
    """
    Stateful idempotency & normalization gate for Manual, Cron, and Event triggers.
    Prevents cron startup stampedes, multi-worker duplicates, and polling race loops.
    """

    def __init__(self, seen_keys: set[str] | list[str] | None = None, *, max_history: int = 1000) -> None:
        self._seen: dict[str, float] = {str(k): time.time() for k in (seen_keys or ())}
        self.max_history = max_history

    @property
    def seen_keys(self) -> list[str]:
        return list(self._seen.keys())[-self.max_history :]

    def load_keys(self, keys: list[str] | set[str]) -> None:
        now = time.time()
        for k in keys:
            self._seen[str(k)] = now

    def _commit_key(self, key: str) -> bool:
        if key in self._seen:
            return False
        self._seen[key] = time.time()
        if len(self._seen) > self.max_history * 2:
            self._seen = dict(list(self._seen.items())[-self.max_history :])
        return True

    def prepare(
        self,
        flow_name: str,
        spec: TriggerSpec,
        payload: dict[str, Any] | None = None,
        *,
        scheduled_slot_epoch: int | None = None,
        now_epoch: int | None = None,
        dedup_override: str | None = None,
    ) -> TriggerEnvelope | None:
        """
        Normalize raw trigger input into a `TriggerEnvelope`.
        Returns `None` if outside cron grace window or blocked as a duplicate execution.
        """
        raw = dict(payload or {})
        now = int(now_epoch if now_epoch is not None else time.time())

        if spec.kind == "manual":
            dedup_key = dedup_override or f"manual:{flow_name}:{time.time_ns()}"
            init_ctx = raw

        elif spec.kind == "cron":
            slot = (int(scheduled_slot_epoch) // 60) * 60 if scheduled_slot_epoch is not None else (now // 60) * 60
            age_sec = now - slot
            if age_sec < 0:
                return None
            if spec.missed_policy == "skip" and age_sec >= 60:
                return None
            if spec.missed_policy == "run_once" and age_sec > spec.grace_sec:
                return None

            expr_tag = (spec.cron_expr or "*").strip()
            dedup_key = dedup_override or f"cron:{flow_name}:{expr_tag}:{slot}"
            init_ctx = {**raw, "_cron_slot": slot, "_tz": spec.tz}

        elif spec.kind == "event":
            ev_id = extract_json_path(raw, spec.dedup_path) if spec.dedup_path else None
            if ev_id is None:
                canonical = json.dumps(raw, sort_keys=True, default=str).encode("utf-8")
                ev_id = hashlib.sha256(canonical).hexdigest()[:16]
            dedup_key = dedup_override or f"event:{flow_name}:{spec.source}:{ev_id}"
            init_ctx = (
                {dst: val for dst, src in spec.map_in.items() if (val := extract_json_path(raw, src)) is not None}
                if spec.map_in
                else raw
            )
        else:
            raise ValueError(f"Unsupported trigger kind: {spec.kind!r}")

        if not self._commit_key(dedup_key):
            return None

        return TriggerEnvelope(
            flow_name=flow_name,
            kind=spec.kind,
            dedup_key=dedup_key,
            initial_context=init_ctx,
        )


# =====================================================================
# 3. Active Cron Scheduler & Event Fan-Out Ingress Runtime
# =====================================================================


class TriggerRuntime:
    """
    High-level trigger ingress & scheduler engine bound to `ViewportWorkspace`.
    Uses `compile_cron(...).latest_slot_at(...)` for exact slot matching and grace-window recovery,
    and fans out inbound events by `source` across all published workflows.
    """

    def __init__(self, workspace: TriggerTargetWorkspace) -> None:
        self.workspace = workspace

    def _active_flow_names(self, *, include_drafts: bool) -> list[str]:
        if include_drafts:
            return list(self.workspace.session.registry.flows.keys())
        return sorted(self.workspace.session.published_flows)

    def resolve_cron_slot(self, spec: TriggerSpec, now_epoch: int) -> int | None:
        """Resolve the matching minute-aligned cron slot at `now_epoch` respecting `missed_policy`."""
        if spec.kind != "cron" or not spec.cron_expr:
            return None
        compiled = compile_cron(spec.cron_expr)
        lookback_min = (max(0, spec.grace_sec) // 60) if spec.missed_policy == "run_once" else 0
        slot = compiled.latest_slot_at(now_epoch, spec.tz, lookback_minutes=lookback_min)
        if slot is None:
            return None
        if spec.missed_policy == "run_once" and (now_epoch - slot) > spec.grace_sec:
            return None
        return slot

    def tick_cron(
        self,
        now_epoch: int | None = None,
        *,
        dry_run: bool = False,
        include_drafts: bool = False,
    ) -> list[tuple[str, ViewportSnapshot, dict[str, Any]]]:
        """
        Scan registered `cron` triggers at `now_epoch` and fire matching flows idempotently.
        Recovers missed ticks within `grace_sec` when `missed_policy == 'run_once'`.
        """
        ts = int(now_epoch if now_epoch is not None else time.time())
        executed: list[tuple[str, ViewportSnapshot, dict[str, Any]]] = []

        for flow_name in self._active_flow_names(include_drafts=include_drafts):
            for spec in self.workspace.session.flow_triggers.get(flow_name, ()):
                slot_epoch = self.resolve_cron_slot(spec, ts)
                if slot_epoch is None:
                    continue

                # Execute directly via workspace using the matched spec's slot
                env = self.workspace.trigger_gate.prepare(
                    flow_name,
                    spec,
                    scheduled_slot_epoch=slot_epoch,
                    now_epoch=ts,
                )
                if env is None:
                    continue

                snap, out = (
                    self.workspace.test_dry_run(flow_name, initial_context=env.initial_context)
                    if dry_run
                    else self.workspace.session.execute_with_trace(flow_name, initial_context=env.initial_context)
                )
                executed.append((flow_name, snap, out))

        return executed

    def ingest_event(
        self,
        source: str,
        payload: dict[str, Any],
        *,
        dry_run: bool = False,
        include_drafts: bool = False,
    ) -> list[tuple[str, ViewportSnapshot, dict[str, Any]]]:
        """
        Fan-out an inbound event from `source` (e.g. `'github.push'`, `'slack.command'`)
        to all subscribed workflows, enforcing per-flow deduplication and `map_in` projection.
        """
        executed: list[tuple[str, ViewportSnapshot, dict[str, Any]]] = []

        for flow_name in self._active_flow_names(include_drafts=include_drafts):
            specs = self.workspace.session.flow_triggers.get(flow_name, ())
            if not any(s.kind == "event" and s.source == source for s in specs):
                continue

            res = self.workspace.fire_trigger(
                flow_name,
                kind="event",
                payload=payload,
                source=source,
                dry_run=dry_run,
                require_published=not include_drafts,
            )
            if res is not None:
                executed.append((flow_name, res[0], res[1]))

        return executed