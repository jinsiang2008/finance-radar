import unittest
from datetime import datetime
from research.podcast_poc import schedule_policy as policy


def dt(value):
    return datetime.fromisoformat(value + "+08:00")


class ScheduleTests(unittest.TestCase):
    def test_all_boundaries_use_beijing_time(self):
        for value, expected in [("08:59", "off_peak"), ("09:00", "peak"), ("11:59", "peak"),
                                ("12:00", "off_peak"), ("13:59", "off_peak"), ("14:00", "peak"),
                                ("17:59", "peak"), ("18:00", "off_peak")]:
            self.assertEqual(policy.period_at(dt("2026-10-08T" + value)), expected)
        self.assertEqual(policy.period_at(datetime.fromisoformat("2026-10-08T01:00+00:00")), "peak")

    def test_holidays_and_makeup_weekends_are_off_peak(self):
        for date in ("2026-10-01", "2026-10-07", "2026-10-10", "2026-02-20", "2026-05-05"):
            self.assertEqual(policy.period_at(dt(date + "T10:00")), "off_peak")

    def test_unknown_year_is_explicit_and_conservative(self):
        result = policy.snapshot(at=dt("2027-01-01T10:00"))
        self.assertEqual(result["calendar_status"], "unknown_year")
        self.assertEqual(result["period"], "peak")

    def test_noon_transition_does_not_wait_four_hours(self):
        result = policy.snapshot(dt("2026-10-08T10:00").isoformat(), at=dt("2026-10-08T11:00"))
        self.assertFalse(result["due"])
        self.assertEqual(datetime.fromisoformat(result["next_check_at"]).astimezone(policy.BEIJING), dt("2026-10-08T12:00"))
        self.assertTrue(policy.snapshot(dt("2026-10-08T10:00").isoformat(), at=dt("2026-10-08T12:00"))["due"])

    def test_peak_transition_and_sleep_resume(self):
        self.assertFalse(policy.snapshot(dt("2026-10-08T08:00").isoformat(), at=dt("2026-10-08T09:00"))["due"])
        self.assertTrue(policy.snapshot(dt("2026-10-07T18:00").isoformat(), at=dt("2026-10-08T09:00"))["due"])
        with self.assertRaises(ValueError):
            policy.period_at(datetime(2026, 10, 8))
