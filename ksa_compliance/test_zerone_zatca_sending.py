"""ZATCA sending on a site: standard invoices go live under batches, the scheduled modes, «send now» (permissions,
final statuses, no double send), the status list's filters and the 24 hour alert. ZATCA itself is never called:
`submit_to_zatca` is mocked, and so is the commit the send path makes, so every test rolls back.

Outside the doctype folders on purpose: a FrappeTestCase there makes the runner preload ERPNext's test masters,
which assume a USD site and fail on a SAR one."""

import datetime
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime
from result import Ok

from ksa_compliance import zatca_sending
from ksa_compliance.ksa_compliance.doctype.sales_invoice_additional_fields.sales_invoice_additional_fields import (
    SalesInvoiceAdditionalFields,
)
from ksa_compliance.ksa_compliance.doctype.sales_invoice_additional_fields import (
    sales_invoice_additional_fields as siaf_module,
)
from ksa_compliance.invoice import InvoiceMode
from ksa_compliance.standard_doctypes import sales_invoice as si_hooks
from ksa_compliance.standard_doctypes.payment_entry import payment_entry as pe_hooks

SIAF = 'Sales Invoice Additional Fields'
COMPANY = '_ZS Company'
CUSTOMER = '_ZS Customer'


def _invoice(name, posted_at: datetime.datetime, customer=CUSTOMER, company=COMPANY):
    doc = frappe.new_doc('Sales Invoice')
    doc.name, doc.company, doc.customer, doc.customer_name = name, company, customer, customer
    doc.posting_date, doc.posting_time = posted_at.date(), posted_at.time()
    doc.docstatus, doc.grand_total, doc.currency = 1, 115, 'SAR'
    doc.db_insert()


def _prepayment(name, posted_at: datetime.datetime, customer=CUSTOMER, company=COMPANY, prepayment=1):
    doc = frappe.new_doc('Payment Entry')
    doc.name, doc.company, doc.payment_type, doc.party_type = name, company, 'Receive', 'Customer'
    doc.party, doc.party_name, doc.posting_date = customer, customer, posted_at.date()
    doc.docstatus, doc.paid_amount, doc.received_amount, doc.paid_from_account_currency = 1, 115, 115, 'SAR'
    doc.custom_prepayment_invoice = prepayment
    doc.db_insert()


def _siaf(name, invoice, status, docstatus=0, transaction='0200000', latest=1, doctype='Sales Invoice'):
    doc = frappe.new_doc(SIAF)
    doc.name, doc.sales_invoice, doc.invoice_doctype = name, invoice, doctype
    doc.integration_status, doc.docstatus, doc.is_latest, doc.invoice_type_transaction = (
        status,
        docstatus,
        latest,
        transaction,
    )
    doc.db_insert()


class _SendingTestCase(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        frappe.db.savepoint('zerone_sending')
        # The send path commits its outcome; in tests that would defeat the rollback
        for target in ('commit', 'rollback'):
            patcher = mock.patch.object(frappe.db, target)
            patcher.start()
            self.addCleanup(patcher.stop)
        companies = mock.patch.object(zatca_sending, '_readable_zatca_companies', return_value=[COMPANY])
        companies.start()
        self.addCleanup(companies.stop)

    def tearDown(self):
        frappe.db.sql('rollback to savepoint zerone_sending')
        frappe.local.message_log = []
        super().tearDown()


class TestStandardIsAlwaysLive(_SendingTestCase):
    def _submit(self, sync_with_zatca, transaction):
        invoice = frappe._dict(doctype='Sales Invoice', name='_ZS-SUBMIT', company=COMPANY, is_consolidated=0)
        settings = frappe._dict(enable_zatca_integration=1, sync_with_zatca=sync_with_zatca)
        siaf = mock.MagicMock(invoice_type_transaction=transaction)
        siaf.name = '_ZS-SIAF-SUBMIT'
        with (
            mock.patch.object(si_hooks, '_should_enable_zatca_for_invoice', return_value=True),
            mock.patch.object(si_hooks.ZATCABusinessSettings, 'for_invoice', return_value=settings),
            mock.patch.object(si_hooks.SalesInvoiceAdditionalFields, 'create_for_invoice', return_value=siaf),
            mock.patch.object(si_hooks.ZATCAPrecomputedInvoice, 'for_invoice', return_value=None),
            mock.patch.object(si_hooks.frappe.utils.background_jobs, 'enqueue') as enqueue,
        ):
            si_hooks.create_sales_invoice_additional_fields_doctype(invoice, 'on_submit')
        siaf.insert.assert_called_once()
        return enqueue

    def test_a_standard_invoice_is_cleared_at_once_under_batches(self):
        enqueue = self._submit('Batches', '0100000')
        enqueue.assert_called_once_with(
            'ksa_compliance.zatca_sending.send_pending_job', siaf_name='_ZS-SIAF-SUBMIT', enqueue_after_commit=True
        )

    def test_a_standard_invoice_is_cleared_at_once_under_the_scheduled_modes(self):
        for mode in ('At a set time', 'Every 24 hours'):
            self.assertTrue(self._submit(mode, '0100000').called, mode)

    def test_a_simplified_invoice_waits_unless_live(self):
        self.assertFalse(self._submit('Batches', '0200000').called)
        self.assertFalse(self._submit('At a set time', '0200000').called)
        self.assertFalse(self._submit('Every 24 hours', '0200000').called)
        self.assertTrue(self._submit('Live', '0200000').called)


class TestModeSelection(_SendingTestCase):
    def _resolver(self, mode, company=COMPANY, near=False):
        resolver = mock.MagicMock()
        resolver.mode.return_value = mode
        resolver.company.return_value = company
        resolver.near_reporting_limit.return_value = near
        return resolver

    def test_the_hourly_run_per_mode(self):
        new_simplified = frappe._dict(integration_status='Ready For Batch', invoice_type_transaction='0200000')
        for mode, expected in (('Live', True), ('Batches', True), ('At a set time', False), ('Every 24 hours', False)):
            self.assertEqual(
                zatca_sending.should_send_in_run(new_simplified, self._resolver(mode), None), expected, mode
            )
        # near the reporting limit it goes anyway
        self.assertTrue(
            zatca_sending.should_send_in_run(new_simplified, self._resolver('Every 24 hours', near=True), None)
        )

    def test_a_scheduled_run_sends_only_its_companies(self):
        row = frappe._dict(integration_status='Ready For Batch', invoice_type_transaction='0200000')
        self.assertTrue(zatca_sending.should_send_in_run(row, self._resolver('At a set time'), [COMPANY]))
        self.assertFalse(zatca_sending.should_send_in_run(row, self._resolver('At a set time', 'Other'), [COMPANY]))
        device_row = frappe._dict(row, precomputed_invoice='_ZS-PRE')
        self.assertFalse(zatca_sending.should_send_in_run(device_row, self._resolver('At a set time'), [COMPANY]))

    def test_the_dispatcher_starts_one_job_per_due_company(self):
        now = now_datetime()
        settings = [
            frappe._dict(
                name='_ZS-S1',
                company='A',
                sync_with_zatca='Every 24 hours',
                sync_time=None,
                last_scheduled_sync=now - datetime.timedelta(hours=25),
            ),
            frappe._dict(
                name='_ZS-S2',
                company='B',
                sync_with_zatca='Every 24 hours',
                sync_time=None,
                last_scheduled_sync=now - datetime.timedelta(hours=1),
            ),
            frappe._dict(
                name='_ZS-S3',
                company='C',
                sync_with_zatca='At a set time',
                sync_time=datetime.timedelta(hours=0),
                last_scheduled_sync=None,
            ),
        ]
        with (
            mock.patch.object(zatca_sending.frappe, 'get_all', return_value=settings),
            mock.patch.object(zatca_sending.frappe.db, 'set_value') as set_value,
            mock.patch.object(zatca_sending.frappe, 'enqueue') as enqueue,
        ):
            zatca_sending.dispatch_scheduled_sending()
        self.assertEqual([c.kwargs['companies'] for c in enqueue.call_args_list], [['A'], ['C']])
        self.assertTrue(all(c.kwargs['deduplicate'] for c in enqueue.call_args_list))
        self.assertEqual([c.args[1] for c in set_value.call_args_list], ['_ZS-S1', '_ZS-S3'])


class TestSendNow(_SendingTestCase):
    def setUp(self):
        super().setUp()
        _invoice('_ZS-INV-1', now_datetime())
        _siaf('_ZS-SIAF-1', '_ZS-INV-1', 'Ready For Batch')

    def _accept(self, doc):
        doc.integration_status = 'Accepted'
        frappe.db.set_value(SIAF, doc.name, {'integration_status': 'Accepted', 'docstatus': 1})
        return Ok('Invoice sent to ZATCA. Integration status: Accepted')

    def test_sends_and_returns_the_new_status(self):
        with mock.patch.object(
            SalesInvoiceAdditionalFields, 'submit_to_zatca', autospec=True, side_effect=self._accept
        ) as submit:
            result = zatca_sending.send_now('_ZS-INV-1')
        submit.assert_called_once()
        self.assertEqual(result['integration_status'], 'Accepted')
        self.assertEqual(result['status_group'], 'sent')
        self.assertTrue(result['sent'])

    def test_a_second_send_is_refused_once_final(self):
        with mock.patch.object(
            SalesInvoiceAdditionalFields, 'submit_to_zatca', autospec=True, side_effect=self._accept
        ) as submit:
            zatca_sending.send_now('_ZS-INV-1')
            with self.assertRaises(frappe.ValidationError):
                zatca_sending.send_now('_ZS-INV-1')
        submit.assert_called_once()

    def test_refuses_final_statuses(self):
        for status in ('Accepted', 'Accepted with warnings', 'Rejected', 'Duplicate', 'Clearance switched off'):
            frappe.db.set_value(SIAF, '_ZS-SIAF-1', {'integration_status': status, 'docstatus': 1})
            with mock.patch.object(SalesInvoiceAdditionalFields, 'submit_to_zatca') as submit:
                with self.assertRaises(frappe.ValidationError, msg=status):
                    zatca_sending.send_now('_ZS-INV-1')
            submit.assert_not_called()

    def test_no_double_send_while_one_is_running(self):
        with zatca_sending._send_lock('_ZS-SIAF-1') as held:
            self.assertTrue(held)
            with mock.patch.object(SalesInvoiceAdditionalFields, 'submit_to_zatca') as submit:
                with self.assertRaises(zatca_sending.ZatcaSendInProgress):
                    zatca_sending.send_now('_ZS-INV-1')
                self.assertEqual(zatca_sending.send_pending('_ZS-SIAF-1')['skipped'], 'in_progress')
            submit.assert_not_called()
        # released afterwards
        with zatca_sending._send_lock('_ZS-SIAF-1') as held:
            self.assertTrue(held)

    def test_requires_read_and_submit_permission(self):
        for denied in ('read', 'submit'):

            def has_permission(doctype, ptype='read', doc=None, throw=False, **kwargs):
                if ptype == denied:
                    raise frappe.PermissionError
                return True

            with (
                mock.patch.object(zatca_sending.frappe, 'has_permission', side_effect=has_permission),
                mock.patch.object(SalesInvoiceAdditionalFields, 'submit_to_zatca') as submit,
            ):
                with self.assertRaises(frappe.PermissionError, msg=denied):
                    zatca_sending.send_now('_ZS-INV-1')
            submit.assert_not_called()

    def test_a_user_without_invoice_permissions_is_refused(self):
        frappe.set_user('Guest')
        try:
            with mock.patch.object(SalesInvoiceAdditionalFields, 'submit_to_zatca') as submit:
                with self.assertRaises(frappe.PermissionError):
                    zatca_sending.send_now('_ZS-INV-1')
            submit.assert_not_called()
        finally:
            frappe.set_user('Administrator')

    def _reply(self, status, docstatus):
        """A submit_to_zatca that stores what ZATCA answered and returns Ok, as the real one does for every reply."""

        def submit(doc):
            doc.integration_status = status
            frappe.db.set_value(SIAF, doc.name, {'integration_status': status, 'docstatus': docstatus})
            return Ok(f'Invoice sent to ZATCA. Integration status: {status}')

        return submit

    def test_the_answer_follows_the_stored_status(self):
        cases = (
            ('Accepted', 1, True, 'sent', 'sent'),
            ('Accepted with warnings', 1, True, 'sent', 'sent'),
            ('Rejected', 1, False, 'failed', 'failed'),
            ('Duplicate', 1, False, 'failed', 'failed'),
            ('Resend', 0, False, 'retrying', 'not_sent'),
        )
        for status, docstatus, sent, outcome, group in cases:
            frappe.db.set_value(SIAF, '_ZS-SIAF-1', {'integration_status': 'Ready For Batch', 'docstatus': 0})
            with mock.patch.object(
                SalesInvoiceAdditionalFields,
                'submit_to_zatca',
                autospec=True,
                side_effect=self._reply(status, docstatus),
            ):
                result = zatca_sending.send_now('_ZS-INV-1')
            self.assertEqual(
                (result['sent'], result['outcome'], result['status_group'], result['integration_status']),
                (sent, outcome, group, status),
                status,
            )
            self.assertEqual(frappe.db.get_value(SIAF, '_ZS-SIAF-1', 'integration_status'), status)

    def test_an_exception_leaves_it_pending_and_says_so(self):
        with mock.patch.object(
            SalesInvoiceAdditionalFields, 'submit_to_zatca', side_effect=RuntimeError('token=abc123 connection reset')
        ):
            result = zatca_sending.send_now('_ZS-INV-1')
        self.assertEqual((result['sent'], result['outcome'], result['status_group']), (False, 'retrying', 'not_sent'))
        self.assertNotIn('abc123', str(result))  # the exception text stays in the log
        self.assertEqual(
            frappe.db.get_value(SIAF, '_ZS-SIAF-1', ['integration_status', 'docstatus']), ('Ready For Batch', 0)
        )

    def test_the_document_is_read_for_update_inside_the_lock(self):
        real_get_doc = frappe.get_doc
        with (
            mock.patch.object(zatca_sending.frappe, 'get_doc', side_effect=real_get_doc) as get_doc,
            mock.patch.object(SalesInvoiceAdditionalFields, 'submit_to_zatca', autospec=True, side_effect=self._accept),
        ):
            zatca_sending.send_now('_ZS-INV-1')
        get_doc.assert_any_call(SIAF, '_ZS-SIAF-1', for_update=True)

    def test_bulk_queues_one_job_per_company_and_returns_at_once(self):
        _invoice('_ZS-INV-2', now_datetime())
        _siaf('_ZS-SIAF-2', '_ZS-INV-2', 'Ready For Batch')
        _invoice('_ZS-INV-B', now_datetime(), company='_ZS Company B')
        _siaf('_ZS-SIAF-B', '_ZS-INV-B', 'Ready For Batch')
        with (
            mock.patch('frappe.utils.background_jobs.enqueue', return_value=mock.MagicMock()) as enqueue,
            mock.patch.object(SalesInvoiceAdditionalFields, 'submit_to_zatca') as submit,
        ):
            queued = zatca_sending.send_now_bulk(frappe.as_json(['_ZS-INV-1', '_ZS-INV-B', '_ZS-INV-2', '_ZS-NONE']))
        submit.assert_not_called()  # nothing is sent inside the request
        calls = {c.kwargs['company']: c.kwargs for c in enqueue.call_args_list}
        self.assertEqual(len(enqueue.call_args_list), 2)
        self.assertEqual(calls[COMPANY]['invoices'], ['_ZS-INV-1', '_ZS-INV-2'])
        self.assertEqual(calls['_ZS Company B']['invoices'], ['_ZS-INV-B'])
        self.assertTrue(all(c['deduplicate'] for c in calls.values()))
        self.assertNotEqual(calls[COMPANY]['job_id'], calls['_ZS Company B']['job_id'])
        self.assertEqual([r['invoice'] for r in queued['refused']], ['_ZS-NONE'])

        # The job of one company runs; the other company's job is gone without running
        with mock.patch.object(
            SalesInvoiceAdditionalFields, 'submit_to_zatca', autospec=True, side_effect=self._accept
        ):
            zatca_sending.send_now_bulk_job(queued['bulk_id'], COMPANY, ['_ZS-INV-1', '_ZS-INV-2'], 'Sales Invoice')
        with mock.patch.object(zatca_sending, '_bulk_job_alive', return_value=True):
            running = zatca_sending.get_bulk_send_status(queued['bulk_id'])
        self.assertEqual((running['done'], running['pending'], running['sent']), (False, 1, 2))
        with mock.patch.object(zatca_sending, '_bulk_job_alive', return_value=False):
            status = zatca_sending.get_bulk_send_status(queued['bulk_id'])
        self.assertTrue(status['done'])
        self.assertEqual((status['sent'], status['failed'], status['retrying']), (2, 2, 0))

        # Another user cannot read it
        frappe.set_user('Guest')
        try:
            with self.assertRaises(frappe.DoesNotExistError):
                zatca_sending.get_bulk_send_status(queued['bulk_id'])
        finally:
            frappe.set_user('Administrator')

    def test_bulk_counts_an_invoice_another_sender_holds_as_retrying(self):
        with mock.patch('frappe.utils.background_jobs.enqueue', return_value=mock.MagicMock()):
            queued = zatca_sending.send_now_bulk(['_ZS-INV-1'])
        with zatca_sending._send_lock('_ZS-SIAF-1'):
            zatca_sending.send_now_bulk_job(queued['bulk_id'], COMPANY, ['_ZS-INV-1'], 'Sales Invoice')
        status = zatca_sending.get_bulk_send_status(queued['bulk_id'])
        self.assertEqual((status['done'], status['sent'], status['failed'], status['retrying']), (True, 0, 0, 1))

    def test_bulk_refuses_a_company_whose_bulk_send_is_running(self):
        with mock.patch('frappe.utils.background_jobs.enqueue', return_value=None):  # deduplicated: still running
            queued = zatca_sending.send_now_bulk(['_ZS-INV-1'])
        self.assertEqual(queued['jobs'], [])
        self.assertEqual([r['invoice'] for r in queued['refused']], ['_ZS-INV-1'])

    def test_bulk_is_capped(self):
        with self.assertRaises(frappe.ValidationError):
            zatca_sending.send_now_bulk([f'_ZS-{i}' for i in range(zatca_sending.BULK_LIMIT + 1)])


class TestPrepayments(_SendingTestCase):
    def _submit(self, sync_with_zatca, transaction):
        entry = frappe._dict(
            doctype='Payment Entry', name='_ZS-PE-SUBMIT', company=COMPANY, custom_prepayment_invoice=1
        )
        settings = frappe._dict(enable_zatca_integration=1, sync_with_zatca=sync_with_zatca)
        siaf = mock.MagicMock(invoice_type_transaction=transaction)
        siaf.name = '_ZS-SIAF-PE-SUBMIT'
        with (
            mock.patch.object(pe_hooks.ZATCABusinessSettings, 'for_invoice', return_value=settings),
            mock.patch.object(pe_hooks.SalesInvoiceAdditionalFields, 'create_for_invoice', return_value=siaf),
            mock.patch.object(pe_hooks.frappe.utils.background_jobs, 'enqueue') as enqueue,
        ):
            pe_hooks.create_prepayment_invoice_additional_fields_doctype(entry, 'on_submit')
        siaf.insert.assert_called_once()
        return enqueue

    def test_a_prepayment_goes_through_the_locked_path(self):
        enqueue = self._submit('Batches', '0100000')  # standard: cleared at once whatever the mode
        enqueue.assert_called_once_with(
            'ksa_compliance.zatca_sending.send_pending_job', siaf_name='_ZS-SIAF-PE-SUBMIT', enqueue_after_commit=True
        )
        self.assertFalse(self._submit('Every 24 hours', '0200000').called)
        self.assertTrue(self._submit('Live', '0200000').called)

    def test_the_prepayment_send_takes_the_lock(self):
        _prepayment('_ZS-PE-1', now_datetime())
        _siaf('_ZS-SIAF-PE-1', '_ZS-PE-1', 'Ready For Batch', doctype='Payment Entry')
        with zatca_sending._send_lock('_ZS-SIAF-PE-1') as held:
            self.assertTrue(held)
            with mock.patch.object(SalesInvoiceAdditionalFields, 'submit_to_zatca') as submit:
                zatca_sending.send_pending_job('_ZS-SIAF-PE-1')
                # a job enqueued before the upgrade, by the old function
                pe_hooks._submit_additional_fields(frappe.get_doc(SIAF, '_ZS-SIAF-PE-1'))
                siaf_module._submit_additional_fields(frappe.get_doc(SIAF, '_ZS-SIAF-PE-1'))
                with self.assertRaises(zatca_sending.ZatcaSendInProgress):
                    zatca_sending.send_now('_ZS-PE-1', 'Payment Entry')
            submit.assert_not_called()

    def test_prepayments_are_listed_and_sent_now(self):
        _prepayment('_ZS-PE-2', now_datetime() - datetime.timedelta(days=1))
        _siaf('_ZS-SIAF-PE-2', '_ZS-PE-2', 'Ready For Batch', doctype='Payment Entry')
        _prepayment('_ZS-PE-PLAIN', now_datetime(), prepayment=0)  # an ordinary payment: not an invoice
        result = zatca_sending.get_sending_status(invoice_doctype='Payment Entry', company=COMPANY)
        names = [r['invoice'] for r in result['data'] if r['invoice'].startswith('_ZS-')]
        self.assertEqual(names, ['_ZS-PE-2'])
        row = result['data'][[r['invoice'] for r in result['data']].index('_ZS-PE-2')]
        self.assertEqual((row['customer'], row['status_group'], row['can_send']), (CUSTOMER, 'not_sent', True))
        self.assertTrue(row['near_deadline'])  # issued at midnight a day ago

        def accept(doc):
            frappe.db.set_value(SIAF, doc.name, {'integration_status': 'Accepted', 'docstatus': 1})
            return Ok('Accepted')

        with mock.patch.object(SalesInvoiceAdditionalFields, 'submit_to_zatca', autospec=True, side_effect=accept):
            sent = zatca_sending.send_now('_ZS-PE-2', 'Payment Entry')
        self.assertEqual((sent['sent'], sent['outcome']), (True, 'sent'))

    def test_prepayment_send_now_checks_permissions(self):
        _prepayment('_ZS-PE-3', now_datetime())
        _siaf('_ZS-SIAF-PE-3', '_ZS-PE-3', 'Ready For Batch', doctype='Payment Entry')
        frappe.set_user('Guest')
        try:
            with mock.patch.object(SalesInvoiceAdditionalFields, 'submit_to_zatca') as submit:
                with self.assertRaises(frappe.PermissionError):
                    zatca_sending.send_now('_ZS-PE-3', 'Payment Entry')
            submit.assert_not_called()
        finally:
            frappe.set_user('Administrator')


class TestPrecomputedType(_SendingTestCase):
    def _precomputed(self, xml):
        _invoice('_ZS-PRE-INV', now_datetime())
        doc = frappe.new_doc(SIAF)
        doc.sales_invoice, doc.invoice_doctype, doc.precomputed, doc.invoice_xml = (
            '_ZS-PRE-INV',
            'Sales Invoice',
            1,
            xml,
        )
        doc.before_insert()
        return doc

    def test_the_type_comes_from_the_device_xml(self):
        doc = self._precomputed('<Invoice><cbc:InvoiceTypeCode name="0100000">388</cbc:InvoiceTypeCode></Invoice>')
        self.assertEqual(doc.invoice_type_transaction, '0100000')

    def test_without_one_in_the_xml_the_settings_decide(self):
        settings = frappe._dict(invoice_mode=InvoiceMode.Standard)
        with (
            mock.patch.object(siaf_module.ZATCABusinessSettings, 'for_invoice', return_value=settings),
            mock.patch.object(SalesInvoiceAdditionalFields, '_get_buyer_doc', return_value=None),
            mock.patch.object(siaf_module, '_check_invoice_for_export', return_value=False),
        ):
            doc = self._precomputed('<Invoice/>')
        self.assertEqual(doc.invoice_type_transaction, '0100000')


class TestPrecomputedBackfill(_SendingTestCase):
    def test_pending_precomputed_rows_get_their_type_from_the_xml(self):
        from ksa_compliance.patches import _2026_11_28_backfill_precomputed_invoice_type as patch

        _invoice('_ZS-PRE-OLD', now_datetime())
        _siaf('_ZS-SIAF-PRE-OLD', '_ZS-PRE-OLD', 'Ready For Batch', transaction='')
        _invoice('_ZS-PRE-DONE', now_datetime())
        _siaf('_ZS-SIAF-PRE-DONE', '_ZS-PRE-DONE', 'Accepted', docstatus=1, transaction='')
        xml = '<Invoice><cbc:InvoiceTypeCode name="0100000">388</cbc:InvoiceTypeCode></Invoice>'
        for name in ('_ZS-SIAF-PRE-OLD', '_ZS-SIAF-PRE-DONE'):
            frappe.db.set_value(SIAF, name, {'precomputed': 1, 'invoice_xml': xml}, update_modified=False)
        patch.execute()
        self.assertEqual(frappe.db.get_value(SIAF, '_ZS-SIAF-PRE-OLD', 'invoice_type_transaction'), '0100000')
        self.assertFalse(frappe.db.get_value(SIAF, '_ZS-SIAF-PRE-DONE', 'invoice_type_transaction'))  # final: left


class TestStatusList(_SendingTestCase):
    def setUp(self):
        super().setUp()
        now = now_datetime()
        _invoice('_ZS-SENT', now - datetime.timedelta(hours=2))
        _siaf('_ZS-SIAF-SENT', '_ZS-SENT', 'Accepted', docstatus=1)
        _invoice('_ZS-PENDING', now - datetime.timedelta(hours=21))
        _siaf('_ZS-SIAF-PENDING', '_ZS-PENDING', 'Resend')
        _invoice('_ZS-FAILED', now - datetime.timedelta(hours=3), customer='_ZS Other')
        _siaf('_ZS-SIAF-FAILED', '_ZS-FAILED', 'Rejected', docstatus=1, transaction='0100000')
        _siaf('_ZS-SIAF-FAILED-OLD', '_ZS-FAILED', 'Ready For Batch', latest=0)  # an earlier generation
        _invoice('_ZS-NOSIAF', now - datetime.timedelta(days=1))

    def _names(self, **filters):
        return sorted(
            r['invoice'] for r in zatca_sending.get_sending_status(**filters)['data'] if r['invoice'].startswith('_ZS-')
        )

    def test_status_groups(self):
        self.assertEqual(self._names(status_group='sent'), ['_ZS-SENT'])
        self.assertEqual(self._names(status_group='not_sent'), ['_ZS-NOSIAF', '_ZS-PENDING'])
        self.assertEqual(self._names(status_group='failed'), ['_ZS-FAILED'])
        self.assertEqual(len(self._names()), 4)

    def test_counts_and_deadline(self):
        result = zatca_sending.get_sending_status(company=COMPANY)
        self.assertGreaterEqual(result['counts']['not_sent'], 2)
        self.assertGreaterEqual(result['near_deadline'], 1)
        pending = next(r for r in result['data'] if r['invoice'] == '_ZS-PENDING')
        self.assertTrue(pending['can_send'])
        self.assertTrue(pending['near_deadline'])
        self.assertEqual(pending['invoice_type'], 'Simplified')

    def test_type_customer_and_date_filters(self):
        self.assertEqual(self._names(invoice_type='Standard'), ['_ZS-FAILED'])
        self.assertEqual(self._names(invoice_type='Simplified'), ['_ZS-PENDING', '_ZS-SENT'])
        self.assertEqual(self._names(customer='_ZS Other'), ['_ZS-FAILED'])
        today = now_datetime().date()
        self.assertNotIn('_ZS-NOSIAF', self._names(from_date=str(today), to_date=str(today)))

    def test_pagination(self):
        first = zatca_sending.get_sending_status(company=COMPANY, page_length=2)
        self.assertEqual(len(first['data']), 2)
        self.assertEqual(first['page_length'], 2)
        self.assertEqual(
            zatca_sending.get_sending_status(page_length=10_000)['page_length'], zatca_sending.MAX_PAGE_LENGTH
        )

    def test_a_company_the_user_may_not_read_is_refused(self):
        with self.assertRaises(frappe.PermissionError):
            zatca_sending.get_sending_status(company='_ZS Not Mine')

    def test_bad_filters_are_refused(self):
        with self.assertRaises(frappe.ValidationError):
            zatca_sending.get_sending_status(status_group='bogus')
        with self.assertRaises(frappe.ValidationError):
            zatca_sending.get_sending_status(invoice_doctype='Journal Entry')


class TestReportingLimitAlert(_SendingTestCase):
    def test_alerts_once_per_invoice_past_the_threshold(self):
        now = now_datetime()
        _invoice('_ZS-OLD', now - datetime.timedelta(hours=21))
        _siaf('_ZS-SIAF-OLD', '_ZS-OLD', 'Ready For Batch')
        _invoice('_ZS-NEW', now - datetime.timedelta(hours=10))
        _siaf('_ZS-SIAF-NEW', '_ZS-NEW', 'Ready For Batch')
        _invoice('_ZS-STD', now - datetime.timedelta(hours=21))
        _siaf('_ZS-SIAF-STD', '_ZS-STD', 'Ready For Batch', transaction='0100000')  # standard: not a reporting case

        with (
            mock.patch.object(zatca_sending, 'alert_recipients', return_value=['Administrator']),
            mock.patch('frappe.desk.doctype.notification_log.notification_log.make_notification_logs') as notify,
        ):
            zatca_sending.alert_near_reporting_limit()
            zatca_sending.alert_near_reporting_limit()

        alerted = [c.args[0].document_name for c in notify.call_args_list if c.args[0].document_name.startswith('_ZS')]
        self.assertEqual(alerted, ['_ZS-OLD'])
        self.assertEqual(frappe.db.get_value(SIAF, '_ZS-SIAF-OLD', 'deadline_alert_sent'), 1)
        self.assertEqual(frappe.db.get_value(SIAF, '_ZS-SIAF-NEW', 'deadline_alert_sent'), 0)

    def test_one_failing_row_does_not_stop_the_rest(self):
        now = now_datetime()
        _invoice('_ZS-OLD-A', now - datetime.timedelta(hours=23))
        _siaf('_ZS-SIAF-OLD-A', '_ZS-OLD-A', 'Ready For Batch')
        _invoice('_ZS-OLD-B', now - datetime.timedelta(hours=21))
        _siaf('_ZS-SIAF-OLD-B', '_ZS-OLD-B', 'Ready For Batch')

        def notify(row, users, at):
            if row.invoice == '_ZS-OLD-A':
                raise RuntimeError('notification log full')

        with (
            mock.patch.object(zatca_sending, 'alert_recipients', return_value=['Administrator']),
            mock.patch.object(zatca_sending, '_notify', side_effect=notify) as notified,
        ):
            zatca_sending.alert_near_reporting_limit()

        tried = [c.args[0].invoice for c in notified.call_args_list if c.args[0].invoice.startswith('_ZS')]
        self.assertEqual(tried, ['_ZS-OLD-A', '_ZS-OLD-B'])
        self.assertEqual(frappe.db.get_value(SIAF, '_ZS-SIAF-OLD-A', 'deadline_alert_sent'), 0)  # tried again later
        self.assertEqual(frappe.db.get_value(SIAF, '_ZS-SIAF-OLD-B', 'deadline_alert_sent'), 1)
