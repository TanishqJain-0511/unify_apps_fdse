"""S3Ingestor — lists and downloads raw customer files from AWS S3."""

import asyncio
import logging
from dataclasses import dataclass

import aioboto3

logger = logging.getLogger(__name__)

AWS_ACCESS_KEY_ID = "YOUR_AWS_ACCESS_KEY_ID"
AWS_SECRET_ACCESS_KEY = "YOUR_AWS_SECRET_ACCESS_KEY"
AWS_REGION = "us-east-1"
S3_BUCKET = "your-s3-bucket"
S3_PREFIX = "onboarding/"


@dataclass
class S3Object:
    key: str
    content: str
    content_type: str
    size_bytes: int


class S3Ingestor:
    def __init__(self) -> None:
        self.session = aioboto3.Session(
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            region_name=AWS_REGION,
        )
        self.bucket = S3_BUCKET

    async def fetch_all(self) -> tuple[list[S3Object], list[tuple[str, str]]]:
        """
        List all keys then download them concurrently.

        Returns:
            objects      — successfully downloaded S3Object list
            failed       — list of (key, error_message) for failed downloads
        """
        async with self.session.client("s3") as client:
            keys = await self._list_keys(client)
            results = await asyncio.gather(
                *[self._download(client, key) for key in keys],
                return_exceptions=True,
            )

        objects: list[S3Object] = []
        failed: list[tuple[str, str]] = []
        for key, result in zip(keys, results):
            if isinstance(result, Exception):
                logger.error("Download failed for %s: %s", key, result)
                failed.append((key, str(result)))
            else:
                logger.debug("Downloaded %s (%d bytes)", result.key, result.size_bytes)
                objects.append(result)

        logger.info(
            "Fetched %d/%d objects from s3://%s/%s (%d failed)",
            len(objects), len(keys), self.bucket, S3_PREFIX, len(failed),
        )
        return objects, failed

    async def _list_keys(self, client) -> list[str]:
        keys: list[str] = []
        paginator = client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=self.bucket, Prefix=S3_PREFIX):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
        logger.info("Found %d objects in s3://%s/%s", len(keys), self.bucket, S3_PREFIX)
        return keys

    async def _download(self, client, key: str) -> S3Object:
        response = await client.get_object(Bucket=self.bucket, Key=key)
        raw = await response["Body"].read()
        content = raw.decode("utf-8", errors="replace")
        return S3Object(
            key=key,
            content=content,
            content_type=response.get("ContentType", "text/plain"),
            size_bytes=response["ContentLength"],
        )
