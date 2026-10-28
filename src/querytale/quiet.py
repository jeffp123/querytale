"""Quiet windows: recurring times when screens are expected to be idle.

They are calibration ground truth, never rules: a session found inside one is either a threshold
to tighten or a real exception worth a look.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from querytale.config import Config, ConfigError

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WHEN = re.compile(r"^\s*([a-z]{3})[a-z]*\s+(\d{1,2}):(\d{2})\s*$", re.IGNORECASE)

CANDLE_LIGHTING = timedelta(minutes=18)  # before Friday sunset
NIGHTFALL_DEPRESSION = 8.5  # degrees below the horizon on Saturday evening


@dataclass(frozen=True)
class QuietInterval:
    name: str
    start: datetime  # UTC, naive
    end: datetime


def _parse_when(text: str) -> tuple[int, time]:
    m = _WHEN.match(text)
    if not m or m.group(1).lower() not in DAYS:
        raise ConfigError(f"Quiet window time {text!r}: use e.g. 'fri 20:30'")
    return DAYS.index(m.group(1).lower()), time(int(m.group(2)), int(m.group(3)))


def _utc(local: datetime) -> datetime:
    return local.astimezone(UTC).replace(tzinfo=None)


def _weekly(name: str, start: str, end: str, lo: datetime, hi: datetime, zone: ZoneInfo):
    s_day, s_time = _parse_when(start)
    e_day, e_time = _parse_when(end)
    length_days = (e_day - s_day) % 7
    first = lo.replace(tzinfo=UTC).astimezone(zone).date() - timedelta(days=7)
    day = first + timedelta(days=(s_day - first.weekday()) % 7)
    while True:
        s = datetime.combine(day, s_time, zone)
        e = datetime.combine(day + timedelta(days=length_days), e_time, zone)
        if e <= s:
            e += timedelta(days=7)
        if _utc(s) >= hi:
            return
        if _utc(e) > lo:
            yield QuietInterval(name, _utc(s), _utc(e))
        day += timedelta(days=7)


def _shabbat(cfg: Config, lo: datetime, hi: datetime, zone: ZoneInfo):
    from astral import Observer
    from astral.sun import dusk, sunset

    if cfg.latitude is None or cfg.longitude is None:
        raise ConfigError("shabbat = true needs [location] latitude and longitude in config")
    where = Observer(latitude=cfg.latitude, longitude=cfg.longitude)
    first: date = lo.replace(tzinfo=UTC).astimezone(zone).date() - timedelta(days=7)
    friday = first + timedelta(days=(4 - first.weekday()) % 7)
    while True:
        try:
            s = sunset(where, friday, tzinfo=zone) - CANDLE_LIGHTING
            e = dusk(where, friday + timedelta(days=1), NIGHTFALL_DEPRESSION, tzinfo=zone)
        except ValueError:  # no sunset (polar latitudes)
            friday += timedelta(days=7)
            continue
        if _utc(s) >= hi:
            return
        if _utc(e) > lo:
            yield QuietInterval("shabbat", _utc(s), _utc(e))
        friday += timedelta(days=7)


def quiet_intervals(cfg: Config, lo: datetime, hi: datetime) -> list[QuietInterval]:
    zone = ZoneInfo(cfg.tz())
    out: list[QuietInterval] = []
    for i, w in enumerate(cfg.quiet):
        try:
            out += _weekly(w.get("name", f"quiet-{i + 1}"), w["start"], w["end"], lo, hi, zone)
        except KeyError:
            raise ConfigError("Each [[quiet]] window needs start and end") from None
    if cfg.shabbat:
        out += _shabbat(cfg, lo, hi, zone)
    return sorted(out, key=lambda q: q.start)
