"""Lifecycle-safe PostgreSQL LangGraph checkpointer factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


@asynccontextmanager
async def postgres_checkpointer(database_url: str) -> AsyncIterator[AsyncPostgresSaver]:
    """Create required checkpoint tables once, then yield a durable saver."""
    conn_string = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    async with AsyncPostgresSaver.from_conn_string(conn_string) as saver:
        await saver.setup()
        yield saver
