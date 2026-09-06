"""Public HTTP surface for runtime_config content (currently: company details)."""

from fastapi import APIRouter

from tourism_backend.api.deps import DbSession
from tourism_backend.modules.runtime_config.application.company_details_schemas import (
    CompanyDetailsOut,
)
from tourism_backend.modules.runtime_config.application.service import get_company_details

router = APIRouter(tags=["app-content"])


@router.get("/company-details", response_model=CompanyDetailsOut)
async def read_company_details(session: DbSession) -> CompanyDetailsOut:
    """Guest-readable, like the catalog — shown on the "О приложении" screen."""
    return await get_company_details(session)
