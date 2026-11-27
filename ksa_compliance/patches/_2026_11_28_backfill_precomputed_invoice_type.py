import frappe

from ksa_compliance.zatca_sending_rules import transaction_from_xml


def execute():
    """Pending device-computed additional fields created before their type was stored: read it from the device XML,
    so standard ones are cleared by the hourly job and show in the report's type filter and the 24 hour alert."""
    rows = frappe.db.sql(
        """select name, invoice_xml from `tabSales Invoice Additional Fields`
            where precomputed = 1 and docstatus = 0 and ifnull(invoice_type_transaction, '') = ''""",
        as_dict=True,
    )
    for row in rows:
        transaction = transaction_from_xml(row.invoice_xml)
        if transaction:
            frappe.db.set_value(
                'Sales Invoice Additional Fields',
                row.name,
                'invoice_type_transaction',
                transaction,
                update_modified=False,
            )
