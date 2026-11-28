"""ZerOne: the company's cash customer always gets a simplified (B2C) invoice (ZR-868, owner issue 1, point 6).

zerone_business marks it on the Company (`zerone_cash_customer`); `_get_invoice_type` reads the mark before the
company's invoice mode, so a company on «Standard» still reports a walk-in sale as simplified. The Frappe runner
supplies the connection; the Company's field is set inside a rolled-back savepoint.
"""

import frappe
from ksa_compliance.zerone_test_support import ZeroneSiteTestCase

from ksa_compliance.ksa_compliance.doctype.sales_invoice_additional_fields.sales_invoice_additional_fields import (
    _get_invoice_type,
    is_zerone_cash_customer,
)
from ksa_compliance.invoice import InvoiceMode


class TestZeroneCashCustomer(ZeroneSiteTestCase):
    def setUp(self):
        super().setUp()
        if not frappe.get_meta('Company').has_field('zerone_cash_customer'):
            self.skipTest('zerone_business is not installed: no Company.zerone_cash_customer')
        self.company = frappe.get_all('Company', pluck='name', order_by='creation asc', limit=1)[0]
        frappe.db.set_value('Company', self.company, 'zerone_cash_customer', 'ZR868 Walk-in', update_modified=False)

    def settings(self, mode):
        return frappe._dict(company=self.company, invoice_mode=mode)

    def test_the_cash_customer_is_simplified_in_every_mode(self):
        walk_in = frappe._dict(name='ZR868 Walk-in')
        for mode in InvoiceMode:
            with self.subTest(mode=mode):
                self.assertTrue(is_zerone_cash_customer(self.settings(mode), walk_in))
                self.assertEqual(_get_invoice_type(self.settings(mode), walk_in), 'Simplified')

    def test_another_customer_follows_the_company_mode(self):
        other = frappe._dict(name='ZR868 Company customer')
        self.assertFalse(is_zerone_cash_customer(self.settings(InvoiceMode.Standard), other))
        self.assertEqual(_get_invoice_type(self.settings(InvoiceMode.Standard), other), 'Standard')
        self.assertFalse(is_zerone_cash_customer(self.settings(InvoiceMode.Standard), None))
