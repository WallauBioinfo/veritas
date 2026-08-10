# veritas_runner/artefacts.py
#
# Split out of the old client.py. Downloading a frozen artefact from a signed
# URL is not a PathoEQA control-plane call - the URL may point at object
# storage, and none of the manifest/callback semantics apply. Different
# timeouts, different size profile, different failure surface.
#
# Like pathoeqa.py, nothing here retries. One call, one attempt, classified error.

from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Optional

import requests

from veritas_runner.status import StatusClass
from veritas_runner.datamodels import ManifestFile
from veritas_runner.exceptions import VeritasRunnerError

logger = logging.getLogger(__name__)

DOWNLOAD_CONNECT_TIMEOUT_S = float(os.environ.get("VERITAS_DOWNLOAD_CONNECT_TIMEOUT", 10))
DOWNLOAD_READ_TIMEOUT_S = float(os.environ.get("VERITAS_DOWNLOAD_READ_TIMEOUT", 60))

# 4 MiB. The old 8 KiB meant ~131k Python-level iterations per GB, all of it
# interpreter overhead on multi-GB references.
CHUNK_BYTES = int(os.environ.get("VERITAS_DOWNLOAD_CHUNK_BYTES", 4 * 1024 * 1024))


def download_artefact(
    artefact: ManifestFile,
    dest_path: str,
    session: requests.Session,
    attempt_id: Optional[str] = None,
    deadline: Optional[float] = None,
) -> str:
    """
    Stream one manifest artefact to disk, hashing as it goes.

    Writes to `<dest_path>.part` and renames only after size and SHA-256 both
    verify, so a truncated or corrupt file can never be mistaken for a good one
    by a later continuation.

    `deadline` is a time.monotonic() value. Checked between chunks so a slow
    transfer aborts as DEADLINE_EXCEEDED against the operational budget rather
    than being killed at the 90-minute hard timeout with nothing reported.
    """
    tmp_path = f"{dest_path}.part"
    digest = hashlib.sha256()
    written = 0

    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    logger.info("Downloading role=%s -> %s", artefact.role, dest_path)

    def fail(failure_class: StatusClass, message: str) -> VeritasRunnerError:
        return VeritasRunnerError(
            failure_class=failure_class,
            message=f"[role={artefact.role}] {message}",
            attempt_id=attempt_id,
        )

    try:
        try:
            with session.get(
                artefact.url,
                stream=True,
                timeout=(DOWNLOAD_CONNECT_TIMEOUT_S, DOWNLOAD_READ_TIMEOUT_S),
                # Identity encoding is mandatory: if a proxy gzips the stream,
                # requests transparently inflates it and the SHA-256 we compute
                # no longer matches the one PathoEQA froze.
                headers={"Accept-Encoding": "identity"},
                # Signed URLs carry their own auth; never leak the OIDC bearer
                # to an object-storage host.
                auth=None,
            ) as response:
                if response.status_code >= 400:
                    cls = (
                        StatusClass.DOWNLOAD_FAILED
                        if response.status_code in (408, 429) or response.status_code >= 500
                        else StatusClass.INVALID_INPUT
                    )
                    raise fail(cls, f"HTTP {response.status_code} fetching artefact.")

                with open(tmp_path, "wb") as fh:
                    for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        digest.update(chunk)
                        written += len(chunk)

                        if artefact.size is not None and written > artefact.size:
                            raise fail(
                                StatusClass.CHECKSUM_MISMATCH,
                                f"Stream exceeded declared size of {artefact.size} bytes.",
                            )
                        if deadline is not None and time.monotonic() > deadline:
                            raise fail(
                                StatusClass.DEADLINE_EXCEEDED,
                                f"Operational deadline hit after {written} bytes.",
                            )
                    fh.flush()
                    os.fsync(fh.fileno())

        except VeritasRunnerError:
            raise
        except requests.exceptions.Timeout as e:
            raise fail(StatusClass.DOWNLOAD_FAILED, "Timed out mid-transfer.") from e
        except requests.exceptions.RequestException as e:
            raise fail(
                StatusClass.DOWNLOAD_FAILED, f"Transport failure: {type(e).__name__}"
            ) from e
        except OSError as e:
            raise fail(StatusClass.CONFIG_ERROR, f"Cannot write to disk: {e}") from e

        if artefact.size is not None and written != artefact.size:
            raise fail(
                StatusClass.CHECKSUM_MISMATCH,
                f"Size mismatch: declared {artefact.size} bytes, received {written}.",
            )

        computed = digest.hexdigest()
        if computed != artefact.sha256.lower():
            raise fail(
                StatusClass.CHECKSUM_MISMATCH,
                f"SHA-256 mismatch: expected {artefact.sha256[:12]}…, "
                f"got {computed[:12]}….",
            )

        os.replace(tmp_path, dest_path)
        logger.info("Verified role=%s (%d bytes)", artefact.role, written)
        return dest_path

    except Exception:
        _discard(tmp_path)
        raise


def _discard(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("Could not remove partial file %s: %s", path, e)
