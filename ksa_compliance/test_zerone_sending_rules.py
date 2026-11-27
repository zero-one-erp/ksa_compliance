"""The ZATCA sending rules: which mode sends what, and when. Plain unit tests, no site or database needed."""

import datetime
import unittest

from ksa_compliance import zatca_sending_rules as rules

D = datetime.datetime


class TestSendingModes(unittest.TestCase):
    def test_legacy_and_unknown_values_mean_hourly_batches(self):
        for value in ('Batches', 'batches', '', None, 'something else'):
            self.assertEqual(rules.normalize_mode(value), rules.MODE_BATCHES, value)
        self.assertEqual(rules.normalize_mode('live'), rules.MODE_LIVE)
        self.assertEqual(rules.normalize_mode('At a set time'), rules.MODE_AT_SET_TIME)
        self.assertEqual(rules.normalize_mode('Every 24 hours'), rules.MODE_EVERY_24_HOURS)

    def test_the_invoice_type_transaction_tells_standard_from_simplified(self):
        self.assertTrue(rules.is_standard_transaction('0100000'))
        self.assertTrue(rules.is_standard_transaction('0100100'))  # export
        self.assertFalse(rules.is_standard_transaction('0200000'))
        self.assertFalse(rules.is_standard_transaction(None))  # precomputed: follows its EGS

    def test_a_standard_invoice_is_sent_on_submit_in_every_mode(self):
        for mode in rules.SENDING_MODES:
            self.assertTrue(rules.send_on_submit(mode, is_standard=True), mode)

    def test_a_simplified_invoice_is_sent_on_submit_only_when_live(self):
        self.assertTrue(rules.send_on_submit('Live', is_standard=False))
        for mode in ('Batches', 'At a set time', 'Every 24 hours', None):
            self.assertFalse(rules.send_on_submit(mode, is_standard=False), mode)

    def test_the_hourly_job_keeps_sending_what_it_sent_before(self):
        for mode in ('Live', 'Batches', None):
            for status in rules.PENDING_STATUSES:
                self.assertTrue(rules.hourly_job_sends(mode, False, status), (mode, status))
                self.assertTrue(rules.hourly_job_sends(mode, True, status), (mode, status))

    def test_under_a_scheduled_mode_the_hourly_job_leaves_new_simplified_invoices_to_the_schedule(self):
        for mode in rules.SCHEDULED_MODES:
            self.assertFalse(rules.hourly_job_sends(mode, False, 'Ready For Batch'), mode)
            # retries of attempted ones go on hourly
            self.assertTrue(rules.hourly_job_sends(mode, False, 'Resend'), mode)
            self.assertTrue(rules.hourly_job_sends(mode, False, 'Corrected'), mode)
            # standard invoices never wait
            self.assertTrue(rules.hourly_job_sends(mode, True, 'Ready For Batch'), mode)
            # and nothing waits past the alert threshold
            self.assertTrue(rules.hourly_job_sends(mode, False, 'Ready For Batch', near_reporting_limit=True), mode)


class TestScheduledRuns(unittest.TestCase):
    T = datetime.time(21, 30)

    def test_at_a_set_time_runs_once_a_day_after_the_time(self):
        due = rules.is_scheduled_run_due
        self.assertFalse(due('At a set time', D(2026, 11, 27, 21, 29), self.T, D(2026, 11, 26, 21, 30)))
        self.assertTrue(due('At a set time', D(2026, 11, 27, 21, 30), self.T, D(2026, 11, 26, 21, 30)))
        self.assertTrue(due('At a set time', D(2026, 11, 27, 23, 0), self.T, D(2026, 11, 26, 21, 35)))
        # already ran today
        self.assertFalse(due('At a set time', D(2026, 11, 27, 21, 40), self.T, D(2026, 11, 27, 21, 35)))
        # never ran: runs once the time has come
        self.assertTrue(due('At a set time', D(2026, 11, 27, 22, 0), self.T, None))
        self.assertFalse(due('At a set time', D(2026, 11, 27, 8, 0), self.T, None))
        # no time set: never
        self.assertFalse(due('At a set time', D(2026, 11, 27, 22, 0), None, None))

    def test_every_24_hours_runs_a_day_after_the_previous_run(self):
        due = rules.is_scheduled_run_due
        last = D(2026, 11, 26, 10, 0)
        self.assertFalse(due('Every 24 hours', D(2026, 11, 27, 9, 59), None, last))
        self.assertTrue(due('Every 24 hours', D(2026, 11, 27, 10, 0), None, last))
        self.assertTrue(due('Every 24 hours', D(2026, 11, 27, 10, 0), None, None))

    def test_live_and_batches_have_no_scheduled_run(self):
        for mode in ('Live', 'Batches', None):
            self.assertFalse(rules.is_scheduled_run_due(mode, D(2026, 11, 27, 23, 0), self.T, None), mode)


class TestStatusGroups(unittest.TestCase):
    def test_groups(self):
        self.assertEqual(rules.status_group('Accepted'), 'sent')
        self.assertEqual(rules.status_group('Accepted with warnings'), 'sent')
        for status in ('Ready For Batch', 'Resend', 'Corrected', '', None):
            self.assertEqual(rules.status_group(status), 'not_sent', status)
        self.assertEqual(rules.status_group(None, has_additional_fields=False), 'not_sent')
        for status in ('Rejected', 'Duplicate', 'Clearance switched off'):
            self.assertEqual(rules.status_group(status), 'failed', status)

    def test_statuses_for_group_cover_every_status(self):
        every = set(rules.PENDING_STATUSES + rules.SENT_STATUSES + rules.FAILED_STATUSES)
        for status in every:
            self.assertIn(status, rules.statuses_for_group(rules.status_group(status)))
        with self.assertRaises(ValueError):
            rules.statuses_for_group('bogus')


class TestReportingLimitAlert(unittest.TestCase):
    NOW = D(2026, 11, 27, 12, 0)

    def test_alert_threshold_is_20_hours_after_issue(self):
        near = rules.is_near_reporting_limit
        self.assertFalse(near(D(2026, 11, 26, 16, 1), self.NOW))  # 19h59m
        self.assertTrue(near(D(2026, 11, 26, 16, 0), self.NOW))  # exactly 20h
        self.assertTrue(near(D(2026, 11, 25, 12, 0), self.NOW))  # past the limit
        self.assertFalse(near(None, self.NOW))
        self.assertEqual(rules.alert_cutoff(self.NOW), D(2026, 11, 26, 16, 0))

    def test_hours_left(self):
        self.assertEqual(rules.hours_left(D(2026, 11, 26, 16, 0), self.NOW), 4.0)
        self.assertEqual(rules.hours_left(D(2026, 11, 26, 10, 0), self.NOW), -2.0)


if __name__ == '__main__':
    unittest.main()
