"""Three provider defects found by the Zero One audit (2026-10-27): the Phase 1 QR's time, a Payment Entry buyer
without an address, and which earlier generation of an invoice's additional fields gives way.

Outside the doctype folders on purpose: a FrappeTestCase there makes the runner preload ERPNext's test masters,
which assume a USD site and fail on a SAR one ('Standard Buying' again)."""

from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase

from ksa_compliance import jinja

DOCTYPE = 'Sales Invoice Additional Fields'


class TestPhase1AndGenerations(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        frappe.db.savepoint('zerone_phase1')

    def tearDown(self):
        frappe.db.rollback(save_point='zerone_phase1')
        super().tearDown()

    def test_phase1_qr_time_is_the_sites_local_time_in_utc(self):
        # A posting time is the site's local time; 12:00 in Riyadh is 09:00Z, whatever the server's clock zone.
        with mock.patch.object(jinja, 'get_system_timezone', return_value='Asia/Riyadh', create=True):
            self.assertEqual(jinja._format_date('2026-10-28', '12:00:00'), '2026-10-28T09:00:00Z')

    def test_a_payment_entry_customer_without_any_address_on_a_simplified_invoice(self):
        # No primary address and no linked one: a simplified (B2C) document needs none and must not fail.
        siaf = frappe.new_doc(DOCTYPE)
        customer = frappe._dict(name='_no such customer', customer_primary_address=None,
                                custom_vat_registration_number=None, custom_additional_ids=[])
        siaf._set_buyer_details(customer, 'Simplified', frappe._dict(doctype='Payment Entry'))
        self.assertFalse(siaf.get('buyer_street_name'))
