import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.auth import require_tenant
from app.models.auth import AuthenticatedUser
from app.services.cache import get_revenue_summary
from app.services.reservations import PropertyNotFound

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/dashboard/summary")
async def get_dashboard_summary(
    property_id: str,
    month: Optional[int] = Query(None, ge=1, le=12, description="Calendar month, in the property timezone"),
    year: Optional[int] = Query(None, ge=1970, le=2100),
    # require_tenant guarantees a non-null tenant_id. There is no
    # "default_tenant" fallback anywhere on this path: a request without a
    # resolved tenant used to land in a shared bucket, which is exactly how one
    # client ends up reading another client's revenue.
    current_user: AuthenticatedUser = Depends(require_tenant),
) -> Dict[str, Any]:

    tenant_id = current_user.tenant_id

    if (month is None) != (year is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="month and year must be provided together",
        )

    try:
        revenue_data = await get_revenue_summary(property_id, tenant_id, month, year)
    except PropertyNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Property {property_id} not found",
        )

    return {
        "property_id": revenue_data["property_id"],
        "period": revenue_data["period"],
        "timezone": revenue_data["timezone"],
        # Sent as a decimal string: JSON numbers are IEEE-754 doubles and cannot
        # represent every 2-decimal amount exactly, which is what made totals
        # drift by a cent.
        "total_revenue": revenue_data["total"],
        "currency": revenue_data["currency"],
        "reservations_count": revenue_data["count"],
    }
