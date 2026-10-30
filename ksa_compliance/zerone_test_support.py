"""Site tests with explicit fixtures, without Frappe's automatic DocType test records.

Frappe's runner supplies the site connection. Ordinary unittest cases are supported
by that runner and do not preload ERPNext's currency-specific master data.
"""
import unittest
import uuid


class ZeroneSiteTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        import frappe

        if not frappe.flags.in_test:
            raise RuntimeError('Run these site tests through the Frappe test runner')
        user = frappe.session.user
        point = 'zerone_' + uuid.uuid4().hex
        frappe.db.savepoint(point)
        self.addCleanup(frappe.set_user, user)
        self.addCleanup(self._rollback, point)
        frappe.set_user('Administrator')

    @staticmethod
    def _rollback(point):
        import frappe

        try:
            frappe.db.rollback(save_point=point)
        finally:
            # Documents and defaults cached during a test must not outlive its rows.
            frappe.clear_cache()
