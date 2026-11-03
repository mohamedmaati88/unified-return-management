"""Pure helpers shared by every channel (Sales, Inventory, POS, Portal)."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def urp_deadline_boundary(reference_utc, days, tz_name):
    """Return the exclusive deadline boundary as a naive UTC datetime.

    Rule: the local calendar day of ``reference_utc`` is day 0; the right
    expires at the end of local day ``days``. We store the *start* of local day
    ``days + 1`` and test eligibility with ``now < boundary`` — no 23:59:59 gap,
    DST-safe through zoneinfo.
    """
    if not reference_utc or days is None or not tz_name:
        return False
    zone = ZoneInfo(tz_name)
    ref = reference_utc.replace(tzinfo=timezone.utc) if reference_utc.tzinfo is None else reference_utc
    local_day0 = ref.astimezone(zone).date()
    target = datetime.combine(local_day0 + timedelta(days=days + 1), time(0))
    boundary = target.replace(tzinfo=zone)
    as_utc = boundary.astimezone(timezone.utc)
    # local midnight that does not exist (DST gap at midnight): take the first valid instant
    if as_utc.astimezone(zone).replace(tzinfo=None) < target:
        as_utc = target.replace(tzinfo=zone, fold=1).astimezone(timezone.utc)
    return as_utc.replace(tzinfo=None)


URP_LOCKABLE_TABLES = ("stock_move", "pos_order_line", "urp_return_override_line", "urp_return_exception", "pos_config",
                       "res_users", "res_company")


def urp_lock_rows(cr, table, ids):
    """Serialise two returns of the same goods (POS Refund, Inventory, two terminals).

    A plain ``SELECT ... FOR UPDATE`` is not enough under Odoo's REPEATABLE READ isolation: the
    second transaction waits for the lock, then continues with its OLD snapshot and does not see
    the return the first one just committed. Writing the rows (no-op update) makes the second
    transaction fail with a serialization error instead; Odoo retries the request with fresh data
    and the quantity check then sees the first return."""
    assert table in URP_LOCKABLE_TABLES, table
    if ids:
        cr.execute(f'UPDATE "{table}" SET write_date = write_date WHERE id IN %s', [tuple(ids)])
