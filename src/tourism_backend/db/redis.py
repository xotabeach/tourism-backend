from redis.asyncio import Redis

from tourism_backend.config import Settings


def create_redis_client(settings: Settings) -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


async def ping_redis(client: Redis) -> None:
    await client.ping()
