import logging
from typing import Dict, Any, Optional
from urllib.parse import quote

from app.core.redis_client import redis_client
from app.services.reservations import calculate_total_revenue, period_label

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 300


def build_cache_key(
    property_id: str,
    tenant_id: str,
    month: Optional[int] = None,
    year: Optional[int] = None,
) -> str:
    """Build a cache key that is unique per tenant, property and period.

    The tenant id is mandatory: property ids are only unique *within* a tenant
    (see the composite primary key in database/schema.sql), so a key built from
    the property id alone makes two different companies share the same entry.

    Components are percent-escaped: `property_id` comes from the query string,
    and a raw `:` in it would let two different triples collapse onto one key.
    """
    parts = (quote(tenant_id, safe=""), quote(property_id, safe=""), period_label(month, year))
    return "revenue:" + ":".join(parts)


async def get_revenue_summary(
    property_id: str,
    tenant_id: str,
    month: Optional[int] = None,
    year: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Fetches revenue summary, utilizing caching to improve performance.
    """
    if not tenant_id:
        raise ValueError("tenant_id is required to read revenue data")

    cache_key = build_cache_key(property_id, tenant_id, month, year)

    # The shared client is pooled, configured from Settings, opened and closed by
    # the app lifespan, and already returns None on any Redis failure - so a cache
    # outage degrades to a recompute instead of breaking the dashboard.
    cached = await redis_client.get(cache_key)
    if cached:
        return cached

    result = await calculate_total_revenue(property_id, tenant_id, month, year)

    # Cache the result for 5 minutes
    await redis_client.set(cache_key, result, ttl=CACHE_TTL_SECONDS)

    return result
