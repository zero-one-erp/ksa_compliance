"""Sending invoices to ZATCA: the one locked path every sender goes through, «send now», the status list for the
report, the scheduled sending of simplified invoices and the alert before ZATCA's 24 hour reporting limit.

The rules (which mode sends what and when) live in `zatca_sending_rules`, without Frappe; this module applies them.
"""

from __future__ import annotations

import contextlib
import datetime
import uuid
from typing import Iterator, Optional, cast

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, get_time, getdate, now_datetime
from result import is_ok

from ksa_compliance import logger
from ksa_compliance import zatca_sending_rules as rules

SIAF = 'Sales Invoice Additional Fields'
INVOICE_DOCTYPES = ('Sales Invoice', 'POS Invoice')
BULK_LIMIT = 100
MAX_PAGE_LENGTH = 500
SEND_LOCK_SECONDS = 300  # well above the API timeout (30 s) and the signing-free submit path
ALERT_ROLES = ('Accounts Manager', 'System Manager')


class ZatcaSendInProgress(frappe.ValidationError):
    pass


# --------------------------------------------------------------------------------------------------------------------
# The locked send path


@contextlib.contextmanager
def _send_lock(siaf_name: str) -> Iterator[bool]:
    """Holds a Redis lock on one additional fields document for the duration of a send; yields whether it was taken.

    The lock is shared by every sender (submission, the hourly and scheduled jobs, «send now»), so two workers never
    send the same invoice at once."""
    cache = frappe.cache
    key = cache.make_key(f'ksa_compliance:zatca_send:{siaf_name}')
    token = uuid.uuid4().hex
    acquired = bool(cache.set(key, token, nx=True, ex=SEND_LOCK_SECONDS))
    try:
        yield acquired
    finally:
        if acquired:
            current = cache.get(key)
            if current in (token, token.encode()):
                cache.delete(key)


def send_pending(siaf_name: str) -> dict:
    """Sends one pending additional fields document to ZATCA through the existing submit path, under the send lock.

    Returns {'siaf', 'sent', 'integration_status', 'message', 'skipped'}: `skipped` is None when it was sent, else
    'in_progress' (another worker holds the lock) or 'not_pending' (it is already final or gone).
    The outcome is committed before the lock is released, so the next holder sees it."""
    from ksa_compliance.ksa_compliance.doctype.sales_invoice_additional_fields.sales_invoice_additional_fields import (
        SalesInvoiceAdditionalFields,
    )

    with _send_lock(siaf_name) as acquired:
        if not acquired:
            status = frappe.db.get_value(SIAF, siaf_name, 'integration_status')
            return _send_result(
                siaf_name, False, status, _('A send of this invoice is already in progress'), 'in_progress'
            )

        # A locking read sees the latest committed row whatever this transaction read before.
        row = frappe.db.get_value(SIAF, siaf_name, ['docstatus', 'integration_status'], as_dict=True, for_update=True)
        if not row or row.docstatus != 0 or row.integration_status not in rules.PENDING_STATUSES:
            return _send_result(
                siaf_name,
                False,
                row.integration_status if row else None,
                _('This invoice is not waiting to be sent'),
                'not_pending',
            )

        try:
            doc = cast(SalesInvoiceAdditionalFields, frappe.get_doc(SIAF, siaf_name))
            logger.info(f'Submitting {siaf_name}')
            result = doc.submit_to_zatca()
            message = result.ok_value if is_ok(result) else result.err_value
            logger.info(f'{siaf_name}: {message}')
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            raise

        return _send_result(siaf_name, is_ok(result), doc.integration_status, message, None)


def _send_result(siaf_name, sent, status, message, skipped) -> dict:
    return {
        'siaf': siaf_name,
        'sent': bool(sent),
        'integration_status': status or '',
        'message': message,
        'skipped': skipped,
    }


def send_pending_job(siaf_name: str) -> None:
    """Background job: sends a newly submitted invoice's additional fields."""
    send_pending(siaf_name)


# --------------------------------------------------------------------------------------------------------------------
# Send now


@frappe.whitelist(methods=['POST'])
def send_now(invoice: str, invoice_doctype: str = 'Sales Invoice') -> dict:
    """Sends one invoice's pending ZATCA additional fields now.

    Throws if the caller may not read and submit the invoice, if it has nothing pending (its status is final), or if a
    send is already running. Returns the invoice's row: {'invoice', 'invoice_doctype', 'siaf', 'sent',
    'integration_status', 'status_group', 'message'}."""
    return _send_now(invoice, invoice_doctype)


@frappe.whitelist(methods=['POST'])
def send_now_bulk(invoices: str | list, invoice_doctype: str = 'Sales Invoice') -> dict:
    """«إرسال المحدد»: sends up to BULK_LIMIT invoices, one at a time; one invoice's failure does not stop the rest.

    Returns {'results': [row | {'invoice', 'error'}], 'sent': int, 'failed': int}."""
    names = frappe.parse_json(invoices) if isinstance(invoices, str) else invoices
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        frappe.throw(_('Invoices must be a list of names'))
    names = list(dict.fromkeys(names))
    if len(names) > BULK_LIMIT:
        frappe.throw(_('At most {0} invoices can be sent at once').format(BULK_LIMIT))

    results, sent, failed = [], 0, 0
    for name in names:
        try:
            row = _send_now(name, invoice_doctype)
            results.append(row)
            sent += 1 if row['sent'] else 0
            failed += 0 if row['sent'] else 1
        except (frappe.PermissionError, frappe.ValidationError) as e:
            results.append({'invoice': name, 'invoice_doctype': invoice_doctype, 'error': str(e) or type(e).__name__})
            failed += 1
        except Exception as e:
            logger.error(f'Error sending {name} to ZATCA', exc_info=True)
            results.append({'invoice': name, 'invoice_doctype': invoice_doctype, 'error': str(e) or type(e).__name__})
            failed += 1
        finally:
            # Each refusal queues a message for the desk; the result carries them instead.
            frappe.clear_messages()

    return {'results': results, 'sent': sent, 'failed': failed}


def _send_now(invoice: str, invoice_doctype: str) -> dict:
    if invoice_doctype not in INVOICE_DOCTYPES:
        frappe.throw(_('Unsupported invoice type: {0}').format(invoice_doctype))

    if not frappe.db.exists(invoice_doctype, invoice):
        raise frappe.DoesNotExistError(_('{0} {1} not found').format(_(invoice_doctype), invoice))

    frappe.has_permission(invoice_doctype, 'read', doc=invoice, throw=True)
    frappe.has_permission(invoice_doctype, 'submit', doc=invoice, throw=True)

    siaf = frappe.db.get_value(
        SIAF,
        {'sales_invoice': invoice, 'invoice_doctype': invoice_doctype, 'is_latest': 1},
        ['name', 'docstatus', 'integration_status'],
        as_dict=True,
    )
    if not siaf:
        frappe.throw(_('{0} has no ZATCA data to send yet').format(invoice))
    if siaf.docstatus != 0 or siaf.integration_status not in rules.PENDING_STATUSES:
        frappe.throw(
            _('{0} cannot be sent again: its ZATCA status is final ({1})').format(
                invoice, _(siaf.integration_status or '')
            )
        )

    result = send_pending(siaf.name)
    if result['skipped'] == 'in_progress':
        frappe.throw(_('{0} is already being sent to ZATCA').format(invoice), exc=ZatcaSendInProgress)
    if result['skipped'] == 'not_pending':
        frappe.throw(
            _('{0} cannot be sent again: its ZATCA status is final ({1})').format(
                invoice, _(result['integration_status'])
            )
        )

    return {
        'invoice': invoice,
        'invoice_doctype': invoice_doctype,
        'siaf': result['siaf'],
        'sent': result['sent'],
        'integration_status': result['integration_status'],
        'status_group': rules.status_group(result['integration_status']),
        'message': result['message'],
    }


# --------------------------------------------------------------------------------------------------------------------
# Status list


@frappe.whitelist()
def get_sending_status(
    status_group: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    invoice_type: str | None = None,
    customer: str | None = None,
    company: str | None = None,
    invoice_doctype: str = 'Sales Invoice',
    start: int | str = 0,
    page_length: int | str = 50,
) -> dict:
    """Read-only, paginated list of submitted invoices with their ZATCA status, for the sending report.

    status_group: 'sent' | 'not_sent' | 'failed' | None (all). invoice_type: 'Standard' | 'Simplified' | None.
    Only companies with active ZATCA settings that the user may read are listed, under the invoice's own permissions.
    """
    if invoice_doctype not in INVOICE_DOCTYPES:
        frappe.throw(_('Unsupported invoice type: {0}').format(invoice_doctype))
    if status_group and status_group not in rules.STATUS_GROUPS:
        frappe.throw(_('Unknown status group: {0}').format(status_group))
    if invoice_type and invoice_type not in ('Standard', 'Simplified'):
        frappe.throw(_('Unknown invoice type: {0}').format(invoice_type))
    frappe.has_permission(invoice_doctype, 'read', throw=True)

    start = max(cint(start), 0)
    page_length = min(max(cint(page_length) or 50, 1), MAX_PAGE_LENGTH)
    companies = _readable_zatca_companies(invoice_doctype)
    if company:
        if company not in companies:
            raise frappe.PermissionError(_('Not permitted for company {0}').format(company))
        companies = [company]

    empty = {
        'data': [],
        'total': 0,
        'counts': {g: 0 for g in rules.STATUS_GROUPS},
        'near_deadline': 0,
        'start': start,
        'page_length': page_length,
    }
    if not companies:
        return empty

    inv = f'`tab{invoice_doctype}`'
    values = {'doctype': invoice_doctype, 'companies': tuple(companies), 'start': start, 'page_length': page_length}
    base = [f'{inv}.docstatus = 1', f'{inv}.company in %(companies)s']
    if invoice_doctype == 'Sales Invoice':
        base.append(f'ifnull({inv}.is_consolidated, 0) = 0')
    if from_date:
        base.append(f'{inv}.posting_date >= %(from_date)s')
        values['from_date'] = getdate(from_date)
    if to_date:
        base.append(f'{inv}.posting_date <= %(to_date)s')
        values['to_date'] = getdate(to_date)
    if customer:
        base.append(f'{inv}.customer = %(customer)s')
        values['customer'] = customer
    if invoice_type:
        base.append('siaf.invoice_type_transaction like %(type_prefix)s')
        values['type_prefix'] = '01%' if invoice_type == 'Standard' else '02%'

    from frappe.desk.reportview import get_match_cond

    match = get_match_cond(invoice_doctype)  # '' or ' and (...)', on the `tabX` table name
    joins = (
        f'from {inv} left join `tab{SIAF}` siaf on siaf.sales_invoice = {inv}.name '
        f'and siaf.invoice_doctype = %(doctype)s and siaf.is_latest = 1'
    )
    group_sql = {g: _group_condition(g) for g in rules.STATUS_GROUPS}
    where = ' and '.join(base) + match

    counts_row = frappe.db.sql(
        'select '
        + ', '.join(f'sum(case when {cond} then 1 else 0 end) as {g}' for g, cond in group_sql.items())
        + f' {joins} where {where}',
        values,
        as_dict=True,
    )[0]
    counts = {g: cint(counts_row.get(g)) for g in rules.STATUS_GROUPS}

    page_where = where + (f' and {group_sql[status_group]}' if status_group else '')
    total = counts[status_group] if status_group else sum(counts.values())
    rows = frappe.db.sql(
        f"""select {inv}.name as invoice, {inv}.company, {inv}.customer, {inv}.customer_name,
                {inv}.posting_date, {inv}.posting_time, {inv}.grand_total, {inv}.currency,
                siaf.name as siaf, siaf.docstatus as siaf_docstatus, siaf.integration_status,
                siaf.invoice_type_transaction, siaf.last_attempt
            {joins} where {page_where}
            order by {inv}.posting_date desc, {inv}.posting_time desc, {inv}.name desc
            limit %(page_length)s offset %(start)s""",
        values,
        as_dict=True,
    )

    now = now_datetime()
    data = [_status_row(row, invoice_doctype, now) for row in rows]
    near_deadline = _count_near_deadline(invoice_doctype, companies, customer, match, now)
    return {
        'data': data,
        'total': total,
        'counts': counts,
        'near_deadline': near_deadline,
        'start': start,
        'page_length': page_length,
    }


def _group_condition(group: str) -> str:
    def quoted(statuses):
        return ', '.join(frappe.db.escape(s) for s in statuses)

    pending = quoted(rules.PENDING_STATUSES)
    if group == 'sent':
        return f'siaf.integration_status in ({quoted(rules.SENT_STATUSES)})'
    if group == 'not_sent':
        return f"(siaf.name is null or ifnull(siaf.integration_status, '') in ('', {pending}))"
    # Failed: any other status, which only final documents carry
    return (
        f"(siaf.name is not null and ifnull(siaf.integration_status, '') not in "
        f"('', {pending}, {quoted(rules.SENT_STATUSES)}))"
    )


def _status_row(row, invoice_doctype: str, now: datetime.datetime) -> dict:
    has_siaf = bool(row.siaf)
    group = rules.status_group(row.integration_status, has_siaf)
    invoice_type = None
    if has_siaf and row.invoice_type_transaction:
        invoice_type = 'Standard' if rules.is_standard_transaction(row.invoice_type_transaction) else 'Simplified'
    issued_at = _issued_at(row.posting_date, row.posting_time)
    pending_simplified = group == 'not_sent' and invoice_type == 'Simplified'
    return {
        'invoice': row.invoice,
        'invoice_doctype': invoice_doctype,
        'company': row.company,
        'customer': row.customer,
        'customer_name': row.customer_name,
        'posting_date': row.posting_date,
        'posting_time': str(row.posting_time) if row.posting_time is not None else None,
        'grand_total': row.grand_total,
        'currency': row.currency,
        'siaf': row.siaf,
        'integration_status': row.integration_status or '',
        'status_group': group,
        'invoice_type': invoice_type,
        'last_attempt': row.last_attempt,
        'can_send': has_siaf and row.siaf_docstatus == 0 and row.integration_status in rules.PENDING_STATUSES,
        'hours_left': rules.hours_left(issued_at, now) if pending_simplified and issued_at else None,
        'near_deadline': pending_simplified and rules.is_near_reporting_limit(issued_at, now),
    }


def _issued_at(posting_date, posting_time) -> Optional[datetime.datetime]:
    if not posting_date:
        return None
    return get_datetime(f'{getdate(posting_date)} {posting_time or "00:00:00"}')


def _count_near_deadline(invoice_doctype, companies, customer, match, now) -> int:
    """Pending simplified invoices at or past the alert threshold (whatever their date), under the same permissions."""
    inv = f'`tab{invoice_doctype}`'
    values = {
        'doctype': invoice_doctype,
        'companies': tuple(companies),
        'cutoff': rules.alert_cutoff(now),
        'pending': rules.PENDING_STATUSES,
    }
    conds = ''
    if customer:
        conds = f' and {inv}.customer = %(customer)s'
        values['customer'] = customer
    return cint(
        frappe.db.sql(
            f"""select count(*) from {inv} join `tab{SIAF}` siaf on siaf.sales_invoice = {inv}.name
                and siaf.invoice_doctype = %(doctype)s and siaf.is_latest = 1
            where {inv}.docstatus = 1 and {inv}.company in %(companies)s and siaf.docstatus = 0
                and siaf.integration_status in %(pending)s and siaf.invoice_type_transaction like '02%%'
                and timestamp({inv}.posting_date, {inv}.posting_time) <= %(cutoff)s{conds}{match}""",
            values,
        )[0][0]
    )


def _readable_zatca_companies(invoice_doctype: str, user: str | None = None) -> list[str]:
    """Companies with active, enabled ZATCA settings, limited to those the user's Company permissions allow."""
    companies = sorted(
        set(
            frappe.get_all(
                'ZATCA Business Settings', filters={'enable_zatca_integration': 1, 'status': 'Active'}, pluck='company'
            )
        )
    )
    return [c for c in companies if _user_may_read_company(c, invoice_doctype, user)]


def _user_may_read_company(company: str, invoice_doctype: str, user: str | None = None) -> bool:
    from frappe.core.doctype.user_permission.user_permission import get_user_permissions

    allowed = [
        p
        for p in get_user_permissions(user or frappe.session.user).get('Company', [])
        if not p.get('applicable_for') or p.get('applicable_for') == invoice_doctype
    ]
    return not allowed or any(p.get('doc') == company for p in allowed)


# --------------------------------------------------------------------------------------------------------------------
# Which pending documents a job sends


class ModeResolver:
    """Resolves the company and sending mode of pending additional fields, caching per run."""

    def __init__(self, now: Optional[datetime.datetime] = None):
        self.now = now or now_datetime()
        self._invoices: dict[tuple[str, str], frappe._dict] = {}
        self._modes: dict[str, str] = {}
        self._devices: dict[str, str] = {}

    def _invoice(self, row) -> frappe._dict:
        key = (row.invoice_doctype, row.sales_invoice)
        if key not in self._invoices:
            fields = ['company', 'posting_date'] + (['posting_time'] if row.invoice_doctype != 'Payment Entry' else [])
            self._invoices[key] = (
                frappe.db.get_value(row.invoice_doctype, row.sales_invoice, fields, as_dict=True) or frappe._dict()
            )
        return self._invoices[key]

    def company(self, row) -> Optional[str]:
        return self._invoice(row).get('company')

    def near_reporting_limit(self, row) -> bool:
        inv = self._invoice(row)
        return rules.is_near_reporting_limit(_issued_at(inv.get('posting_date'), inv.get('posting_time')), self.now)

    def mode(self, row) -> str:
        if row.get('precomputed_invoice'):
            # The EGS setting overrides the company's for invoices a device computed
            device = frappe.db.get_value('ZATCA Precomputed Invoice', row.precomputed_invoice, 'device_id')
            if device not in self._devices:
                self._devices[device] = rules.normalize_mode(
                    frappe.db.get_value('ZATCA EGS', {'unit_common_name': device}, 'sync_with_zatca')
                )
            return self._devices[device]

        company = self.company(row)
        if company not in self._modes:
            from ksa_compliance.ksa_compliance.doctype.zatca_business_settings.zatca_business_settings import (
                ZATCABusinessSettings,
            )

            settings = ZATCABusinessSettings.for_company(company) if company else None
            self._modes[company] = rules.normalize_mode(settings.sync_with_zatca if settings else None)
        return self._modes[company]


def should_send_in_run(row, resolver: ModeResolver, companies: Optional[list[str]]) -> bool:
    """Whether a job run sends this pending row: the hourly run (`companies` None) applies the hourly rule; a
    scheduled run sends everything its companies have pending, device-computed invoices aside (those follow their
    EGS)."""
    if companies is not None:
        return not row.get('precomputed_invoice') and resolver.company(row) in companies
    is_standard = rules.is_standard_transaction(row.get('invoice_type_transaction'))
    mode = resolver.mode(row)
    if rules.hourly_job_sends(mode, is_standard, row.get('integration_status')):
        return True
    return rules.hourly_job_sends(mode, is_standard, row.get('integration_status'), resolver.near_reporting_limit(row))


# --------------------------------------------------------------------------------------------------------------------
# Scheduled sending


def dispatch_scheduled_sending() -> None:
    """Every few minutes: starts the run of each company whose set time or 24 hours have come. Cheap when nothing is
    due; one deduplicated long job per due company, never a job per invoice."""
    now = now_datetime()
    settings_list = frappe.get_all(
        'ZATCA Business Settings',
        filters={'enable_zatca_integration': 1, 'status': 'Active', 'sync_with_zatca': ['in', rules.SCHEDULED_MODES]},
        fields=['name', 'company', 'sync_with_zatca', 'sync_time', 'last_scheduled_sync'],
    )
    for s in settings_list:
        sync_time = get_time(s.sync_time) if s.sync_time not in (None, '') else None
        last_run = get_datetime(s.last_scheduled_sync) if s.last_scheduled_sync else None
        if not rules.is_scheduled_run_due(s.sync_with_zatca, now, sync_time, last_run):
            continue

        logger.info(f'Scheduled ZATCA sending is due for {s.name} ({s.sync_with_zatca})')
        frappe.db.set_value('ZATCA Business Settings', s.name, 'last_scheduled_sync', now, update_modified=False)
        frappe.db.commit()
        frappe.enqueue(
            'ksa_compliance.background_jobs.sync_e_invoices',
            companies=[s.company],
            queue='long',
            timeout=3480,
            job_id=f'ksa_compliance:zatca_scheduled:{s.name}',
            deduplicate=True,
        )


# --------------------------------------------------------------------------------------------------------------------
# The 24 hour alert


def alert_near_reporting_limit() -> None:
    """Notifies the accounts users of each company, once per invoice, about simplified invoices still not reported
    ALERT_AFTER_HOURS after their issue (ZATCA's limit being REPORTING_LIMIT_HOURS)."""
    now = now_datetime()
    candidates = find_near_deadline(now)
    recipients: dict[str, list[str]] = {}
    for row in candidates:
        if row.company not in recipients:
            recipients[row.company] = alert_recipients(row.company)
        users = recipients[row.company]
        if users:
            _notify(row, users, now)
        frappe.db.set_value(SIAF, row.siaf, 'deadline_alert_sent', 1, update_modified=False)
        frappe.db.commit()


def find_near_deadline(now: datetime.datetime) -> list:
    """Pending simplified additional fields not alerted on, issued between the lookback floor and the cutoff."""
    values = {
        'cutoff': rules.alert_cutoff(now),
        'floor': now - datetime.timedelta(days=rules.ALERT_LOOKBACK_DAYS),
        'pending': rules.PENDING_STATUSES,
    }
    rows = []
    for doctype in ('Sales Invoice', 'POS Invoice', 'Payment Entry'):
        inv = f'`tab{doctype}`'
        issued = (
            f'timestamp({inv}.posting_date)'
            if doctype == 'Payment Entry'
            else f'timestamp({inv}.posting_date, {inv}.posting_time)'
        )
        rows += frappe.db.sql(
            f"""select siaf.name as siaf, siaf.invoice_doctype, {inv}.name as invoice, {inv}.company,
                    {issued} as issued_at
                from `tab{SIAF}` siaf join {inv} on {inv}.name = siaf.sales_invoice
                where siaf.invoice_doctype = {frappe.db.escape(doctype)} and siaf.is_latest = 1
                    and siaf.docstatus = 0 and ifnull(siaf.deadline_alert_sent, 0) = 0
                    and siaf.integration_status in %(pending)s and siaf.invoice_type_transaction like '02%%'
                    and {issued} <= %(cutoff)s and {issued} >= %(floor)s
                order by issued_at""",
            values,
            as_dict=True,
        )
    return rows


def alert_recipients(company: str) -> list[str]:
    users = frappe.get_all(
        'Has Role',
        filters={'role': ['in', ALERT_ROLES], 'parenttype': 'User'},
        pluck='parent',
        distinct=True,
    )
    enabled = set(
        frappe.get_all(
            'User', filters={'name': ['in', users or ['']], 'enabled': 1, 'user_type': 'System User'}, pluck='name'
        )
    )
    return sorted(u for u in enabled if u != 'Guest' and _user_may_read_company(company, 'Sales Invoice', u))


def _notify(row, users: list[str], now: datetime.datetime) -> None:
    from frappe.desk.doctype.notification_log.notification_log import make_notification_logs

    issued_at = get_datetime(row.issued_at)
    left = rules.hours_left(issued_at, now)
    if left > 0:
        subject = _('ZATCA: simplified invoice {0} is not reported yet; {1} hours remain of the 24 hour limit').format(
            row.invoice, left
        )
    else:
        subject = _('ZATCA: simplified invoice {0} is past the 24 hour reporting limit and not reported yet').format(
            row.invoice
        )
    make_notification_logs(
        frappe._dict(
            subject=subject,
            type='Alert',
            document_type=row.invoice_doctype,
            document_name=row.invoice,
            from_user='Administrator',
        ),
        users,
    )
