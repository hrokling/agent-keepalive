from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import re

_TOKEN_RE = re.compile(r"\s*(\d+)\s*([smhd]?)\s*")
_UNIT_SECONDS = {
    "": 1,
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
}


def parse_duration(value: str) -> timedelta:
    text = value.strip().lower()
    if not text:
        raise ValueError("duration cannot be empty")

    total_seconds = 0
    position = 0
    while position < len(text):
        match = _TOKEN_RE.match(text, position)
        if not match:
            raise ValueError(
                f"invalid duration {value!r}; expected values like 1h, 90m, or 2h30m"
            )
        amount = int(match.group(1))
        unit = match.group(2)
        total_seconds += amount * _UNIT_SECONDS[unit]
        position = match.end()

    if total_seconds <= 0:
        raise ValueError("duration must be greater than zero")
    return timedelta(seconds=total_seconds)


def format_duration(value: timedelta) -> str:
    total_seconds = int(value.total_seconds())
    if total_seconds < 0:
        total_seconds = 0

    parts: list[str] = []
    days, remainder = divmod(total_seconds, 24 * 60 * 60)
    hours, remainder = divmod(remainder, 60 * 60)
    minutes, seconds = divmod(remainder, 60)

    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return "".join(parts)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def format_relative_time(
    moment: datetime | None,
    *,
    now: datetime | None = None,
) -> str:
    if moment is None:
        return "-"
    now = now or utc_now()
    delta = now - moment
    if delta.total_seconds() < 0:
        delta = timedelta(seconds=0)
    return f"{format_duration(delta)} ago"
