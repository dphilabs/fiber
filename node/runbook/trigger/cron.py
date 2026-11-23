# fiber.node.runbook.trigger.cron
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

_MACROS: dict[str, str] = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

_MONTH_NAMES = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
_DOW_NAMES = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}


def _replace_names(expr: str, mapping: dict[str, int]) -> str:
    upper = expr.upper()
    for name, num in mapping.items():
        upper = upper.replace(name, str(num))
    return upper


def _parse_field(
    field_str: str,
    min_val: int,
    max_val: int,
    *,
    names: dict[str, int] | None = None,
    is_dow: bool = False,
) -> tuple[frozenset[int], bool, bool]:
    """Returns `(allowed_values, is_wildcard, has_last_day_flag)`."""
    raw = _replace_names(field_str.strip(), names) if names else field_str.strip().upper()
    if raw == "*":
        return frozenset(range(min_val, max_val + 1)), True, False

    values: set[int] = set()
    has_last = False

    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "L" and not is_dow:
            has_last = True
            continue

        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError(f"Cron step must be > 0 in '{field_str}'")

        if part == "*":
            start, end = min_val, max_val
        elif "-" in part:
            s_str, e_str = part.split("-", 1)
            start, end = int(s_str), int(e_str)
            if is_dow and end == 7:
                end = 7  # handled in loop below
        else:
            val = int(part)
            if is_dow and val == 7:
                val = 0
            if not (min_val <= val <= max_val):
                raise ValueError(f"Cron value {val} out of range [{min_val}, {max_val}] in '{field_str}'")
            values.add(val)
            continue

        max_bound = 7 if is_dow else max_val
        if start < min_val or end > max_bound or start > end:
            raise ValueError(f"Invalid cron range '{part}' in '{field_str}'")

        for v in range(start, end + 1, step):
            values.add(0 if (is_dow and v == 7) else v)

    return frozenset(values), False, has_last


@dataclass(frozen=True, slots=True)
class CompiledCron:
    expr: str
    minutes: frozenset[int]
    hours: frozenset[int]
    doms: frozenset[int]
    months: frozenset[int]
    dows: frozenset[int]
    dom_is_star: bool
    dow_is_star: bool
    dom_has_last: bool

    def _match_day(self, year: int, month: int, day: int, cron_dow: int) -> bool:
        last_dom = calendar.monthrange(year, month)[1]
        dom_match = (day in self.doms) or (self.dom_has_last and day == last_dom)
        dow_match = cron_dow in self.dows

        # POSIX Vixie rule: if both DOM and DOW are restricted (non-'*'), match if EITHER matches
        if not self.dom_is_star and not self.dow_is_star:
            return dom_match or dow_match
        return dom_match and dow_match

    def matches_dt(self, dt: datetime) -> bool:
        cron_dow = (dt.weekday() + 1) % 7
        return (
            dt.minute in self.minutes
            and dt.hour in self.hours
            and dt.month in self.months
            and self._match_day(dt.year, dt.month, dt.day, cron_dow)
        )

    def latest_slot_at(self, now_epoch: int, tz_name: str = "UTC", *, lookback_minutes: int = 60) -> int | None:
        """
        Find the most recent matching minute-aligned slot epoch within `[now - lookback_minutes, now]`.
        Enables exact recovery of missed cron ticks within `grace_sec` without external DB timers.
        """
        tz = ZoneInfo(tz_name) if tz_name and tz_name.upper() != "UTC" else timezone.utc
        aligned_epoch = (int(now_epoch) // 60) * 60
        base_dt = datetime.fromtimestamp(aligned_epoch, tz=tz)

        for offset in range(max(0, lookback_minutes) + 1):
            cand_dt = base_dt - timedelta(minutes=offset)
            if self.matches_dt(cand_dt):
                return aligned_epoch - (offset * 60)
        return None


@lru_cache(maxsize=256)
def compile_cron(expr: str) -> CompiledCron:
    norm = _MACROS.get(expr.strip().lower(), expr.strip())
    fields = norm.split()
    if len(fields) != 5:
        raise ValueError(f"Expected 5-field cron expression or @macro, got {expr!r}")

    minutes, _, _ = _parse_field(fields[0], 0, 59)
    hours, _, _ = _parse_field(fields[1], 0, 23)
    doms, dom_star, dom_last = _parse_field(fields[2], 1, 31)
    months, _, _ = _parse_field(fields[3], 1, 12, names=_MONTH_NAMES)
    dows, dow_star, _ = _parse_field(fields[4], 0, 6, names=_DOW_NAMES, is_dow=True)

    return CompiledCron(
        expr=expr,
        minutes=minutes,
        hours=hours,
        doms=doms,
        months=months,
        dows=dows,
        dom_is_star=dom_star,
        dow_is_star=dow_star,
        dom_has_last=dom_last,
    )