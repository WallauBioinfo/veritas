import os
import uuid
import hashlib
import logging
import requests
from datetime import datetime, timezone
from typing import Optional, Set, Dict, Any, Literal
from pydantic import ValidationError

from datamodels import Manifest, SampleInput, ManifestFile, CallbackEnvelope
from exceptions import VeritasRunnerError
from status import StatusClass

logger = logging.getLogger(__name__)

MAX_MANIFEST_SIZE_BYTES = 256 * 1024  # 256 KB limit per ADR-13

# --- Network & API Operations ---

def fetch_manifest(attempt_id: str) -> Manifest:
    """
    Fetches the attempt manifest from PathoEQA, enforces the 256 KB payload size limit,
    and parses/validates the response into a Manifest Pydantic model.
    """
    api_url = os.environ.get("PATHOEQA_API_URL", "").rstrip("/")
    url = f"{api_url}/attempts/{attempt_id}/manifest"
    headers = {"Authorization": f"Bearer {os.environ.get('GITHUB_OIDC_TOKEN', '')}"}

    logger.info("Fetching manifest for attempt_id=%s", attempt_id)
    
    try:
        response = requests.get(url, headers=headers, stream=True, timeout=15)
        response.raise_for_status()
    except requests.RequestException as e:
        raise VeritasRunnerError(
            StatusClass.FETCH_FAILED,
            f"Failed to reach manifest endpoint: {e}"
        )

    # 1. Fast Content-Length check if header is present
    content_length = response.headers.get("Content-Length")
    if content_length and int(content_length) > MAX_MANIFEST_SIZE_BYTES:
        raise VeritasRunnerError(
            StatusClass.MANIFEST_INVALID,
            f"Manifest Content-Length ({content_length} bytes) exceeds 256 KB limit."
        )

    # 2. Strict read up to 256 KB + 1 byte
    raw_bytes = response.raw.read(MAX_MANIFEST_SIZE_BYTES + 1)
    if len(raw_bytes) > MAX_MANIFEST_SIZE_BYTES:
        raise VeritasRunnerError(
            StatusClass.MANIFEST_INVALID,
            "Manifest payload exceeded maximum allowed size of 256 KB."
        )

    try:
        # Pydantic parses and validates schema automatically
        manifest = Manifest.model_validate_json(raw_bytes)

        # Enforce domain-specific file checks across all samples
        for sample in manifest.samples:
            validate_sample_files(sample)

        return manifest

    except ValidationError as e:
        raise VeritasRunnerError(
            StatusClass.MANIFEST_INVALID, 
            f"Manifest failed schema validation: {e}"
        )
    except VeritasRunnerError:
        raise
    except Exception as e:
        raise VeritasRunnerError(
            StatusClass.MANIFEST_INVALID, 
            f"Failed to process manifest payload: {e}"
        )


def download_file(url: str, dest_path: str, expected_sha256: str, expected_size: Optional[int] = None) -> None:
    """
    Streams download to disk, computing SHA-256 and byte counts on the fly.
    Enforces optional size limits during stream and mandatory SHA-256 check on completion.
    """
    if not expected_sha256:
        raise VeritasRunnerError(
            StatusClass.MANIFEST_INVALID,
            f"Cannot download {dest_path}: expected SHA-256 is empty."
        )

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
                                StatusClass.FETCH_FAILED,
                                f"Download stream exceeded expected size of {expected_size} bytes."
                            )
    except requests.RequestException as e:
        raise VeritasRunnerError(
            StatusClass.FETCH_FAILED,
            f"Network error downloading {dest_path}: {e}"
        )

    # Optional strict size verification
    if expected_size is not None and downloaded_bytes != expected_size:
        raise VeritasRunnerError(
            StatusClass.FETCH_FAILED,
            f"Size mismatch for {dest_path}: expected {expected_size} bytes, got {downloaded_bytes}."
        )

    # Mandatory cryptographic SHA-256 verification
    computed_sha256 = sha256_hash.hexdigest()
    if computed_sha256 != expected_sha256:
        raise VeritasRunnerError(
            StatusClass.FETCH_FAILED,
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