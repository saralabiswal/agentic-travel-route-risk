"""Minimal public static-console service with no API or provider credentials."""

from __future__ import annotations

import json
import os

from fastapi import FastAPI
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="RouteShield Web", docs_url=None, redoc_url=None)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "routeshield-web"}


@app.get("/runtime-config.js")
async def runtime_config() -> Response:
    """Expose only a browser-safe API base URL, never a credential or tenant ID."""
    api_base_url = os.getenv("PUBLIC_API_BASE_URL", "").rstrip("/")
    body = f"window.ROUTESHIELD_CONFIG = {json.dumps({'apiBaseUrl': api_base_url})};\n"
    return Response(
        content=body,
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/console/")


app.mount("/console", StaticFiles(directory="apps/web", html=True), name="console")
