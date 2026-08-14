# veritas_runner/artefacts.py

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tarfile
import time
import zipfile
from typing import Optional

import requests

from veritas_runner.datamodels import ManifestFile
from veritas_runner.exceptions import HttpFailureClassifier, HttpSurface, VeritasRunnerError
from veritas_runner.status import StatusClass

logger = logging.getLogger(__name__)

DOWNLOAD_CONNECT_TIMEOUT_S = float(os.environ.get("VERITAS_DOWNLOAD_CONNECT_TIMEOUT", 10))
DOWNLOAD_READ_TIMEOUT_S = float(os.environ.get("VERITAS_DOWNLOAD_READ_TIMEOUT", 60))
CHUNK_BYTES = int(os.environ.get("VERITAS_DOWNLOAD_CHUNK_BYTES", 4 * 1024 * 1024))
_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar", ".zip")


import hashlib


def _file_sha256(path: str) -> str:
    """
    Calculate the SHA-256 hexadecimal digest of an existing file on disk.
    
    Verifies local file integrity and validates cache hits. Uses
    `hashlib.file_digest` for C-level chunked hashing, maintaining $O(1)$
    memory consumption regardless of file size while letting CPython manage
    buffer sizing internally.

    Parameters
    ----------
    path : str
        Absolute or relative path to the file on disk.

    Returns
    -------
    str
        64-character lowercase hexadecimal SHA-256 digest string.

    Raises
    ------
    OSError
        If the file does not exist or lacks read permissions.
    """
    with open(path, "rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def _resolve_extracted_dir(extract_to: str) -> str:
    """
    Unwrap a single top-level directory resulting from archive extraction.

    Inspects the extraction directory while ignoring hidden files (e.g., 
    `.DS_Store` or `.__MACOSX`). If extraction yielded exactly one top-level 
    directory, that nested directory's path is returned as the resolved root.
    Otherwise, the original `extract_to` path is returned.

    Parameters
    ----------
    extract_to : str
        Path to the target directory where the archive was extracted.

    Returns
    -------
    str
        Path to the single nested sub-directory if one was produced by 
        extraction; otherwise, `extract_to`.
    """
    entries = [e for e in os.listdir(extract_to) if not e.startswith(".")]
    if len(entries) == 1:
        sole = os.path.join(extract_to, entries[0])
        if os.path.isdir(sole):
            return sole
    return extract_to


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

    Skips the HTTP transfer when `dest_path` already exists with a matching
    SHA-256.

    `deadline` is a time.monotonic() value. Checked between chunks so a slow
    transfer aborts as DEADLINE_EXCEEDED against the operational budget rather
    than being killed at the 90-minute hard timeout with nothing reported.
    """
    if os.path.isfile(dest_path) and _file_sha256(dest_path) == artefact.sha256:
        logger.info("Skipping download role=%s; cached file verified at %s", artefact.role, dest_path)
        return dest_path

    tmp_path = f"{dest_path}.part" # atomic staging file
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
                    failure_class = HttpFailureClassifier.classify(
                        response.status_code, HttpSurface.ARTEFACT
                    )
                    raise fail(
                        failure_class or StatusClass.DOWNLOAD_FAILED,
                        f"HTTP {response.status_code} fetching artefact.",
                    )

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
        if computed != artefact.sha256:
            raise fail(
                StatusClass.CHECKSUM_MISMATCH,
                f"SHA-256 mismatch: expected {artefact.sha256[:12]}…, "
                f"got {computed[:12]}….",
            )

        os.replace(tmp_path, dest_path)
        logger.info("Verified role=%s (%d bytes)", artefact.role, written)
        return dest_path

    except Exception:
        _discard_partial(tmp_path)
        raise


def prepare_rtg_sdf(
    downloaded_path: str,
    extract_dir: str,
    *,
    attempt_id: Optional[str] = None,
) -> str:
    """
    Ensure `--rtg-reference` points at an RTG SDF directory.

    Accepts an already-extracted directory or an archive (.tar.gz, .tgz, .tar, .zip).
    """
    if os.path.isdir(downloaded_path):
        if not os.listdir(downloaded_path):
            raise VeritasRunnerError(
                StatusClass.ARTEFACT_INVALID,
                f"RTG SDF directory is empty: {downloaded_path}",
                attempt_id=attempt_id,
            )
        return downloaded_path

    lower = downloaded_path.lower()
    if not any(lower.endswith(suffix) for suffix in _ARCHIVE_SUFFIXES):
        raise VeritasRunnerError(
            StatusClass.ARTEFACT_INVALID,
            f"rtg_sdf must be a directory or archive, got file: {downloaded_path}",
            attempt_id=attempt_id,
        )

    if os.path.exists(extract_dir):
        shutil.rmtree(extract_dir)
    os.makedirs(extract_dir, exist_ok=True)

    try:
        if lower.endswith(".zip"):
            with zipfile.ZipFile(downloaded_path) as zf:
                zf.extractall(extract_dir)
        else:
            with tarfile.open(downloaded_path, "r:*") as tf:
                tf.extractall(extract_dir, filter="data")
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as e:
        raise VeritasRunnerError(
            StatusClass.ARTEFACT_INVALID,
            f"Could not extract rtg_sdf archive: {e}",
            attempt_id=attempt_id,
        ) from e

    sdf_dir = _resolve_extracted_dir(extract_dir)
    if not os.path.isdir(sdf_dir) or not os.listdir(sdf_dir):
        raise VeritasRunnerError(
            StatusClass.ARTEFACT_INVALID,
            f"Extracted RTG SDF directory is empty: {sdf_dir}",
            attempt_id=attempt_id,
        )
    return sdf_dir


def install_truth_vcf_index(truth_vcf_path: str, tbi_path: str) -> None:
    """
    Place the truth VCF tabix index where pysam/rtg expect it: adjacent to the VCF.
    """
    expected = f"{truth_vcf_path}.tbi"
    if os.path.abspath(tbi_path) == os.path.abspath(expected):
        return
    if os.path.exists(expected):
        os.remove(expected)
    shutil.copy2(tbi_path, expected)


def _discard_partial(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("Could not remove partial file %s: %s", path, e)
''