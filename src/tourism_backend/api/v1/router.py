from fastapi import APIRouter

router = APIRouter(prefix="/api/v1")


@router.get("")
@router.get("/")
async def api_v1_root() -> dict[str, str]:
    return {"name": "tourism-backend", "api_version": "v1"}
