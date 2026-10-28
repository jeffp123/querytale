"""Parse --last / --date / --from / --to into a UTC window. All user input is local time."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

_DURATION = re.compile(r"^\s*(\d+)\s*([mhdw])\s*$", re.IGNORECASE)
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


class WindowError(ValueError):
    pass


@dataclass(frozen=True)
class Window:
    start: datetime  # UTC, naive
    end: datetime  # UTC, naive, exclusive
    label: str


def parse_duration(text: str) -> timedelta:
    m = _DURATION.match(text)
    if not m:
        raise WindowError(f"Can't read duration {text!r}; use e.g. 90m, 6h, 3d, 2w")
    return timedelta(**{_UNITS[m.group(2).lower()]: int(m.group(1))})


def _to_utc(local: datetime, zone: ZoneInfo) -> datetime:
    return local.replace(tzinfo=zone).astimezone(UTC).replace(tzinfo=None)


def _parse_local(text: str, zone: ZoneInfo) -> datetime:
    try:
        return _to_utc(datetime.fromisoformat(text.strip()), zone)
    except ValueError:
        hint = "use e.g. 2026-10-27 or '2026-10-27 15:00'"
        raise WindowError(f"Can't read time {text!r}; {hint}") from None


def parse_window(
    *,
    last: str | None = None,
    on_date: str | None = None,
    from_: str | None = None,
    to: str | None = None,
    tz: str,
    now: datetime | None = None,
    default_last: str = "24h",
) -> Window:
    zone = ZoneInfo(tz)
    now = now or datetime.now(UTC).replace(tzinfo=None)
    if sum(x is not None for x in (last, on_date)) + (from_ is not None or to is not None) > 1:
        raise WindowError("Use only one of --last, --date, or --from/--to")
    if on_date:
        try:
            day = date.fromisoformat(on_date)
        except ValueError:
            raise WindowError(f"Can't read date {on_date!r}; use YYYY-MM-DD") from None
        start = _to_utc(datetime.combine(day, datetime.min.time()), zone)
        end = _to_utc(datetime.combine(day + timedelta(days=1), datetime.min.time()), zone)
        return Window(start, end, day.isoformat())
    if from_ or to:
        if not from_:
            raise WindowError("--to needs --from")
        start = _parse_local(from_, zone)
        end = _parse_local(to, zone) if to else now
        if end <= start:
            raise WindowError("--to must be after --from")
        return Window(start, end, f"{from_} to {to or 'now'}")
    last = last or default_last
    return Window(now - parse_duration(last), now, f"last {last}")
