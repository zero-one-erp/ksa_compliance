"""The ZATCA sending rules: which mode sends what, and when. Plain unit tests, no site or database needed."""

import ast
import csv
import datetime
import json
import os
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

    def _runs_over_days(self, at, days=3):
        """The runs a dispatcher checking every 5 minutes starts over `days` days, the first run already made."""
        step = datetime.timedelta(minutes=5)
        now, end = D(2026, 11, 25, 0, 0), D(2026, 11, 25, 0, 0) + datetime.timedelta(days=days)
        last, runs = D(2026, 11, 24, 23, 59), []
        while now <= end:
            if rules.is_scheduled_run_due('At a set time', now, at, last):
                last = now
                runs.append(now)
            now += step
        return runs

    def test_a_time_after_the_last_check_of_the_day_still_runs_once_a_day(self):
        # Checks fall at 23:50, 23:55 and 00:00: a set time after 23:55 is caught at 00:00 the next day
        for minute in (50, 56, 57, 59):
            runs = self._runs_over_days(datetime.time(23, minute))
            self.assertEqual(len(runs), 3, (minute, runs))
        self.assertEqual(
            self._runs_over_days(datetime.time(23, 57)),
            [D(2026, 11, 26, 0, 0), D(2026, 11, 27, 0, 0), D(2026, 11, 28, 0, 0)],
        )

    def test_a_late_time_is_due_at_the_first_check_after_it(self):
        due = rules.is_scheduled_run_due
        at = datetime.time(23, 57)
        ran = D(2026, 11, 27, 0, 0)  # the run for 26 Nov 23:57
        self.assertFalse(due('At a set time', D(2026, 11, 27, 23, 55), at, ran))
        self.assertTrue(due('At a set time', D(2026, 11, 28, 0, 0), at, ran))
        # never ran: the first check after the time starts it
        self.assertFalse(due('At a set time', D(2026, 11, 27, 23, 55), at, None))
        self.assertTrue(due('At a set time', D(2026, 11, 28, 0, 0), at, None))

    def test_a_missed_run_is_made_up_once(self):
        due = rules.is_scheduled_run_due
        # the dispatcher was down at 21:30 yesterday and today: it runs at the next check, once
        self.assertTrue(due('At a set time', D(2026, 11, 27, 9, 0), self.T, D(2026, 11, 25, 21, 30)))
        self.assertFalse(due('At a set time', D(2026, 11, 27, 9, 5), self.T, D(2026, 11, 27, 9, 0)))
        self.assertTrue(due('At a set time', D(2026, 11, 27, 21, 30), self.T, D(2026, 11, 27, 9, 0)))

    def test_latest_occurrence(self):
        at = datetime.time(23, 59)
        self.assertEqual(rules.latest_occurrence(D(2026, 11, 28, 0, 0), at), D(2026, 11, 27, 23, 59))
        self.assertEqual(rules.latest_occurrence(D(2026, 11, 27, 23, 59), at), D(2026, 11, 27, 23, 59))

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

    def test_under_a_scheduled_mode_the_alert_comes_after_the_safety_net(self):
        for mode in rules.SCHEDULED_MODES:
            self.assertEqual(rules.alert_after_hours(mode), 22, mode)
            self.assertGreater(rules.alert_after_hours(mode), rules.ALERT_AFTER_HOURS, mode)
            # the hourly job's safety net still sends at 20 h
            issued = self.NOW - datetime.timedelta(hours=20)
            self.assertTrue(
                rules.hourly_job_sends(mode, False, 'Ready For Batch', rules.is_near_reporting_limit(issued, self.NOW))
            )
            self.assertFalse(rules.is_near_reporting_limit(issued, self.NOW, rules.alert_after_hours(mode)))
        for mode in ('Live', 'Batches', None):
            self.assertEqual(rules.alert_after_hours(mode), 20, mode)

    def test_hours_left(self):
        self.assertEqual(rules.hours_left(D(2026, 11, 26, 16, 0), self.NOW), 4.0)
        self.assertEqual(rules.hours_left(D(2026, 11, 26, 10, 0), self.NOW), -2.0)


class TestSendOutcome(unittest.TestCase):
    """A send's outcome follows the status it stored, not the Ok the submit path answers for every ZATCA reply."""

    def test_only_accepted_statuses_count_as_sent(self):
        for status in rules.SENT_STATUSES:
            self.assertEqual(rules.send_outcome(status), 'sent', status)

    def test_final_refusals_are_failed(self):
        for status in ('Rejected', 'Duplicate', 'Clearance switched off'):
            self.assertEqual(rules.send_outcome(status), 'failed', status)

    def test_still_pending_is_retrying(self):
        for status in (*rules.PENDING_STATUSES, '', None):
            self.assertEqual(rules.send_outcome(status), 'retrying', status)


class TestTransactionFromXml(unittest.TestCase):
    """Device-computed invoices carry their type only in the signed XML (BR-KSA-06, InvoiceTypeCode/@name)."""

    def test_reads_the_name_of_invoice_type_code(self):
        xml = '<Invoice><cbc:ID>1</cbc:ID><cbc:InvoiceTypeCode name="0100000">388</cbc:InvoiceTypeCode></Invoice>'
        self.assertEqual(rules.transaction_from_xml(xml), '0100000')
        self.assertTrue(rules.is_standard_transaction(rules.transaction_from_xml(xml)))
        single = "<InvoiceTypeCode listID='x' name='0200100'>381</InvoiceTypeCode>"
        self.assertEqual(rules.transaction_from_xml(single), '0200100')

    def test_none_without_a_type(self):
        for xml in (None, '', '<Invoice/>', '<cbc:InvoiceTypeCode>388</cbc:InvoiceTypeCode>'):
            self.assertIsNone(rules.transaction_from_xml(xml), xml)


class TestArabic(unittest.TestCase):
    """Every message the sending code shows and every field label it added has an Arabic row in ar.csv."""

    HERE = os.path.dirname(os.path.abspath(__file__))
    NEW_FIELDS = {
        'zatca_business_settings': ('sync_with_zatca', 'sync_time', 'last_scheduled_sync'),
        'sales_invoice_additional_fields': ('deadline_alert_sent',),
    }

    def _arabic(self):
        with open(os.path.join(self.HERE, 'translations', 'ar.csv'), encoding='utf-8') as f:
            return {row[0]: row[1] for row in csv.reader(f) if len(row) >= 2}

    def _messages(self):
        with open(os.path.join(self.HERE, 'zatca_sending.py'), encoding='utf-8') as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == '_' and node.args:
                if isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    yield node.args[0].value

    def _field_texts(self):
        for doctype, fieldnames in self.NEW_FIELDS.items():
            path = os.path.join(self.HERE, 'ksa_compliance', 'doctype', doctype, f'{doctype}.json')
            with open(path, encoding='utf-8') as f:
                fields = {d['fieldname']: d for d in json.load(f)['fields']}
            for fieldname in fieldnames:
                field = fields[fieldname]
                if fieldname == 'sync_with_zatca':
                    # an older field: its label stays as it was; the new modes and the description are new
                    yield from ('At a set time', 'Every 24 hours', field['description'])
                    continue
                yield from (field[k] for k in ('label', 'description') if field.get(k))

    def test_every_sending_message_has_arabic(self):
        arabic = self._arabic()
        messages = sorted(set(self._messages()))
        self.assertIn('Invoice sent to ZATCA. Integration status: {0}', messages)  # send_now's answer
        missing = [m for m in messages if not arabic.get(m) or arabic[m] == m]
        self.assertEqual(missing, [])

    def test_every_new_field_has_arabic(self):
        arabic = self._arabic()
        missing = [t for t in self._field_texts() if not arabic.get(t) or arabic[t] == t]
        self.assertEqual(missing, [])

    def test_placeholders_survive_the_translation(self):
        arabic = self._arabic()
        for message in set(self._messages()):
            for placeholder in ('{0}', '{1}'):
                self.assertEqual(placeholder in message, placeholder in arabic.get(message, ''), message)


if __name__ == '__main__':
    unittest.main()
