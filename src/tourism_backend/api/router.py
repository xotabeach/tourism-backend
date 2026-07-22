from fastapi import APIRouter

from tourism_backend.api.health import router as health_router
from tourism_backend.api.v1.router import router as v1_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(v1_router)
