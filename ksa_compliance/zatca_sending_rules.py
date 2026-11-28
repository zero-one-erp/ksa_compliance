"""When an invoice's additional fields go to ZATCA: the rules alone, without Frappe, so they can be tested off a site.

A standard (B2B) invoice is cleared at once, whatever the sending mode, because the buyer cannot use it before ZATCA
clears it. A simplified (B2C) invoice is reported according to the mode the business settings name:

- 'Live': on submission;
- 'Batches': the legacy value, by the hourly job (the behaviour of sites that predate the other modes);
- 'At a set time': once a day at the time the settings name, in the site's time zone;
- 'Every 24 hours': 24 hours after the previous scheduled run.

ZATCA expects a simplified invoice to be reported within 24 hours of its issue, so pending ones nearing that limit
raise an alert.
"""

from __future__ import annotations

import datetime
import re
from typing import Literal, Optional

MODE_LIVE = 'Live'
MODE_BATCHES = 'Batches'
MODE_AT_SET_TIME = 'At a set time'
MODE_EVERY_24_HOURS = 'Every 24 hours'
SENDING_MODES = (MODE_LIVE, MODE_BATCHES, MODE_AT_SET_TIME, MODE_EVERY_24_HOURS)
SCHEDULED_MODES = (MODE_AT_SET_TIME, MODE_EVERY_24_HOURS)

# Draft additional fields in these statuses are waiting to be sent
PENDING_STATUSES = ('Ready For Batch', 'Resend', 'Corrected')
SENT_STATUSES = ('Accepted', 'Accepted with warnings')
# Anything else a submitted additional fields document can carry is final: sending it again is refused
FAILED_STATUSES = ('Rejected', 'Duplicate', 'Clearance switched off')

# What a send came to, from the status it left stored: ZATCA took it, ZATCA refused it for good, or it is still
# pending and a later run tries again (Resend after a network or 5xx error, missing settings, an exception)
SendOutcome = Literal['sent', 'failed', 'retrying']
SEND_OUTCOMES = ('sent', 'failed', 'retrying')

StatusGroup = Literal['sent', 'not_sent', 'failed']
STATUS_GROUPS = ('sent', 'not_sent', 'failed')

REPORTING_LIMIT_HOURS = 24
ALERT_AFTER_HOURS = 20
# Under a scheduled mode the safety net sends at ALERT_AFTER_HOURS; the alert comes two hourly runs later
SCHEDULED_ALERT_AFTER_HOURS = 22
# An «At a set time» schedule that never ran starts at the first occurrence the dispatcher sees within this time
FIRST_RUN_GRACE = datetime.timedelta(hours=1)
# Pending invoices issued longer ago than this are not alerted on. It keeps a first run on an old site from flooding
# users, and keeps it inside the retention of the notification log that makes the alert once per invoice.
ALERT_LOOKBACK_DAYS = 7


def normalize_mode(value: Optional[str]) -> str:
    """The sending mode a stored value means. Blank or unknown values mean the legacy hourly batches."""
    for mode in SENDING_MODES:
        if (value or '').strip().lower() == mode.lower():
            return mode
    return MODE_BATCHES


def is_standard_transaction(invoice_type_transaction: Optional[str]) -> bool:
    """Whether an additional fields' invoice type transaction ('0100000' / '0200000', BR-KSA-06) is a standard one."""
    return (invoice_type_transaction or '').startswith('01')


_INVOICE_TYPE_CODE_NAME = re.compile(r'<(?:\w+:)?InvoiceTypeCode\b[^>]*?\bname\s*=\s*["\'](\d{7})["\']')


def transaction_from_xml(invoice_xml: Optional[str]) -> Optional[str]:
    """The invoice type transaction ('0100000', '0200000', ...) an invoice's XML names in InvoiceTypeCode/@name, or
    None. Device-computed invoices carry it only there."""
    match = _INVOICE_TYPE_CODE_NAME.search(invoice_xml or '')
    return match.group(1) if match else None


def send_outcome(integration_status: Optional[str]) -> SendOutcome:
    """What a send came to, judged by the status it stored (submit_to_zatca answers Ok for every reply ZATCA gave)."""
    if integration_status in SENT_STATUSES:
        return 'sent'
    if not integration_status or integration_status in PENDING_STATUSES:
        return 'retrying'
    return 'failed'


def send_on_submit(mode: Optional[str], is_standard: bool) -> bool:
    """Whether a newly created additional fields document is sent at once."""
    return is_standard or normalize_mode(mode) == MODE_LIVE


def hourly_job_sends(
    mode: Optional[str], is_standard: bool, integration_status: Optional[str], near_reporting_limit: bool = False
) -> bool:
    """Whether the hourly job sends a pending additional fields document.

    Under a scheduled mode a simplified invoice that was never attempted waits for its scheduled run, unless it is
    already near the 24 hour reporting limit (a run every 24 hours alone could miss it). One already attempted
    (Resend, Corrected) is retried hourly, so a transient failure does not cost it another day."""
    if is_standard or near_reporting_limit:
        return True
    if normalize_mode(mode) not in SCHEDULED_MODES:
        return True
    return integration_status != 'Ready For Batch'


def is_scheduled_run_due(
    mode: Optional[str],
    now: datetime.datetime,
    sync_time: Optional[datetime.time],
    last_run: Optional[datetime.datetime],
) -> bool:
    """Whether a scheduled run of the given mode is due at `now`, the previous one having started at `last_run`.

    All times are the site's naive local times."""
    mode = normalize_mode(mode)
    if mode == MODE_AT_SET_TIME:
        if sync_time is None:
            return False
        # The latest occurrence of the set time at or before `now`: yesterday's when today's has not come yet. The
        # dispatcher checks every few minutes, so a time after its last check of the day (23:56 with checks at
        # 23:55 and 00:00) is caught by the first check of the next day instead of never.
        occurrence = latest_occurrence(now, sync_time)
        if last_run is None:
            # Never ran: the first run follows an occurrence, not the moment the mode was chosen
            return now - occurrence < FIRST_RUN_GRACE
        return last_run < occurrence

    if mode == MODE_EVERY_24_HOURS:
        return last_run is None or now - last_run >= datetime.timedelta(hours=24)

    return False


def latest_occurrence(now: datetime.datetime, at: datetime.time) -> datetime.datetime:
    """The latest moment at or before `now` whose time of day is `at`."""
    today = datetime.datetime.combine(now.date(), at)
    return today if today <= now else today - datetime.timedelta(days=1)


def alert_after_hours(mode: Optional[str]) -> float:
    """Hours after its issue at which a pending simplified invoice raises the reporting-limit alert.

    Under Live and Batches the hourly job sends everything pending, so one still pending at ALERT_AFTER_HOURS is
    stuck and alerted on then. Under a scheduled mode an invoice issued shortly after a run waits for the safety net
    (the hourly job sends it at ALERT_AFTER_HOURS, see hourly_job_sends): that is routine, so the alert waits
    until the net has had its runs and fires only for what is still pending after them."""
    if normalize_mode(mode) in SCHEDULED_MODES:
        return SCHEDULED_ALERT_AFTER_HOURS
    return ALERT_AFTER_HOURS


def status_group(integration_status: Optional[str], has_additional_fields: bool = True) -> StatusGroup:
    """The report's group for an invoice whose latest additional fields carry `integration_status`."""
    if not has_additional_fields or not integration_status or integration_status in PENDING_STATUSES:
        return 'not_sent'
    if integration_status in SENT_STATUSES:
        return 'sent'
    return 'failed'


def statuses_for_group(group: StatusGroup) -> tuple[str, ...]:
    if group == 'sent':
        return SENT_STATUSES
    if group == 'not_sent':
        return PENDING_STATUSES
    if group == 'failed':
        return FAILED_STATUSES
    raise ValueError(f'Unknown status group: {group}')


def alert_cutoff(now: datetime.datetime, alert_after_hours: float = ALERT_AFTER_HOURS) -> datetime.datetime:
    """Pending simplified invoices issued at or before this time are close to (or past) the reporting limit."""
    return now - datetime.timedelta(hours=alert_after_hours)


def is_near_reporting_limit(
    issued_at: Optional[datetime.datetime], now: datetime.datetime, alert_after_hours: float = ALERT_AFTER_HOURS
) -> bool:
    return issued_at is not None and issued_at <= alert_cutoff(now, alert_after_hours)


def hours_left(issued_at: datetime.datetime, now: datetime.datetime) -> float:
    """Hours left before the 24 hour reporting limit; negative once it has passed."""
    deadline = issued_at + datetime.timedelta(hours=REPORTING_LIMIT_HOURS)
    return round((deadline - now).total_seconds() / 3600, 1)
