import frappe

from ksa_compliance.zatca_sending_rules import transaction_from_xml


def execute():
    """Device-computed additional fields created before their type was stored: read it from the device XML.

    Pending ones, so standard ones are cleared by the hourly job and simplified ones reach the 24 hour alert; sent
    and failed ones too, so the sending report's Standard / Simplified filter and type column cover them. The type
    is a fact of the signed XML, so writing it on a submitted document changes nothing ZATCA saw. Only blank types
    are written, so the patch can run again."""
    rows = frappe.db.sql(
        """select name, invoice_xml from `tabSales Invoice Additional Fields`
            where precomputed = 1 and ifnull(invoice_type_transaction, '') = ''""",
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
