"""Apply the RouteShield schema before a deployment receives traffic.

Schema changes are additive in this repository.  Destructive changes require an
approved, separately reviewed migration and must not be folded into startup.
"""

from __future__ import annotations

import asyncio
import os

from apps.api.postgres_repository import PostgresRouteShieldRepository


async def main() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required for migrations")
    repository = PostgresRouteShieldRepository(database_url)
    try:
        await repository.setup()
    finally:
        await repository.close()


if __name__ == "__main__":
    asyncio.run(main())
