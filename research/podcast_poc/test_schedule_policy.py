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

    def test_off_peak_only_never_starts_overdue_or_manual_work_in_peak(self):
        for at, boundary in (("2026-10-13T09:00", "2026-10-13T12:00"),
                             ("2026-10-13T14:00", "2026-10-13T18:00")):
            result = policy.snapshot("2026-10-10T00:00:00+08:00", at=dt(at), off_peak_only=True)
            self.assertFalse(result["due"])
            self.assertFalse(result["execution_allowed"])
            self.assertEqual(result["manual_execution_policy"], "off_peak_only")
            self.assertEqual(result["deferred_reason"], "off_peak_only")
            self.assertEqual(datetime.fromisoformat(result["next_check_at"]), dt(boundary))
            self.assertEqual(datetime.fromisoformat(result["next_run_at"]), dt(boundary))

    def test_first_run_in_peak_waits_and_weekends_holidays_remain_eligible(self):
        result = policy.snapshot(at=dt("2026-10-13T10:17"), off_peak_only=True)
        self.assertFalse(result["due"])
        self.assertEqual(datetime.fromisoformat(result["next_run_at"]), dt("2026-10-13T12:00"))
        for stamp in ("2026-10-13T12:00", "2026-10-13T18:00", "2026-10-10T10:00", "2026-10-01T10:00"):
            self.assertTrue(policy.snapshot(at=dt(stamp), off_peak_only=True)["due"])

    def test_paid_call_guard_exposes_next_window_without_provider_error(self):
        policy.require_off_peak(at=dt("2026-10-13T08:59"))
        with self.assertRaises(policy.OffPeakDeferred) as caught:
            policy.require_off_peak(at=dt("2026-10-13T09:00"))
        self.assertEqual(datetime.fromisoformat(caught.exception.not_before), dt("2026-10-13T12:00"))


    def test_explicit_summary_permission_is_live_and_does_not_relax_fetch_guard(self):
        selected = [False]
        peak = dt("2026-10-14T10:00")
        with policy.summary_request_context(lambda: selected[0]):
            with self.assertRaises(policy.OffPeakDeferred):
                policy.require_summary_window(at=peak)
            selected[0] = True
            policy.require_summary_window(at=peak)
            with self.assertRaises(policy.OffPeakDeferred):
                policy.require_off_peak(at=peak)
        self.assertFalse(policy.summary_context_active())
        with self.assertRaises(policy.OffPeakDeferred):
            policy.require_summary_window(at=peak)

    def test_summary_permission_does_not_leak_after_failure_or_to_other_threads(self):
        from concurrent.futures import ThreadPoolExecutor
        with self.assertRaises(RuntimeError):
            with policy.summary_request_context(lambda: True):
                self.assertTrue(policy.summary_is_on_demand())
                with ThreadPoolExecutor(max_workers=1) as pool:
                    self.assertFalse(pool.submit(policy.summary_is_on_demand).result())
                raise RuntimeError("simulated job failure")
        self.assertFalse(policy.summary_is_on_demand())

    def test_summary_permission_requires_true_not_truthy_data(self):
        for value in (1, "true", {}, None):
            with policy.summary_request_context(lambda: value):
                self.assertFalse(policy.summary_is_on_demand())
