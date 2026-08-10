# veritas/manifest.py

import logging
import os
from typing import Optional
import requests
import hashlib
from pydantic import ValidationError

from .exceptions import VeritasRunnerError, raise_for_http_status
from veritas_runner.datamodels import Manifest
from veritas_runner.status import StatusClass

logger = logging.getLogger(__name__)

MAX_MANIFEST_SIZE_BYTES = int(os.environ.get("VERITAS_MANIFEST_MAX_BYTES", 256 * 1024))
CONNECT_TIMEOUT_S = int(os.environ.get("VERITAS_HTTP_CONNECT_TIMEOUT", 10))
READ_TIMEOUT_S = int(os.environ.get("VERITAS_HTTP_READ_TIMEOUT", 15))
SUPPORTED_SCHEMA_MAJOR = os.environ.get("VERITAS_SUPPORTED_SCHEMA_MAJOR", "1")
DOWNLOAD_CONNECT_TIMEOUT_S = int(os.environ.get("VERITAS_DOWNLOAD_CONNECT_TIMEOUT", 10))
DOWNLOAD_READ_TIMEOUT_S = int(os.environ.get("VERITAS_DOWNLOAD_READ_TIMEOUT", 60))


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
        if len(raw_bytes) > MAX_MANIFEST_SIZE_BYTES: #TODO evaluate need for defensive streaming (cf. download_file)
            raise VeritasRunnerError(
                StatusClass.MANIFEST_INVALID,
                f"Manifest payload ({len(raw_bytes)} bytes) exceeds limit of {MAX_MANIFEST_SIZE_BYTES} bytes.",
                attempt_id=attempt_id,
            )

    except VeritasRunnerError:
        raise
    except requests.exceptions.Timeout as e:
        raise VeritasRunnerError(
            StatusClass.SERVER_UNAVAILABLE,
            "PathoEQA endpoint timed out during manifest fetch.",
            attempt_id=attempt_id,
        ) from e
    except requests.exceptions.ConnectionError as e:
        raise VeritasRunnerError(
            StatusClass.SERVER_UNAVAILABLE,
            "Failed to establish network connection to PathoEQA.",
            attempt_id=attempt_id,
        ) from e
    except requests.exceptions.InvalidURL as e:
        raise VeritasRunnerError(
            StatusClass.CONFIG_ERROR,
            f"Invalid API URL structure: {e}",
            attempt_id=attempt_id,
        ) from e
    except requests.exceptions.RequestException as e:
        raise VeritasRunnerError(
            StatusClass.UNKNOWN_HTTP_ERROR,
            f"Transport failure: {type(e).__name__}",
            attempt_id=attempt_id,
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
            f"Manifest schema validation failed ({e.error_count()} error(s)).",
            attempt_id=attempt_id,
        ) from e
    except Exception as e:
        raise VeritasRunnerError(
            StatusClass.INTERNAL_ERROR,
            f"Internal runner failure parsing manifest: {type(e).__name__}",
            attempt_id=attempt_id,
        ) from e

    major = manifest.schema_version.split(".", 1)[0]
    if major != SUPPORTED_SCHEMA_MAJOR:
        raise VeritasRunnerError(
            StatusClass.MANIFEST_INVALID,
            f"Unsupported schema_version '{manifest.schema_version}'; runner supports major version {SUPPORTED_SCHEMA_MAJOR}.",
            attempt_id=attempt_id,
        )

    return manifest


def download_file(
    url: str,
    dest_path: str,
    expected_sha256: str,
    expected_size: Optional[int] = None,
    attempt_id: Optional[str] = None,
    session: Optional[requests.Session] = None
) -> None:
    """
    Streams a file download to disk, computing SHA-256 and byte counts on the fly.
    Enforces optional size limits during streaming and mandatory SHA-256 verification.
    Cleans up partial or corrupted files on disk if the download or verification fails.
    """
    logger.info("Downloading asset to %s", dest_path)
    sha256_hash = hashlib.sha256()
    downloaded_bytes = 0

    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    http = session or requests

    try:
        try:
            with http.get(
                url,
                stream=True,
                timeout=(DOWNLOAD_CONNECT_TIMEOUT_S, DOWNLOAD_READ_TIMEOUT_S),
            ) as response:
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
                                    f"Download stream exceeded expected size limit of {expected_size} bytes.",
                                    attempt_id=attempt_id,
                                )

        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response is not None else "Unknown"
            raise VeritasRunnerError(
                StatusClass.DOWNLOAD_FAILED,
                f"HTTP error {status_code} while downloading file.",
                attempt_id=attempt_id,
            ) from e
        except requests.exceptions.Timeout as e:
            raise VeritasRunnerError(
                StatusClass.DOWNLOAD_FAILED,
                "Timed out while downloading asset file.",
                attempt_id=attempt_id,
            ) from e
        except requests.exceptions.RequestException as e:
            raise VeritasRunnerError(
                StatusClass.DOWNLOAD_FAILED,
                f"Network transport failure during asset download: {type(e).__name__}.",
                attempt_id=attempt_id,
            ) from e

        # Size Verification
        if expected_size is not None and downloaded_bytes != expected_size:
            raise VeritasRunnerError(
                StatusClass.DOWNLOAD_FAILED,
                f"Size mismatch for {dest_path}: expected {expected_size} bytes, got {downloaded_bytes}.",
                attempt_id=attempt_id,
            )

        # Mandatory Cryptographic Integrity Check
        computed_sha256 = sha256_hash.hexdigest()
        if computed_sha256.lower() != expected_sha256.lower():
            raise VeritasRunnerError(
                StatusClass.CHECKSUM_MISMATCH,
                f"SHA-256 mismatch for asset. Expected {expected_sha256[:12]}..., got {computed_sha256[:12]}...",
                attempt_id=attempt_id,
            )

    except Exception:
        # Guarantee partial/corrupted file cleanup on ANY exception
        if os.path.exists(dest_path):
            try:
                os.remove(dest_path)
            except OSError as cleanup_err:
                logger.warning("Failed to clean up partial file %s: %s", dest_path, cleanup_err)
        raise


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