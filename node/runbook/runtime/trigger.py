# fiber.node.runbook.runtime.trigger
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import time
from typing import TYPE_CHECKING, Any, Protocol

from xphi.arch.reflex.fsm import extract_json_path
from xphi.node.runtime.trigger.spec import TriggerKind, TriggerSpec
from xphi.node.runtime.trigger.cron import compile_cron

if TYPE_CHECKING:
    from fiber.node.runbook.runtime.projector import ViewportSnapshot


class TriggerTargetWorkspace(Protocol):
    """Structural protocol for `ViewportWorkspace` bound to `TriggerRuntime`."""

    @property
    def session(self) -> Any: ...

    @property
    def trigger_gate(self) -> TriggerGate: ...

    def test_dry_run(
        self,
        flow_name: str,
        *,
        initial_context: dict[str, Any] | None = None,
    ) -> tuple[ViewportSnapshot, dict[str, Any]]: ...

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


@dataclass(frozen=True, slots=True)
class TriggerEnvelope:
    flow_name: str
    kind: TriggerKind
    dedup_key: str
    initial_context: dict[str, Any]
    triggered_at: float = field(default_factory=time.time)


class TriggerGate:
    """Stateful idempotency & normalization gate for Manual, Cron, and Event triggers."""

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

        elif spec.kind in ("event", "poll"):
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


class TriggerRuntime:
    """High-level trigger ingress & scheduler engine bound to `ViewportWorkspace`."""

    def __init__(self, workspace: TriggerTargetWorkspace) -> None:
        self.workspace = workspace

    def _active_flow_names(self, *, include_drafts: bool) -> list[str]:
        if include_drafts:
            return list(self.workspace.session.registry.flows.keys())
        return sorted(self.workspace.session.published_flows)

    def resolve_cron_slot(self, spec: TriggerSpec, now_epoch: int) -> int | None:
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
        ts = int(now_epoch if now_epoch is not None else time.time())
        executed: list[tuple[str, ViewportSnapshot, dict[str, Any]]] = []

        for flow_name in self._active_flow_names(include_drafts=include_drafts):
            for spec in self.workspace.session.flow_triggers.get(flow_name, ()):
                slot_epoch = self.resolve_cron_slot(spec, ts)
                if slot_epoch is None:
                    continue

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