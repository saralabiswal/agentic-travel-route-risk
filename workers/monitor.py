"""Cloud Run Job entry point for isolated scheduled assessment processing."""

from __future__ import annotations

import asyncio
import json

from apps.api.main import app, lifespan, process_due_assessments


async def main() -> None:
    async with lifespan(app):
        result = await process_due_assessments()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
