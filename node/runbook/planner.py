# fiber.node.runbook.planner
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import time
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fiber.node.runbook.manifest import RunbookManifest

if TYPE_CHECKING:
    from fiber.node.runbook.trigger.base import TriggerSpec

_MAX_SEARCH_MINUTES = 366 * 24 * 60  # 윤년 포함 최대 1년 탐색


@dataclass(frozen=True, slots=True)
class ScheduleVerdict:
    auto_runnable: bool
    manual_only: bool
    blocking_runtime_slots: tuple[str, ...]
    next_run_epoch: float | None
    display_label: str
    reason: str = ""


def _parse_cron_field(token: str, min_val: int, max_val: int) -> set[int]:
    """단일 크론 필드를 허용 정수 집합으로 변환 (*, */n, a-b, a-b/n, a,b 지원)."""
    allowed: set[int] = set()
    for part in token.split(","):
        part = part.strip()
        if not part:
            raise ValueError("Empty cron field segment.")

        step = 1
        if "/" in part:
            part, step_str = part.split("/", 1)
            step = int(step_str)
            if step <= 0:
                raise ValueError(f"Cron step must be positive: {step}")

        if part == "*":
            start, end = min_val, max_val
        elif "-" in part:
            s_str, e_str = part.split("-", 1)
            start, end = int(s_str), int(e_str)
        else:
            val = int(part)
            if step == 1:
                if not (min_val <= val <= max_val):
                    raise ValueError(f"Cron value {val} out of bounds [{min_val}, {max_val}].")
                allowed.add(val)
                continue
            start, end = val, max_val

        if start < min_val or end > max_val or start > end:
            raise ValueError(f"Cron range {start}-{end} out of bounds [{min_val}, {max_val}].")
        allowed.update(range(start, end + 1, step))

    return allowed


def _resolve_tz(tz_name: str | None) -> timezone | ZoneInfo:
    if not tz_name or tz_name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return timezone.utc


def compute_next_cron_epoch(cron_expr: str, *, now_epoch: float, tz_name: str = "UTC") -> float:
    """5필드 크론식(분 시 일 월 요일) 기준 now_epoch 이후 가장 빠른 실행 시각(epoch) 계산."""
    fields = cron_expr.split()
    if len(fields) != 5:
        raise ValueError(f"Expected 5-field cron expression, got: '{cron_expr}'")

    minutes = _parse_cron_field(fields[0], 0, 59)
    hours = _parse_cron_field(fields[1], 0, 23)
    days = _parse_cron_field(fields[2], 1, 31)
    months = _parse_cron_field(fields[3], 1, 12)
    # 크론 요일: 0 또는 7 = 일요일, 1 = 월요일 ... 6 = 토요일
    raw_dows = _parse_cron_field(fields[4], 0, 7)
    cron_dows = {(0 if d == 7 else d) for d in raw_dows}
    dom_wild = fields[2].strip() == "*"
    dow_wild = fields[4].strip() == "*"

    tz = _resolve_tz(tz_name)
    cursor = datetime.fromtimestamp(now_epoch, tz=tz).replace(second=0, microsecond=0) + timedelta(minutes=1)

    visited = 0
    while visited < _MAX_SEARCH_MINUTES:
        if cursor.month not in months:
            # 다음 달 1일 00:00으로 점프
            next_year = cursor.year + (1 if cursor.month == 12 else 0)
            next_month = 1 if cursor.month == 12 else cursor.month + 1
            nxt = cursor.replace(year=next_year, month=next_month, day=1, hour=0, minute=0)
            visited += max(1, int((nxt - cursor).total_seconds() // 60))
            cursor = nxt
            continue

        # Python weekday(): 월=0..일=6 -> 크론 요일: 일=0, 월=1..토=6
        c_dow = (cursor.weekday() + 1) % 7
        day_match = (
            (cursor.day in days and c_dow in cron_dows)
            if (dom_wild or dow_wild)
            else (cursor.day in days or c_dow in cron_dows)
        )
        if not day_match:
            nxt = (cursor + timedelta(days=1)).replace(hour=0, minute=0)
            visited += max(1, int((nxt - cursor).total_seconds() // 60))
            cursor = nxt
            continue

        if cursor.hour not in hours:
            nxt = (cursor + timedelta(hours=1)).replace(minute=0)
            visited += max(1, int((nxt - cursor).total_seconds() // 60))
            cursor = nxt
            continue

        if cursor.minute not in minutes:
            cursor += timedelta(minutes=1)
            visited += 1
            continue

        return cursor.timestamp()

    raise ValueError(f"No reachable cron slot within 1 year for '{cron_expr}'.")


def _format_schedule_label(cron_expr: str | None, next_epoch: float | None, tz_name: str, fallback_kind: str) -> str:
    """L1 목록용 스케줄 라벨 포맷 (예: '⏱ 매일 23:00', '⏱ 12/01 09:00')."""
    if cron_expr:
        parts = cron_expr.split()
        if len(parts) == 5 and parts[2] == "*" and parts[3] == "*" and parts[4] == "*":
            if parts[0].isdigit() and parts[1].isdigit():
                return f"⏱ 매일 {int(parts[1]):02d}:{int(parts[0]):02d}"
    if next_epoch is not None:
        dt = datetime.fromtimestamp(next_epoch, tz=_resolve_tz(tz_name))
        return f"⏱ {dt.strftime('%m/%d %H:%M')}"
    return f"⏱ {fallback_kind}"

class SchedulePlanner:
    def evaluate(
        self,
        manifest: RunbookManifest,
        triggers: tuple[TriggerSpec, ...],
        *,
        now_epoch: float | None = None,
    ) -> ScheduleVerdict:
        now = now_epoch if now_epoch is not None else time.time()
        rt_slots = manifest.runtime_slot_names()
        auto_triggers = [t for t in triggers if getattr(t, "kind", "manual") != "manual"]

        if rt_slots:
            slot_list = ", ".join(rt_slots)
            return ScheduleVerdict(
                auto_runnable=False,
                manual_only=True,
                blocking_runtime_slots=rt_slots,
                next_run_epoch=None,
                display_label="수동 전용",
                reason=f"자동 실행: 불가 ⓘ '실행 시' 슬롯 {len(rt_slots)}개 ({slot_list})",
            )

        if not auto_triggers:
            return ScheduleVerdict(
                auto_runnable=False,
                manual_only=True,
                blocking_runtime_slots=(),
                next_run_epoch=None,
                display_label="수동 전용",
                reason="자동 실행 트리거 미설정 (manual 전용)",
            )

        earliest_epoch: float | None = None
        chosen_trigger = auto_triggers[0]

        for trig in auto_triggers:
            cron_expr = getattr(trig, "cron_expr", None)
            tz_name = getattr(trig, "tz", "UTC") or "UTC"
            if cron_expr:
                candidate = compute_next_cron_epoch(cron_expr, now_epoch=now, tz_name=tz_name)
                if earliest_epoch is None or candidate < earliest_epoch:
                    earliest_epoch = candidate
                    chosen_trigger = trig

        chosen_cron = getattr(chosen_trigger, "cron_expr", None)
        chosen_tz = getattr(chosen_trigger, "tz", "UTC") or "UTC"
        chosen_kind = getattr(chosen_trigger, "kind", "schedule")

        return ScheduleVerdict(
            auto_runnable=True,
            manual_only=False,
            blocking_runtime_slots=(),
            next_run_epoch=earliest_epoch,
            display_label=_format_schedule_label(chosen_cron, earliest_epoch, chosen_tz, chosen_kind),
            reason="자동 실행 가능 ('실행 시' 슬롯 0개)",
        )