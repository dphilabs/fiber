# fiber.gateway.bridge.reflex.ledger
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any, Literal
import uuid

from xphi.arch.bound.adapter.pta import (
    NodeSigner,
    PtaAdapter,
    PtaInput,
    PtaPointer,
    PtaTransaction,
    create_mcp_state_anchor,
)
from xphi.arch.bound.adapter.state import StateAdapter

from fiber.node.runbook.manifest import OutputPolicy, RunbookManifest
from fiber.node.runbook.planner import SchedulePlanner

if TYPE_CHECKING:
    from fiber.node.runbook.store import PinnedRelease

RunOutcome = Literal["running", "ok", "no_change", "paused", "failed"]
_BADGES: dict[str, str] = {"ok": "✔", "failed": "✖", "no_change": "– 변경없음", "paused": "⏸ 대기중", "running": "▶ 실행중", "never": "–"}


@dataclass(frozen=True, slots=True)
class StepStreamEvent:
    run_id: str
    seq: int
    kind: str
    node_name: str
    timestamp: float = field(default_factory=time.time)
    duration_ms: int = 0
    inputs_sha: str | None = None
    outputs_sha: str | None = None
    l1_summary: str = ""
    error_line: str | None = None
    sub_run_id: str | None = None


@dataclass(slots=True)
class RunRecord:
    run_id: str
    flow_name: str
    version: int
    trigger_kind: str
    started_at: float
    finished_at: float | None = None
    outcome: RunOutcome = "running"
    failure_headline: str | None = None
    head_tx_hash: str | None = None
    entry_io_refs: dict[str, str] = field(default_factory=dict)
    events: list[StepStreamEvent] = field(default_factory=list)
    formatted_outputs: dict[str, Any] = field(default_factory=dict)
    notify_outputs: dict[str, Any] = field(default_factory=dict)
    summary_text: str | None = None


def _infer_widget(val: Any) -> str:
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return "metric"
    if isinstance(val, list):
        return "table"
    return "link" if isinstance(val, str) and val.startswith(("http://", "https://", "file://", "upload://")) else "text"

class RunLedger:
    def __init__(
        self,
        runs_dir: Path | None = None,
        *,
        planner: SchedulePlanner | None = None,
        pta: PtaAdapter | None = None,
    ) -> None:
        self.runs_dir = runs_dir
        self.planner = planner or SchedulePlanner()
        self.pta = pta
        self._runs: dict[str, RunRecord] = {}
        self._latest_by_flow: dict[str, str] = {}
        self._subscribers: list[Callable[[StepStreamEvent], None]] = []

    def _seal_anchor(self, rec: RunRecord, status: str, payload: dict[str, Any]) -> str:
        inputs: list[PtaInput] = []
        if rec.head_tx_hash:
            ptr = PtaPointer(tx_hash=rec.head_tx_hash, output_index=0)
            signer = NodeSigner.get_instance()
            sig = signer.sign_payload(StateAdapter.to_canonical_bytes({"payload": ptr.to_key()}))
            inputs.append(PtaInput(pointer=ptr, signature=sig, owner_address=signer.pubkey_hex))

        anchor = create_mcp_state_anchor(rec.run_id, status, payload)
        tx = PtaTransaction(inputs=inputs, outputs=[anchor], metadata={"flow": rec.flow_name, "ver": rec.version})
        rec.head_tx_hash = tx.tx_hash

        if self.pta is not None:
            coro = self.pta.execute_transaction(tx, stream_id=rec.run_id)
            try:
                asyncio.get_running_loop().create_task(coro)
            except RuntimeError:
                asyncio.run(coro)
        return tx.tx_hash

    def subscribe(self, listener: Callable[[StepStreamEvent], None]) -> Callable[[], None]:
        self._subscribers.append(listener)
        return lambda: self._subscribers.remove(listener) if listener in self._subscribers else None

    def get_run(self, run_id: str) -> RunRecord:
        return self._runs[run_id]

    def begin(self, flow_name: str, version: int, trigger_kind: str, io_refs: dict[str, Any]) -> str:
        run_id = f"run_{uuid.uuid4().hex[:8]}"
        refs = {k: getattr(v, "sha256", str(v)) for k, v in (io_refs or {}).items()}
        rec = RunRecord(run_id=run_id, flow_name=flow_name, version=version, trigger_kind=trigger_kind, started_at=time.time(), entry_io_refs=refs)
        self._runs[run_id] = rec
        self._latest_by_flow[flow_name] = run_id
        self._seal_anchor(rec, "RUNNING", {"trigger": trigger_kind, "io_refs": refs})
        return run_id

    def emit(self, event: StepStreamEvent) -> None:
        if rec := self._runs.get(event.run_id):
            rec.events.append(event)
            if event.kind == "node.failed" and event.error_line:
                rec.failure_headline = f"{event.node_name} 실패 · {event.error_line}"
            self._seal_anchor(rec, event.kind, asdict(event))
        for sub in tuple(self._subscribers):
            sub(event)

    def catch_halt(self, run_id: str, *, failed_node: str, error: str) -> tuple[RunOutcome, dict[str, Any], float]:
        rec = self._runs[run_id]
        line = error.splitlines()[0].strip() if error else "실행 중단"
        self.emit(StepStreamEvent(run_id=run_id, seq=len(rec.events) + 1, kind="node.failed", node_name=failed_node, error_line=line, l1_summary=f"실패 · {line}"))
        return "failed", {}, 0.0

    def finalize(self, run_id: str, outcome: str, outputs: dict[str, Any], manifest: RunbookManifest, *, score: float = 1.0) -> RunRecord:
        rec = self._runs[run_id]
        rec.outcome = "no_change" if (outcome == "ok" and score == 0.0) else outcome  # type: ignore[assignment]
        if rec.outcome != "paused":
            rec.finished_at = time.time()
        if isinstance(outputs.get("_summary"), str):
            rec.summary_text = outputs["_summary"]

        keys = dict.fromkeys([*manifest.outputs, *(k for k in outputs if not k.startswith("_"))])
        for k in keys:
            if k not in outputs:
                continue
            pol = manifest.outputs.get(k) or OutputPolicy(key=k, widget=_infer_widget(outputs[k]))  # type: ignore[arg-type]
            item = {"value": outputs[k], "widget": pol.widget}
            if pol.show_in_result:
                rec.formatted_outputs[k] = item
            if pol.include_in_notify:
                rec.notify_outputs[k] = item

        self._seal_anchor(rec, rec.outcome, {"outputs": rec.formatted_outputs, "headline": rec.failure_headline})
        return rec

    def summaries(self, releases: list[PinnedRelease], stale_flows: list[str], now_epoch: float) -> list[dict[str, Any]]:
        stale_set = set(stale_flows)
        rows: list[dict[str, Any]] = []
        for rel in releases:
            last = self._runs.get(self._latest_by_flow.get(rel.flow_name, ""))
            outcome = last.outcome if last else "never"
            sched = self.planner.evaluate(rel.manifest, rel.triggers, now_epoch=now_epoch)
            stale = rel.flow_name in stale_set
            rows.append({
                "flow_name": rel.flow_name,
                "version": rel.version,
                "last_run_id": last.run_id if last else None,
                "head_tx_hash": last.head_tx_hash if last else None,
                "last_outcome": outcome,
                "status_badge": "⚠ 재컴파일 필요" if stale else _BADGES.get(outcome, outcome),
                "failure_headline": last.failure_headline if (last and outcome == "failed") else None,
                "next_schedule": sched.display_label,
                "auto_runnable": sched.auto_runnable,
                "recompile_needed": stale,
            })
        return rows

    def project_detail_view(self, run_id: str, manifest: RunbookManifest, *, raw_l2: bool = False) -> dict[str, Any]:
        rec = self._runs[run_id]
        return {
            "run_id": rec.run_id,
            "flow_name": rec.flow_name,
            "version": rec.version,
            "head_tx_hash": rec.head_tx_hash,
            "outcome": rec.outcome,
            "status_badge": _BADGES.get(rec.outcome, rec.outcome),
            "failure_headline": rec.failure_headline,
            "view_level": "L2" if raw_l2 else "L1",
            "timeline": [
                {
                    "seq": ev.seq,
                    "node_name": ev.node_name,
                    "kind": ev.kind,
                    "is_milestone": ev.node_name in manifest.milestones,
                    "expanded": raw_l2 or (ev.node_name in manifest.milestones) or ev.kind in ("node.failed", "run.paused"),
                    "duration_sec": round(ev.duration_ms / 1000.0, 2),
                    "l1_summary": ev.l1_summary,
                    "inputs_sha": ev.inputs_sha,
                    "outputs_sha": ev.outputs_sha,
                    "error_line": ev.error_line,
                }
                for ev in rec.events
            ],
            "results": rec.formatted_outputs,
            "summary": rec.summary_text,
        }