import os
import uuid
import hashlib
import logging
import requests
import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Literal
from pydantic import ValidationError

from datamodels import Manifest, CallbackEnvelope
from exceptions import VeritasRunnerError
from status import StatusClass

logger = logging.getLogger(__name__)

MAX_MANIFEST_SIZE_BYTES = 256 * 1024  # 256 KB limit per ADR-13 & SPEC-14


# --- Network & API Operations ---

def fetch_manifest(attempt_id: str) -> Manifest:
    """
    Fetches the attempt manifest from PathoEQA, enforcing the full HTTP status taxonomy,
    payload size boundaries, and schema validation rules.
    """
    api_url = os.environ.get("PATHOEQA_API_URL", "").rstrip("/")
    url = f"{api_url}/attempts/{attempt_id}/manifest"
    headers = {"Authorization": f"Bearer {os.environ.get('GITHUB_OIDC_TOKEN', '')}"}

    logger.info("Fetching manifest for attempt_id=%s", attempt_id)

    try:
        response = requests.get(url, headers=headers, stream=True, timeout=15)
        response.raise_for_status()

    except requests.exceptions.HTTPError as e:
        if e.response is None:
            raise VeritasRunnerError(
                StatusClass.SERVER_UNAVAILABLE,
                "PathoEQA endpoint failed to return an HTTP response."
            )

        status_code = e.response.status_code
        
        if status_code in (404, 409):
            raise VeritasRunnerError(
                StatusClass.INVALID_INPUT,
                f"PathoEQA rejected attempt_id {attempt_id} (HTTP {status_code})."
            )

        elif status_code in (401, 403):
            raise VeritasRunnerError(
                StatusClass.AUTHENTICATION_ERROR,
                f"Authentication failed with PathoEQA (HTTP {status_code})."
            )

        # Rate Limit Exceeded (429) or Server Errors & Gateway Outages (500, 502, 503, 504): Retriable errors
        elif status_code == 429 or status_code >= 500:
            raise VeritasRunnerError(
                StatusClass.SERVER_UNAVAILABLE,
                f"PathoEQA server unavailable or rate limited (HTTP {status_code})."
            )

        # Any unexpected HTTP status
        else:
            raise VeritasRunnerError(
                StatusClass.UNKNOWN_HTTP_ERROR,
                f"Unexpected HTTP status {status_code} fetching manifest."
            )

    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        # Pure transport failure / local network issue
        raise VeritasRunnerError(
            StatusClass.SERVER_UNAVAILABLE,
            f"Network transport error reaching PathoEQA endpoint: {e}"
        )

    # --- Payload Size Checks ---
    content_length = response.headers.get("Content-Length")
    if content_length and int(content_length) > MAX_MANIFEST_SIZE_BYTES:
        raise VeritasRunnerError(
            StatusClass.INVALID_INPUT,
            f"Manifest Content-Length ({content_length} bytes) exceeds 256 KB limit."
        )

    raw_bytes = response.raw.read(MAX_MANIFEST_SIZE_BYTES + 1)
    if len(raw_bytes) > MAX_MANIFEST_SIZE_BYTES:
        raise VeritasRunnerError(
            StatusClass.INVALID_INPUT,
            "Manifest payload exceeded maximum allowed size of 256 KB."
        )

    # --- Schema & Validation Checks ---
    try:
        return Manifest.model_validate_json(raw_bytes)

    except ValidationError as e:
        raise VeritasRunnerError(
            StatusClass.INVALID_INPUT,
            f"Manifest failed schema validation ({e.error_count()} error(s))."
        )

    except json.JSONDecodeError as e:
        raise VeritasRunnerError(
            StatusClass.INVALID_INPUT,
            f"Manifest payload is not valid JSON (line {e.lineno}, col {e.colno}: {e.msg})."
        )

    except Exception as e:
        # Bug inside internal model validator code / Python runtime bug
        raise VeritasRunnerError(
            StatusClass.INTERNAL_ERROR,
            f"Internal runner failure parsing manifest: {type(e).__name__}"
        )

def download_file(url: str, dest_path: str, expected_sha256: str, expected_size: Optional[int] = None) -> None:
    """
    Streams download to disk, computing SHA-256 and byte counts on the fly.
    Enforces optional size limits during stream, mandatory byte count check,
    and mandatory cryptographic SHA-256 verification on completion.
    """
    logger.info("Downloading file to %s", dest_path)
    sha256_hash = hashlib.sha256()
    downloaded_bytes = 0

    try:
        with requests.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        sha256_hash.update(chunk)
                        downloaded_bytes += len(chunk)

                        if expected_size is not None and downloaded_bytes > expected_size:
                            raise VeritasRunnerError(
                                StatusClass.DOWNLOAD_FAILED,
                                f"Download stream exceeded expected size of {expected_size} bytes."
                            )
    except requests.RequestException as e:
        raise VeritasRunnerError(
            StatusClass.DOWNLOAD_FAILED,
            f"Network error downloading {dest_path}: {e}"
        )

    # Optional strict size verification
    if expected_size is not None and downloaded_bytes != expected_size:
        raise VeritasRunnerError(
            StatusClass.DOWNLOAD_FAILED,
            f"Size mismatch for {dest_path}: expected {expected_size} bytes, got {downloaded_bytes}."
        )

    # Mandatory cryptographic SHA-256 verification
    computed_sha256 = sha256_hash.hexdigest()
    if computed_sha256.lower() != expected_sha256.lower():
        raise VeritasRunnerError(
            StatusClass.CHECKSUM_MISMATCH,
            f"SHA-256 mismatch for {dest_path}. Expected {expected_sha256}, got {computed_sha256}."
        )


# --- Callback Factory & Dispatcher ---

def build_callback(
    attempt_id: str,
    workflow_run_id: int,
    event_type: Literal[
        "attempt_started", "attempt_completed", "attempt_partial", "attempt_failed",
        "sample_started", "sample_completed", "sample_completed_with_warnings",
        "sample_not_evaluable", "sample_failed",
    ],
    sample_run_id: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> CallbackEnvelope:
    """Factory function to build a validated CallbackEnvelope."""
    return CallbackEnvelope(
        schema_version="1.0",
        event_id=str(uuid.uuid4()),
        attempt_id=attempt_id,
        workflow_run_id=workflow_run_id,
        event_type=event_type,
        occurred_at=datetime.now(timezone.utc).isoformat(),
        sample_run_id=sample_run_id,
        payload=payload or {},
    )


def send_callback(envelope: CallbackEnvelope) -> None:
    """Dispatches a CallbackEnvelope event payload back to PathoEQA."""
    api_url = os.environ.get("PATHOEQA_API_URL", "").rstrip("/")
    url = f"{api_url}/callbacks"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {os.environ.get('GITHUB_OIDC_TOKEN', '')}",
    }

    logger.info(
        "Dispatching callback [event_type=%s, attempt_id=%s, sample_run_id=%s]",
        envelope.event_type,
        envelope.attempt_id,
        envelope.sample_run_id,
    )

    try:
        response = requests.post(
            url,
            json=envelope.model_dump(exclude_none=True),
            headers=headers,
            timeout=15
        )
        response.raise_for_status()
    except requests.RequestException as e:
        logger.error("Failed to deliver callback %s: %s", envelope.event_type, e)