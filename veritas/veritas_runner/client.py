# veritas/manifest.py

import logging
import os
from typing import Optional

import requests
from pydantic import ValidationError

from veritas_runner.exceptions import VeritasRunnerError, raise_for_http_status
from veritas_runner.status import StatusClass
from veritas_runner.datamodels import Manifest

logger = logging.getLogger(__name__)

MAX_MANIFEST_SIZE_BYTES = int(os.environ.get("VERITAS_MANIFEST_MAX_BYTES", 256 * 1024))
CONNECT_TIMEOUT_S = int(os.environ.get("VERITAS_HTTP_CONNECT_TIMEOUT", 10))
READ_TIMEOUT_S = int(os.environ.get("VERITAS_HTTP_READ_TIMEOUT", 15))
SUPPORTED_SCHEMA_MAJOR = os.environ.get("VERITAS_SUPPORTED_SCHEMA_MAJOR", "1")


def fetch_manifest(
    attempt_id: str,
    api_url: str,
    oidc_token: str,
    session: Optional[requests.Session] = None,
) -> Manifest:
    """
    Fetches and validates the attempt manifest from PathoEQA.

    Assumes attempt_id, api_url, and oidc_token have been pre-validated.
    """
    http = session or requests
    url = f"{api_url.rstrip('/')}/attempts/{attempt_id}/manifest"
    headers = {"Authorization": f"Bearer {oidc_token}"}

    logger.info("Fetching manifest for attempt_id=%s", attempt_id)

    try:
        response = http.get(
            url,
            headers=headers,
            timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
        )
        raise_for_http_status(response.status_code, attempt_id)

        raw_bytes = response.content
        if len(raw_bytes) > MAX_MANIFEST_SIZE_BYTES:
            raise VeritasRunnerError(
                StatusClass.MANIFEST_INVALID,
                f"Manifest payload ({len(raw_bytes)} bytes) exceeds limit of {MAX_MANIFEST_SIZE_BYTES} bytes "
                f"for attempt '{attempt_id}'."
            )

    except VeritasRunnerError:
        raise
    except requests.exceptions.Timeout as e:
        raise VeritasRunnerError(
            StatusClass.SERVER_UNAVAILABLE,
            f"PathoEQA endpoint timed out fetching manifest for attempt '{attempt_id}'."
        ) from e
    except requests.exceptions.ConnectionError as e:
        raise VeritasRunnerError(
            StatusClass.SERVER_UNAVAILABLE,
            f"Failed to establish network connection to PathoEQA for attempt '{attempt_id}'."
        ) from e
    except requests.exceptions.InvalidURL as e:
        raise VeritasRunnerError(
            StatusClass.CONFIG_ERROR,
            f"Invalid API URL structure while fetching manifest for attempt '{attempt_id}': {e}"
        ) from e
    except requests.exceptions.RequestException as e:
        raise VeritasRunnerError(
            StatusClass.UNKNOWN_HTTP_ERROR,
            f"Transport failure fetching manifest for attempt '{attempt_id}': {type(e).__name__}."
        ) from e

    return _parse_manifest(raw_bytes, attempt_id)


def _parse_manifest(raw_bytes: bytes, attempt_id: str) -> Manifest:
    """
    Parses the attempt manifest from raw bytes, enforcing schema validation rules.
    """
    try:
        manifest = Manifest.model_validate_json(raw_bytes)
    except ValidationError as e:
        raise VeritasRunnerError(
            StatusClass.MANIFEST_INVALID,
            f"Manifest schema validation failed for attempt '{attempt_id}' ({e.error_count()} error(s))."
        ) from e
    except Exception as e:
        raise VeritasRunnerError(
            StatusClass.INTERNAL_ERROR,
            f"Internal runner failure parsing manifest for attempt '{attempt_id}': {type(e).__name__}."
        ) from e

    major = manifest.schema_version.split(".", 1)[0]
    if major != SUPPORTED_SCHEMA_MAJOR:
        raise VeritasRunnerError(
            StatusClass.MANIFEST_INVALID,
            f"Unsupported manifest schema_version '{manifest.schema_version}' "
            f"for attempt '{attempt_id}'; this runner supports major version {SUPPORTED_SCHEMA_MAJOR}."
        )

    return manifest

def download_file(
    url: str, 
    dest_path: str, 
    expected_sha256: str, 
    expected_size: Optional[int] = None
) -> None:
    """
    Streams a file download to disk, computing SHA-256 and byte counts on the fly.
    Enforces optional size limits during streaming and mandatory SHA-256 verification.
    """
    logger.info("Downloading asset to %s", dest_path)
    sha256_hash = hashlib.sha256()
    downloaded_bytes = 0

    try:
        with requests.get(url, stream=True, timeout=60) as response:
            response.raise_for_status()
            
            with open(dest_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        sha256_hash.update(chunk)
                        downloaded_bytes += len(chunk)

                        # Abort early if download exceeds expected size
                        if expected_size is not None and downloaded_bytes > expected_size:
                            raise VeritasRunnerError(
                                StatusClass.DOWNLOAD_FAILED,
                                f"Download stream exceeded expected size limit of {expected_size} bytes."
                            )

    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code
        raise VeritasRunnerError(
            StatusClass.DOWNLOAD_FAILED,
            f"HTTP error {status_code} while downloading file."
        )
    except requests.exceptions.Timeout:
        raise VeritasRunnerError(
            StatusClass.DOWNLOAD_FAILED,
            "Timed out while downloading asset file."
        )
    except requests.exceptions.RequestException:
        raise VeritasRunnerError(
            StatusClass.DOWNLOAD_FAILED,
            "Network transport failure during asset download."
        )

    # Size Verification
    if expected_size is not None and downloaded_bytes != expected_size:
        raise VeritasRunnerError(
            StatusClass.DOWNLOAD_FAILED,
            f"Size mismatch for {dest_path}: expected {expected_size} bytes, got {downloaded_bytes}."
        )

    # Mandatory Cryptographic Integrity Check
    computed_sha256 = sha256_hash.hexdigest()
    if computed_sha256.lower() != expected_sha256.lower():
        raise VeritasRunnerError(
            StatusClass.CHECKSUM_MISMATCH,
            f"SHA-256 mismatch for asset. Expected {expected_sha256[:12]}..., got {computed_sha256[:12]}..."
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