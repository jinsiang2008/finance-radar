"""Beijing peak/off-peak cadence; calendar data is explicit and auditable."""
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


def snapshot(last_check_at=None, *, at=None, peak_minutes=240, off_peak_minutes=60):
    at = at or datetime.now(timezone.utc)
    if not 30 <= off_peak_minutes <= peak_minutes <= 1440:
        raise ValueError("Invalid reading cadence")
    period = period_at(at)
    interval = peak_minutes if period == "peak" else off_peak_minutes
    last = datetime.fromisoformat(last_check_at) if last_check_at else None
    if last and last.tzinfo is None:
        raise ValueError("Stored check time requires a timezone")
    due = last is None or (at - last).total_seconds() >= interval * 60
    next_check = at
    if not due:
        # Re-evaluate at boundaries: at noon a four-hour peak wait may become
        # immediately eligible. Do not blindly add four hours to the last check.
        next_check = at.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(1441):
            cadence = peak_minutes if period_at(next_check) == "peak" else off_peak_minutes
            if (next_check - last).total_seconds() >= cadence * 60:
                break
            next_check += timedelta(minutes=1)
    return {"timezone": "Asia/Shanghai", "period": period, "peak_minutes": peak_minutes,
            "off_peak_minutes": off_peak_minutes, "due": due,
            "next_check_at": next_check.isoformat(),
            "calendar_status": "verified" if at.astimezone(BEIJING).year in HOLIDAYS else "unknown_year",
            "calendar_years": sorted(HOLIDAYS), "price_source": PRICE_SOURCE,
            "calendar_source": CALENDAR_SOURCE}
