from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dateutil import parser as dt_parser

from ..models import DateRange


def month_range(month: str, tz: str = "UTC") -> DateRange:
    """Convert a YYYY-MM string into an inclusive DateRange in the given tz."""
    parts = month.split("-")
    if len(parts) != 2:
        raise ValueError(f"Expected YYYY-MM, got {month!r}")
    year, mon = int(parts[0]), int(parts[1])
    start = date(year, mon, 1)
    if mon == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, mon + 1, 1) - timedelta(days=1)
    return DateRange(start=start, end=end)


def parse_iso(ts: str, default_tz: str = "UTC") -> datetime:
    """Parse an ISO timestamp; if naive, attach default_tz."""
    dt = dt_parser.isoparse(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(default_tz))
    return dt


def to_local(dt: datetime, tz: str) -> datetime:
    return dt.astimezone(ZoneInfo(tz))


def local_date(dt: datetime, tz: str) -> date:
    return to_local(dt, tz).date()
