"""Parse a time window out of a natural-language question.

Deterministic and dependency-free. Recognises relative windows ("last week",
"last 3 days", "yesterday", "this month") and explicit ISO dates
("on 2026-08-20", "from 2026-08-01 to 2026-08-07", "since 2026-08-15").

Anything that names a window but cannot be resolved to a sane [start, end)
raises ``InvalidDateRangeError`` — the caller must not silently fall back to a
default in that case, or the answer would describe the wrong period.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from chat.models import TimeRange

_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_LAST_N = re.compile(r"\blast\s+(\d+)\s+(hour|day|week|month)s?\b", re.I)
_PAST_N = re.compile(r"\b(?:past|previous)\s+(\d+)\s+(hour|day|week|month)s?\b", re.I)
_MAX_LOOKBACK_DAYS = 366 * 3


class InvalidDateRangeError(ValueError):
    """The question named a time window that cannot be resolved."""


def _day(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _parse_iso(y: str, m: str, d: str) -> datetime:
    try:
        return datetime(int(y), int(m), int(d), tzinfo=UTC)
    except ValueError as exc:  # e.g. 2026-13-40
        raise InvalidDateRangeError(f"{y}-{m}-{d} is not a real date") from exc


def _unit_delta(n: int, unit: str) -> timedelta:
    unit = unit.lower()
    if unit == "hour":
        return timedelta(hours=n)
    if unit == "day":
        return timedelta(days=n)
    if unit == "week":
        return timedelta(weeks=n)
    if unit == "month":
        return timedelta(days=30 * n)
    raise InvalidDateRangeError(f"unsupported time unit: {unit!r}")


def has_time_expression(text: str) -> bool:
    """True if the text contains anything this module would try to resolve."""
    lowered = text.lower()
    keywords = (
        "last ", "past ", "previous ", "yesterday", "today", "this week",
        "this month", "since ", " to ", "between ", "on 20", "week", "month",
        "recent", "so far",
    )
    return bool(_ISO_DATE.search(text)) or any(k in lowered for k in keywords)


def parse_time_range(
    text: str,
    *,
    now: datetime | None = None,
    default_days: int | None = 7,
) -> TimeRange:
    """Resolve a window from ``text``.

    Args:
        text: the user's question.
        now: reference "current" time (UTC, tz-aware). Defaults to ``datetime.now(UTC)``.
        default_days: window to use when the text names no time period at all.
            Pass ``None`` to require an explicit period (raises InvalidDateRangeError).

    Raises:
        InvalidDateRangeError: the text names a period that cannot be resolved,
            is inverted, entirely in the future, or absurdly far in the past.
    """
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("`now` must be timezone-aware")
    lowered = text.lower()

    try:
        tr = (
            _explicit_range(text, now)
            or _since(text, now)
            or _single_day(text, lowered, now)
            or _relative(lowered, now)
        )
    except InvalidDateRangeError:
        raise
    except ValueError as exc:
        # e.g. TimeRange rejecting start > end
        raise InvalidDateRangeError(str(exc)) from exc

    if tr is None:
        if not has_time_expression(text):
            if default_days is None:
                raise InvalidDateRangeError(
                    "no time period given; specify e.g. 'last week' or a date range"
                )
            return TimeRange(
                start=now - timedelta(days=default_days),
                end=now,
                label=f"last {default_days} days",
            )
        raise InvalidDateRangeError(
            "could not understand the time period; use 'last N days', "
            "'yesterday', 'this week', or 'from YYYY-MM-DD to YYYY-MM-DD'"
        )

    _validate(tr, now)
    return tr


def _validate(tr: TimeRange, now: datetime) -> None:
    if tr.start >= tr.end:
        raise InvalidDateRangeError(
            f"range start ({tr.start.date()}) is not before end ({tr.end.date()})"
        )
    if tr.start > now:
        raise InvalidDateRangeError(
            f"range starts in the future ({tr.start.date()}); no data can exist yet"
        )
    if (now - tr.start).days > _MAX_LOOKBACK_DAYS:
        raise InvalidDateRangeError("range starts more than 3 years ago")


def _explicit_range(text: str, now: datetime) -> TimeRange | None:
    # "from A to B", "between A and B", "A .. B", "A - B"
    dates = _ISO_DATE.findall(text)
    lowered = text.lower()
    connective = (
        " to " in lowered or "between" in lowered or ".." in text
        or re.search(r"\d\s*-\s*\d{4}-", text) is not None
    )
    if len(dates) >= 2 and connective:
        start = _parse_iso(*dates[0])
        end_date = _parse_iso(*dates[1])
        end = end_date + timedelta(days=1)  # inclusive end date
        if start >= end:
            raise InvalidDateRangeError(
                f"start date {start.date()} is not before end date {end_date.date()}"
            )
        if start < now < end:
            end = now  # clamp an open-ended future bound to 'now'
        return TimeRange(start=start, end=end, label=f"{start.date()} to {end_date.date()}")
    return None


def _since(text: str, now: datetime) -> TimeRange | None:
    m = re.search(r"\bsince\s+(\d{4})-(\d{2})-(\d{2})\b", text, re.I)
    if not m:
        return None
    start = _parse_iso(*m.groups())
    return TimeRange(start=start, end=now, label=f"since {start.date()}")


def _single_day(text: str, lowered: str, now: datetime) -> TimeRange | None:
    m = re.search(r"\bon\s+(\d{4})-(\d{2})-(\d{2})\b", text, re.I)
    if m:
        start = _parse_iso(*m.groups())
        return TimeRange(start=start, end=start + timedelta(days=1), label=str(start.date()))
    if "yesterday" in lowered:
        start = _day(now) - timedelta(days=1)
        return TimeRange(start=start, end=start + timedelta(days=1), label="yesterday")
    if "today" in lowered:
        start = _day(now)
        return TimeRange(start=start, end=now, label="today")
    # bare date with no "on"/range connective
    solo = _ISO_DATE.findall(text)
    if len(solo) == 1 and " to " not in lowered and "since" not in lowered and "between" not in lowered:
        start = _parse_iso(*solo[0])
        return TimeRange(start=start, end=start + timedelta(days=1), label=str(start.date()))
    return None


def _relative(lowered: str, now: datetime) -> TimeRange | None:
    m = _LAST_N.search(lowered) or _PAST_N.search(lowered)
    if m:
        n = int(m.group(1))
        if n <= 0:
            raise InvalidDateRangeError("time period must be a positive number")
        return TimeRange(start=now - _unit_delta(n, m.group(2)), end=now, label=m.group(0))

    if "last week" in lowered or "past week" in lowered or "previous week" in lowered:
        return TimeRange(start=now - timedelta(weeks=1), end=now, label="last week")
    if "last month" in lowered or "past month" in lowered or "previous month" in lowered:
        return TimeRange(start=now - timedelta(days=30), end=now, label="last month")
    if "this week" in lowered:
        start = _day(now) - timedelta(days=now.weekday())
        return TimeRange(start=start, end=now, label="this week")
    if "this month" in lowered:
        start = _day(now).replace(day=1)
        return TimeRange(start=start, end=now, label="this month")
    if "last 24 hours" in lowered or "last day" in lowered:
        return TimeRange(start=now - timedelta(days=1), end=now, label="last 24 hours")
    if "recently" in lowered or "so far" in lowered or "recent" in lowered:
        return TimeRange(start=now - timedelta(days=7), end=now, label="the last 7 days")
    return None
