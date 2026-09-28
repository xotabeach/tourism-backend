#!/usr/bin/env python3
"""Create or refresh the КРЫМТРИП editorial profile (spec 16, D6, D18).

A service user: owns editorial routes and articles, can be followed, never
signs in (the server refuses it codes and tokens), earns no points and has
no place in the ratings. Its number is a non-dialable placeholder. The logo
is uploaded as its avatar from the admin.

Dry-run by default; prints the profile id either way.

  docker compose exec -T backend python scripts/editorial/create_editorial_profile.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

import tourism_backend.main  # noqa: F401  (every model, for the mapper)
from tourism_backend.config import get_settings
from tourism_backend.db.session import create_engine, create_session_factory
from tourism_backend.modules.identity.infrastructure.models import User

NAME = "КРЫМТРИП"
# Not a dialable number in any country: no SMS can ever reach it.
PHONE = "+000000000001"


async def main(apply: bool) -> None:
    engine = create_engine(get_settings())
    try:
        async with create_session_factory(engine)() as session:
            user = await session.scalar(select(User).where(User.phone_e164 == PHONE))
            if user is None:
                user = User(
                    id=uuid4(),
                    display_name=NAME,
                    phone_e164=PHONE,
                    is_system_account=True,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
                session.add(user)
                action = "created"
            else:
                user.display_name = NAME
                user.is_system_account = True
                action = "refreshed"
            if apply:
                await session.commit()
            else:
                await session.rollback()
                action = f"would be {action}"
            print(f"{NAME} {action}: {user.id}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    asyncio.run(main(parser.parse_args().apply))
