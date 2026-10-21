"""Persist the agent's long-term memory to Cloud Storage between runs.

Cloud Run Jobs have ephemeral disks. When ``ADOPTION_AGENT_STATE_BUCKET`` is
set, the CLI downloads ``data/`` and ``reports/`` from the bucket before a run
and uploads them afterwards. The deployed job therefore builds on previous
evidence exactly like a local run does.

This uses the Cloud Storage JSON API with Application Default Credentials (the
job's service account), so no extra client library is needed.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import config

logger = logging.getLogger(__name__)

_API = "https://storage.googleapis.com/storage/v1/b/{bucket}/o"
_UPLOAD = "https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o"
SYNC_DIRS = ("data", "reports")


def state_bucket() -> str:
    return os.getenv("ADOPTION_AGENT_STATE_BUCKET", "").removeprefix("gs://").strip("/")


def _session() -> Any:
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/devstorage.read_write"])
    return AuthorizedSession(creds)


def download_state(bucket: str, session: Any | None = None) -> int:
    """Download data/ and reports/ from gs://bucket. Returns files fetched."""
    session = session or _session()
    n = 0
    for prefix in SYNC_DIRS:
        page_token = None
        while True:
            params = {"prefix": f"{prefix}/", "fields": "items(name),nextPageToken"}
            if page_token:
                params["pageToken"] = page_token
            resp = session.get(_API.format(bucket=bucket), params=params, timeout=30)
            resp.raise_for_status()
            body = resp.json()
            for item in body.get("items", []):
                name = item["name"]
                dest = config.PROJECT_ROOT / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                obj = session.get(
                    f"{_API.format(bucket=bucket)}/{quote(name, safe='')}",
                    params={"alt": "media"},
                    timeout=120,
                )
                obj.raise_for_status()
                dest.write_bytes(obj.content)
                n += 1
            page_token = body.get("nextPageToken")
            if not page_token:
                break
    logger.info("Downloaded %d state files from gs://%s", n, bucket)
    return n


def upload_state(bucket: str, session: Any | None = None) -> int:
    """Upload data/ and reports/ to gs://bucket. Returns files uploaded."""
    session = session or _session()
    n = 0
    for prefix in SYNC_DIRS:
        root = config.PROJECT_ROOT / prefix
        if not root.exists():
            continue
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            name = path.relative_to(config.PROJECT_ROOT).as_posix()
            resp = session.post(
                _UPLOAD.format(bucket=bucket),
                params={"uploadType": "media", "name": name},
                data=Path(path).read_bytes(),
                headers={"Content-Type": "application/octet-stream"},
                timeout=120,
            )
            resp.raise_for_status()
            n += 1
    logger.info("Uploaded %d state files to gs://%s", n, bucket)
    return n
