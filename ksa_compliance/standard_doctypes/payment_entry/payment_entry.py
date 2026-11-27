import frappe
from frappe import _

from ksa_compliance.ksa_compliance.doctype.zatca_business_settings.zatca_business_settings import ZATCABusinessSettings
from ksa_compliance.ksa_compliance.doctype.sales_invoice_additional_fields.sales_invoice_additional_fields import (
    SalesInvoiceAdditionalFields,
)
from erpnext.accounts.doctype.payment_entry.payment_entry import PaymentEntry


from ksa_compliance import logger
from ksa_compliance.throw import fthrow
from ksa_compliance.translation import ft
from ksa_compliance.zatca_sending import send_pending
from ksa_compliance.zatca_sending_rules import is_standard_transaction, send_on_submit


def validate_payment_entry(self: PaymentEntry, method: str = None):
    if not self.custom_prepayment_invoice:
        return

    # Force the tax to be Deduct and not included_in_paid_amount
    if self.taxes:
        for row in self.get('taxes'):
            if row.included_in_paid_amount:
                fthrow(
                    msg=ft('You cannot set Included in Paid Amount for Prepayment Invoice.'),
                    title=ft('This Action Is Not Allowed'),
                )
            if row.add_deduct_tax != 'Deduct':
                fthrow(
                    msg=ft('You cannot set Add Or Deduct type to: Add for Prepayment Invoice. Allowed type is Deduct.'),
                    title=ft('This Action Is Not Allowed'),
                )
            if row.charge_type not in ['Actual', 'On Paid Amount']:
                fthrow(
                    msg=ft(
                        'You cannot set Charge Type to anything other than Actual or On Paid Amount for Prepayment Invoice.'
                    ),
                    title=ft('This Action Is Not Allowed'),
                )


def create_prepayment_invoice_additional_fields_doctype(self: PaymentEntry, method: str = None):
    if not self.custom_prepayment_invoice:
        logger.info(f"Skipping additional fields for {self.name} because it's not a prepayment invoice")
        return

    settings = ZATCABusinessSettings.for_invoice(self.name, self.doctype)
    if not settings:
        if ZATCABusinessSettings.is_revoked_for_company(self.company):
            logger.info(f'Skipping additional fields for {self.name} because of revoked ZATCA settings')
            return
        logger.info(f'Skipping additional fields for {self.name} because of missing ZATCA settings')
        return

    if not settings.enable_zatca_integration:
        logger.info(f'Skipping additional fields for {self.name} because ZATCA integration is disabled in settings')
        return

    prepayment_additional_fields_doc = SalesInvoiceAdditionalFields.create_for_invoice(self.name, self.doctype)
    prepayment_additional_fields_doc.insert()

    # As for sales invoices: a standard (B2B) prepayment is cleared at once whatever the mode, a simplified one follows
    # the mode; either way through the one locked send path
    is_standard = is_standard_transaction(prepayment_additional_fields_doc.invoice_type_transaction)
    if send_on_submit(settings.sync_with_zatca, is_standard):
        # We're running in the context of invoice submission (on_submit hook). We only want to run our ZATCA logic if
        # the invoice submits successfully after on_submit is run successfully from all apps.
        frappe.utils.background_jobs.enqueue(
            'ksa_compliance.zatca_sending.send_pending_job',
            siaf_name=prepayment_additional_fields_doc.name,
            enqueue_after_commit=True,
        )


def _submit_additional_fields(doc: SalesInvoiceAdditionalFields):
    # Kept for jobs enqueued before an upgrade; they go through the locked path by name, like new ones.
    send_pending(doc.name)


def prevent_cancellation_of_prepayment_invoice(self: PaymentEntry, method):
    is_phase_2_enabled_for_company = ZATCABusinessSettings.is_enabled_for_company(self.company)
    if is_phase_2_enabled_for_company and self.custom_prepayment_invoice:
        frappe.throw(
            msg=_('You cannot cancel Prepayment Invoice according to ZATCA Regulations.'),
            title=_('This Action Is Not Allowed'),
        )
