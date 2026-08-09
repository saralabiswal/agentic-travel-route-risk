"""Restricted original-upload storage with a Cloud Storage production implementation."""

from __future__ import annotations

import hashlib
from typing import Protocol
from urllib.parse import quote
from uuid import UUID

import httpx


class OriginalUploadStore(Protocol):
    async def put(
        self,
        *,
        tenant_id: str,
        upload_id: UUID,
        content: bytes,
        content_type: str,
    ) -> tuple[str, str]: ...


def object_key_for(*, tenant_id: str, upload_id: UUID, content_sha256: str) -> str:
    return f"quarantine/{tenant_id}/{upload_id}/{content_sha256}.csv"


class InMemoryOriginalUploadStore:
    """Local/test-only quarantine store; production selects Cloud Storage via EVIDENCE_BUCKET."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(
        self,
        *,
        tenant_id: str,
        upload_id: UUID,
        content: bytes,
        content_type: str,
    ) -> tuple[str, str]:
        del content_type
        content_sha256 = hashlib.sha256(content).hexdigest()
        object_key = object_key_for(
            tenant_id=tenant_id, upload_id=upload_id, content_sha256=content_sha256
        )
        self.objects[object_key] = content
        return object_key, content_sha256


class GcsOriginalUploadStore:
    """Upload through Cloud Run's metadata token without storing credentials locally."""

    _token_url = (
        "http://metadata.google.internal/computeMetadata/v1/instance/"
        "service-accounts/default/token"
    )

    def __init__(self, bucket_name: str) -> None:
        self.bucket_name = bucket_name

    async def put(
        self,
        *,
        tenant_id: str,
        upload_id: UUID,
        content: bytes,
        content_type: str,
    ) -> tuple[str, str]:
        content_sha256 = hashlib.sha256(content).hexdigest()
        object_key = object_key_for(
            tenant_id=tenant_id, upload_id=upload_id, content_sha256=content_sha256
        )
        async with httpx.AsyncClient(timeout=10) as client:
            token_response = await client.get(
                self._token_url, headers={"Metadata-Flavor": "Google"}
            )
            token_response.raise_for_status()
            access_token = token_response.json().get("access_token")
            if not isinstance(access_token, str) or not access_token:
                raise RuntimeError("Cloud Run metadata service did not provide an access token")
            upload_response = await client.post(
                "https://storage.googleapis.com/upload/storage/v1/b/"
                f"{quote(self.bucket_name, safe='')}/o",
                params={"uploadType": "media", "name": object_key},
                content=content,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": content_type,
                    "X-Goog-Content-SHA256": content_sha256,
                },
            )
            upload_response.raise_for_status()
        return object_key, content_sha256
