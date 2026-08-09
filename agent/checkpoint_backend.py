"""Select the durable graph checkpointer without exposing connection details to nodes."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from agent.checkpointer import postgres_checkpointer


@asynccontextmanager
async def checkpoint_backend() -> AsyncIterator[Any | None]:
    """Yield a PostgreSQL saver when DATABASE_URL is configured, else local mode."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        yield None
        return
    async with postgres_checkpointer(database_url) as saver:
        yield saver
