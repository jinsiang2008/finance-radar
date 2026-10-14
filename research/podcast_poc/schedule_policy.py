"""Beijing peak/off-peak cadence; calendar data is explicit and auditable."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
PRICE_SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
CALENDAR_SOURCE = "https://zwfw.gansu.gov.cn/chengguan/zwzx/zxtg/art/2025/art_2529408225b9478487a7bd156ff8c37b.html"
# State Council 2026 holiday notice. Weekend make-up workdays remain off-peak
# because the provider explicitly prices *all weekends* as off-peak.
HOLIDAYS = {2026: [("01-01", "01-03"), ("02-15", "02-23"), ("04-04", "04-06"),
                   ("05-01", "05-05"), ("06-19", "06-21"), ("09-25", "09-27"),
                   ("10-01", "10-07")]}


def period_at(at):
    if at.tzinfo is None:
        raise ValueError("Schedule requires a timezone-aware datetime")
    local = at.astimezone(BEIJING)
    holiday = any(start <= local.strftime("%m-%d") <= end
                  for start, end in HOLIDAYS.get(local.year, []))
    minute = local.hour * 60 + local.minute
    peak = local.weekday() < 5 and not holiday and (540 <= minute < 720 or 840 <= minute < 1080)
    return "peak" if peak else "off_peak"


def next_off_peak(at):
    """Return the current instant or next Beijing off-peak boundary."""
    if period_at(at) == "off_peak":
        return at
    current = at.astimezone(BEIJING)
    hour = 12 if current.hour < 12 else 18
    return current.replace(hour=hour, minute=0, second=0, microsecond=0).astimezone(at.tzinfo)


class OffPeakDeferred(RuntimeError):
    """A scheduling pause, never a provider failure or billable request."""
    code = "off_peak_deferred"

    def __init__(self, at=None):
        moment = at or datetime.now(timezone.utc)
        self.not_before = next_off_peak(moment).astimezone(timezone.utc).isoformat()
        super().__init__("Non-urgent reading waits for the next off-peak window")


def require_off_peak(*, at=None):
    moment = at or datetime.now(timezone.utc)
    if period_at(moment) != "off_peak":
        raise OffPeakDeferred(moment)


# The executor installs a job-specific database predicate, never a global flag.
# ContextVar keeps concurrent request handlers and later jobs from inheriting it.
_SUMMARY_PERMISSION = ContextVar("reading_summary_permission", default=None)


@contextmanager
def summary_request_context(allow_immediate):
    if not callable(allow_immediate):
        raise TypeError("A persisted summary permission predicate is required")
    token = _SUMMARY_PERMISSION.set(allow_immediate)
    try:
        yield
    finally:
        _SUMMARY_PERMISSION.reset(token)


def summary_context_active():
    return _SUMMARY_PERMISSION.get() is not None


def summary_is_on_demand():
    predicate = _SUMMARY_PERMISSION.get()
    return predicate is not None and predicate() is True


def require_summary_window(*, at=None):
    """Check each paid request; only its current explicitly selected job bypasses."""
    if not summary_is_on_demand():
        require_off_peak(at=at)


def snapshot(last_check_at=None, *, at=None, peak_minutes=240, off_peak_minutes=60, off_peak_only=False):
    at = at or datetime.now(timezone.utc)
    if not 30 <= off_peak_minutes <= peak_minutes <= 1440:
        raise ValueError("Invalid reading cadence")
    period = period_at(at)
    interval = peak_minutes if period == "peak" else off_peak_minutes
    last = datetime.fromisoformat(last_check_at) if last_check_at else None
    if last and last.tzinfo is None:
        raise ValueError("Stored check time requires a timezone")
    execution_allowed = not off_peak_only or period == "off_peak"
    due = execution_allowed and (last is None or (at - last).total_seconds() >= interval * 60)
    next_check = at
    if not due:
        # Re-evaluate at boundaries: at noon a four-hour peak wait may become
        # immediately eligible. Do not blindly add four hours to the last check.
        next_check = at.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(1441):
            candidate_period = period_at(next_check)
            cadence = peak_minutes if candidate_period == "peak" else off_peak_minutes
            allowed = not off_peak_only or candidate_period == "off_peak"
            if allowed and (last is None or (next_check - last).total_seconds() >= cadence * 60):
                break
            next_check += timedelta(minutes=1)
    return {"timezone": "Asia/Shanghai", "period": period, "peak_minutes": peak_minutes,
            "off_peak_minutes": off_peak_minutes, "due": due,
            "next_check_at": next_check.isoformat(),
            "off_peak_only": bool(off_peak_only), "execution_allowed": execution_allowed,
            "next_run_at": (next_off_peak(at) if off_peak_only else at).isoformat(),
            "deferred_reason": "off_peak_only" if not execution_allowed else None,
            "manual_execution_policy": "off_peak_only" if off_peak_only else "immediate",
            "calendar_status": "verified" if at.astimezone(BEIJING).year in HOLIDAYS else "unknown_year",
            "calendar_years": sorted(HOLIDAYS), "price_source": PRICE_SOURCE,
            "calendar_source": CALENDAR_SOURCE}
