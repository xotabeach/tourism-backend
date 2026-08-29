from fastapi import APIRouter

from tourism_backend.modules.favorites.presentation.router import router as favorites_router
from tourism_backend.modules.geography.presentation.router import router as geography_router
from tourism_backend.modules.identity.presentation.router import router as identity_router
from tourism_backend.modules.identity.presentation.users_router import router as users_router
from tourism_backend.modules.maps.presentation.router import router as maps_router
from tourism_backend.modules.notifications.presentation.router import (
    router as notifications_router,
)
from tourism_backend.modules.places.presentation.router import router as places_router
from tourism_backend.modules.recommendations.presentation.router import (
    router as recommendations_router,
)
from tourism_backend.modules.route_builder.presentation.router import (
    router as route_builder_router,
)
from tourism_backend.modules.route_execution.presentation.router import (
    router as route_execution_router,
)
from tourism_backend.modules.routes.presentation.router import router as routes_router
from tourism_backend.modules.subscriptions.presentation.router import (
    router as subscriptions_router,
)
from tourism_backend.modules.support.presentation.router import router as support_router

router = APIRouter(prefix="/api/v1")
router.include_router(geography_router)
router.include_router(places_router)
router.include_router(recommendations_router)
router.include_router(routes_router)
router.include_router(route_execution_router)
router.include_router(route_builder_router)
router.include_router(identity_router)
router.include_router(users_router)
router.include_router(favorites_router)
router.include_router(support_router)
router.include_router(notifications_router)
router.include_router(subscriptions_router)
router.include_router(maps_router)


@router.get("")
@router.get("/")
async def api_v1_root() -> dict[str, str]:
    return {"name": "tourism-backend", "api_version": "v1"}
