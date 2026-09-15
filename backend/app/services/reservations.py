"""Revenue aggregation over the `reservations` table.

Two accuracy rules govern this module:

1. A calendar month is a *local* notion. Month boundaries must be built in the
   property's own timezone and only then compared against UTC timestamps,
   otherwise bookings near midnight fall into the wrong month.
2. Money is `NUMERIC(10, 3)` in Postgres. It stays a `Decimal` all the way
   through and is rounded exactly once, on the final total. Rounding each row
   (or going through `float`) is what produces the "off by a few cents" totals.
"""

import logging
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text

from app.core.database_pool import db_pool

logger = logging.getLogger(__name__)

CENTS = Decimal("0.01")
DEFAULT_TIMEZONE = "UTC"
DEFAULT_CURRENCY = "USD"


class PropertyNotFound(Exception):
    """Raised when a property does not exist for the given tenant."""


def _round_money(amount: Decimal) -> Decimal:
    """Round a monetary total to 2 decimals, once, at the very end."""
    return amount.quantize(CENTS, rounding=ROUND_HALF_UP)


def period_label(month: Optional[int], year: Optional[int]) -> str:
    """Canonical name for a reporting period.

    Defined once, next to the month-bounds logic: this string is the `period`
    field of the API response and part of the cache key, so the two must not be
    able to drift apart.
    """
    return f"{year:04d}-{month:02d}" if month is not None and year is not None else "all-time"


def month_bounds_utc(year: int, month: int, timezone_name: str) -> Tuple[datetime, datetime]:
    """Return the [start, end) bounds of a month as timezone-aware datetimes.

    The bounds are anchored in the property's local timezone. Postgres compares
    them against `check_in_date` (TIMESTAMP WITH TIME ZONE) after normalising
    both sides to UTC, so the comparison is correct without any manual shifting.
    """
    tz = ZoneInfo(timezone_name)
    start = datetime(year, month, 1, tzinfo=tz)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=tz)
    else:
        end = datetime(year, month + 1, 1, tzinfo=tz)
    return start, end


async def _get_property_timezone(session, property_id: str, tenant_id: str) -> str:
    """Read the property timezone, scoped to the tenant that owns it."""
    result = await session.execute(
        text(
            """
            SELECT timezone
            FROM properties
            WHERE id = :property_id AND tenant_id = :tenant_id
            """
        ),
        {"property_id": property_id, "tenant_id": tenant_id},
    )
    row = result.fetchone()
    if row is None:
        raise PropertyNotFound(f"Property {property_id} not found for tenant {tenant_id}")

    # `properties.timezone` is free-text, so a typo would otherwise raise
    # ZoneInfoNotFoundError out of month_bounds_utc and 500 the dashboard.
    timezone_name = row.timezone or DEFAULT_TIMEZONE
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.error(
            "Property %s (tenant %s) has an unusable timezone %r - falling back to %s",
            property_id, tenant_id, timezone_name, DEFAULT_TIMEZONE,
        )
        return DEFAULT_TIMEZONE
    return timezone_name


async def _sum_revenue(
    session,
    property_id: str,
    tenant_id: str,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Sum reservation amounts for one property of one tenant.

    `tenant_id` is part of every WHERE clause: property ids are only unique per
    tenant (see the composite primary key in database/schema.sql), so filtering
    on `property_id` alone would mix two companies' bookings together.
    """
    params: Dict[str, Any] = {"property_id": property_id, "tenant_id": tenant_id}
    date_filter = ""
    if start is not None and end is not None:
        date_filter = "AND check_in_date >= :start_date AND check_in_date < :end_date"
        params["start_date"] = start
        params["end_date"] = end

    result = await session.execute(
        text(
            f"""
            SELECT
                COALESCE(currency, :default_currency) AS currency,
                SUM(total_amount) AS total_revenue,
                COUNT(*) AS reservation_count
            FROM reservations
            WHERE property_id = :property_id
              AND tenant_id = :tenant_id
              {date_filter}
            GROUP BY 1
            """
        ),
        {**params, "default_currency": DEFAULT_CURRENCY},
    )
    rows = result.fetchall()

    if not rows:
        return {"total": Decimal("0"), "currency": DEFAULT_CURRENCY, "count": 0}

    if len(rows) > 1:
        # Amounts in different currencies cannot be added up. Surface it loudly
        # instead of silently returning a meaningless number.
        currencies = sorted(row.currency for row in rows)
        raise ValueError(
            f"Property {property_id} (tenant {tenant_id}) mixes currencies {currencies}; "
            "revenue cannot be aggregated into a single total"
        )

    row = rows[0]
    return {
        "total": Decimal(row.total_revenue),
        "currency": row.currency,
        "count": row.reservation_count,
    }


async def calculate_total_revenue(
    property_id: str,
    tenant_id: str,
    month: Optional[int] = None,
    year: Optional[int] = None,
) -> Dict[str, Any]:
    """Aggregate revenue for a property.

    Without `month`/`year` this returns the all-time total. With both, it
    returns the total for that calendar month in the property's timezone.
    This is the single entry point: the month-bounds and round-once rules are
    applied here and nowhere else.
    """
    await db_pool.initialize()
    async with db_pool.get_session() as session:
        timezone_name = await _get_property_timezone(session, property_id, tenant_id)

        start = end = None
        if month is not None and year is not None:
            start, end = month_bounds_utc(year, month, timezone_name)

        summary = await _sum_revenue(session, property_id, tenant_id, start, end)

    return {
        "property_id": property_id,
        "tenant_id": tenant_id,
        "timezone": timezone_name,
        "period": period_label(month, year),
        # Serialised as a string so the exact decimal survives JSON and the
        # cache round-trip; float would reintroduce the cent drift.
        "total": str(_round_money(summary["total"])),
        "currency": summary["currency"],
        "count": summary["count"],
    }
