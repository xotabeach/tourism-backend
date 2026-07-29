from fastapi import APIRouter

from tourism_backend.modules.favorites.presentation.router import router as favorites_router
from tourism_backend.modules.geography.presentation.router import router as geography_router
from tourism_backend.modules.identity.presentation.router import router as identity_router
from tourism_backend.modules.places.presentation.router import router as places_router
from tourism_backend.modules.routes.presentation.router import router as routes_router
from tourism_backend.modules.support.presentation.router import router as support_router

router = APIRouter(prefix="/api/v1")
router.include_router(geography_router)
router.include_router(places_router)
router.include_router(routes_router)
router.include_router(identity_router)
router.include_router(favorites_router)
router.include_router(support_router)


@router.get("")
@router.get("/")
async def api_v1_root() -> dict[str, str]:
    return {"name": "tourism-backend", "api_version": "v1"}
