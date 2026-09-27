"""Time helpers — the business runs on Europe/Warsaw time."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo("Europe/Warsaw")

WEEKDAYS_PL = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]
MONTHS_PL = [
    "stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca",
    "lipca", "sierpnia", "września", "października", "listopada", "grudnia",
]


def now() -> datetime:
    return datetime.now(WARSAW)


def today() -> date:
    return now().date()


def day_start(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=WARSAW)


def day_range(d: date) -> tuple[datetime, datetime]:
    start = day_start(d)
    return start, start + timedelta(days=1)


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def to_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=WARSAW)
    return dt.astimezone(WARSAW)


def fmt_date_pl(d: date) -> str:
    """'środa, 23 września 2026'"""
    return f"{WEEKDAYS_PL[d.weekday()]}, {d.day} {MONTHS_PL[d.month - 1]} {d.year}"


def fmt_short(dt: datetime | None) -> str:
    dt = to_local(dt)
    return dt.strftime("%d.%m %H:%M") if dt else ""


def ago_pl(dt: datetime | None) -> str:
    """Relative time in Polish: 'teraz', '5 min temu', '3 godz. temu', 'wczoraj', '4 dni temu'."""
    dt = to_local(dt)
    if dt is None:
        return "—"
    delta = now() - dt
    secs = int(delta.total_seconds())
    if secs < 0:
        return dt.strftime("%d.%m %H:%M")
    if secs < 60:
        return "teraz"
    if secs < 3600:
        return f"{secs // 60} min temu"
    if secs < 86400 and dt.date() == today():
        return f"{secs // 3600} godz. temu"
    days = (today() - dt.date()).days
    if days == 1:
        return f"wczoraj {dt.strftime('%H:%M')}"
    if days < 7:
        return f"{days} dni temu"
    return dt.strftime("%d.%m.%Y")


def hours_since(dt: datetime | None) -> float:
    dt = to_local(dt)
    if dt is None:
        return 0.0
    return max(0.0, (now() - dt).total_seconds() / 3600)


def _easter(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    return date(year, month, (h + l_ - 7 * m + 114) % 31 + 1)


def pl_holidays(year: int) -> set[date]:
    """Polish statutory days off (Christmas Eve from 2025) — same rule as MyBed OS (src/lib/dailyDates.js)."""
    fixed = [(1, 1), (1, 6), (5, 1), (5, 3), (8, 15), (11, 1), (11, 11), (12, 25), (12, 26)]
    if year >= 2025:
        fixed.append((12, 24))
    easter = _easter(year)
    return {date(year, m, d) for m, d in fixed} | {easter + timedelta(days=n) for n in (0, 1, 49, 60)}


def is_workday(d: date) -> bool:
    return d.weekday() < 5 and d not in pl_holidays(d.year)


def previous_workday(d: date) -> date:
    d -= timedelta(days=1)
    while not is_workday(d):
        d -= timedelta(days=1)
    return d


def next_workday(d: date) -> date:
    d += timedelta(days=1)
    while not is_workday(d):
        d += timedelta(days=1)
    return d
