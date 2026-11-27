import datetime
from typing import Optional, cast

import frappe
from frappe.query_builder import DocType
from pypika import Order
from pypika.queries import QueryBuilder

from ksa_compliance import logger
from ksa_compliance.zatca_sending_rules import PENDING_STATUSES
from ksa_compliance.zatca_sending import ModeResolver, send_pending, should_send_in_run


@frappe.whitelist()
def add_batch_to_background_queue(check_date=datetime.date.today()):
    try:
        logger.info('Start Enqueue E-Invoices')
        frappe.enqueue(
            'ksa_compliance.background_jobs.sync_e_invoices',
            check_date=check_date,
            queue='long',
            timeout=3480,  # 58 minutes, so that we can run it hourly
            job_name='Sync E-Invoices',
            deduplicate=True,
            job_id=f'Sending invoices {check_date}',
        )
    except Exception as ex:
        logger.error('An error occurred queueing the job', exc_info=ex)


def sync_e_invoices(
    check_date: Optional[datetime.datetime | datetime.date] = None,
    batch_size: int = 100,
    dry_run: bool = False,
    companies: Optional[list[str]] = None,
):
    """Sends pending additional fields to ZATCA.

    Without `companies` this is the hourly run: it sends standard invoices, and simplified ones whose company sends
    live or in (legacy) batches; under a scheduled mode it only retries simplified ones already attempted. With
    `companies` it is those companies' scheduled run and sends everything they have pending."""
    prefix = '[Dry run] ' if dry_run else ''
    run = f'scheduled run for {companies}' if companies is not None else 'hourly run'
    logger.info(f'{prefix}Syncing with ZATCA in batches of {batch_size} ({run})')
    resolver = ModeResolver()
    if check_date:
        logger.info(f'{prefix}Limiting sync to >= date: {check_date}')

    # We can't use a numerical offset and increment it by the number of records because of the nature of the query.
    # We're querying for draft sales invoice additional fields then submitting them. Let's say we start with offset 0
    # and get 100 sales invoice additional fields. We submit the 100 and increase the offset to 100. Then we query
    # for a 100 **draft** sales invoice additional fields with offset 100, which skips a 100 draft additional sales
    # invoice fields because the 100 that we wanted to skip are now submitted, not draft.
    #
    # If we kept the offset at 0, the loop would never terminate in dry_run mode because we never update status.
    #
    # The solution is to use the creation date itself as an offset/filter. We sort by it ascending, so after every
    # batch we can query for fields whose creation > the last creation in the previous batch
    if isinstance(check_date, datetime.date):
        offset = cast(Optional[datetime.datetime], datetime.datetime.combine(check_date, datetime.time.min))
    else:
        offset = cast(Optional[datetime.datetime], check_date)

    while True:
        query = build_query(offset, batch_size)
        additional_field_docs = query.run(as_dict=True)
        if not additional_field_docs:
            break

        logger.info(f'{prefix}Syncing {len(additional_field_docs)} after date/time {offset}')
        offset = additional_field_docs[-1].creation

        for doc in additional_field_docs:
            try:
                if not should_send_in_run(doc, resolver, companies):
                    continue

                logger.info(f'{prefix}Submitting {doc.name}')
                if dry_run:
                    continue

                # Locked, re-checked and committed: a send already running elsewhere is skipped, not repeated
                result = send_pending(doc.name)
                logger.info(f'{prefix}{doc.name}: {result["message"]}')
            except Exception:
                logger.error(f'{prefix}Error submitting {doc.name}', exc_info=True)
                frappe.db.rollback()

    logger.info(f'{prefix}Sync Done')


def build_query(check_date: Optional[datetime.datetime], limit: int) -> QueryBuilder:
    batch_status = list(PENDING_STATUSES)
    doctype = DocType('Sales Invoice Additional Fields')
    query = (
        frappe.qb.from_(doctype)
        .select(
            doctype.name,
            doctype.creation,
            doctype.invoice_doctype,
            doctype.sales_invoice,
            doctype.integration_status,
            doctype.invoice_type_transaction,
            doctype.precomputed_invoice,
        )
        .where((doctype.integration_status.isin(batch_status)) & (doctype.docstatus == 0))
    )
    if check_date:
        query = query.where(doctype.creation > check_date)
    query = query.orderby(doctype.creation, order=Order.asc).limit(limit)
    return query
