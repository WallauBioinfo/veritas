from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tarfile
import time
import zipfile
from typing import Optional
from pathlib import Path
import requests

from veritas_runner.datamodels import ManifestFile
from veritas_runner.exceptions import ErrorFactory, HttpSurface, VeritasRunnerError
from veritas_runner.status import StatusClass

logger = logging.getLogger(__name__)


class ArtefactClass:
    """Materialize manifest artefacts on disk per attempt.

    Owns three related methods:
      - `download`: stream a signed URL to disk, verified by size + SHA-256.
      - `prepare_rtg_sdf`: unpack a downloaded RTG SDF archive, or accept one
        already extracted.
      - `install_truth_vcf_index`: place a downloaded `.tbi` where tabix expects
        it, adjacent to the truth VCF it indexes.

    Parameters
    ----------
    session : requests.Session
        Active HTTP session used for streaming artifact downloads.
    attempt_id : str, optional
        Unique execution attempt identifier for log correlation and error tagging.
    connect_timeout_s : float, optional
        HTTP connection timeout in seconds. Defaults to `VERITAS_DOWNLOAD_CONNECT_TIMEOUT`
        env var or 10.0 seconds.
    read_timeout_s : float, optional
        HTTP read timeout in seconds. Defaults to `VERITAS_DOWNLOAD_READ_TIMEOUT`
        env var or 60.0 seconds.
    chunk_bytes : int, optional
        Chunk size in bytes when streaming responses to disk. Defaults to
        `VERITAS_DOWNLOAD_CHUNK_BYTES` env var or 4 MiB (4,194,304 bytes).

    Attributes
    ----------
    session : requests.Session
        HTTP session instance.
    attempt_id : str or None
        Attempt ID string if configured.
    connect_timeout_s : float
        Resolved HTTP connect timeout.
    read_timeout_s : float
        Resolved HTTP read timeout.
    chunk_bytes : int
        Resolved chunk buffer size in bytes.
    """

    _ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar", ".zip")

    DEFAULT_CONNECT_TIMEOUT_S: float = 10.0
    DEFAULT_READ_TIMEOUT_S: float = 60.0
    DEFAULT_CHUNK_BYTES: int = 4 * 1024 * 1024

    def __init__(
        self,
        session: requests.Session,
        attempt_id: Optional[str] = None,
        connect_timeout_s: Optional[float] = None,
        read_timeout_s: Optional[float] = None,
        chunk_bytes: Optional[int] = None,
    ) -> None:
        self.session = session
        self.attempt_id = attempt_id
        self._error = ErrorFactory(attempt_id=attempt_id)

        self.connect_timeout_s = (
            connect_timeout_s
            if connect_timeout_s is not None
            else float(os.environ.get("VERITAS_DOWNLOAD_CONNECT_TIMEOUT", self.DEFAULT_CONNECT_TIMEOUT_S))
        )
        self.read_timeout_s = (
            read_timeout_s
            if read_timeout_s is not None
            else float(os.environ.get("VERITAS_DOWNLOAD_READ_TIMEOUT", self.DEFAULT_READ_TIMEOUT_S))
        )
        self.chunk_bytes = (
            chunk_bytes
            if chunk_bytes is not None
            else int(os.environ.get("VERITAS_DOWNLOAD_CHUNK_BYTES", self.DEFAULT_CHUNK_BYTES))
        )

    @staticmethod
    def _check_stream_invariants(
        artefact: ManifestFile,
        written: int,
        deadline: Optional[float],
        fail: ErrorFactory,
    ) -> None:
        """Validate incoming artefact stream against size and wall-clock limits during download loop.

        Parameters
        ----------
        artefact : ManifestFile
            Manifest file entry.
        written : int
            Current byte count written.
        deadline : float or None
            Monotonic time limit threshold.
        fail : ErrorFactory
            Bound error factory instance.

        Raises
        ------
        VeritasRunnerError
            If written bytes exceed declared size or current monotonic time
            exceeds deadline.
        """
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

    @staticmethod
    def _verify_written(
        artefact: ManifestFile,
        written: int,
        digest: "hashlib._Hash",
        fail: ErrorFactory,
    ) -> None:
        """Verify completed download size and SHA-256 against manifest declared metadata.

        Parameters
        ----------
        artefact : ManifestFile
            Manifest file entry.
        written : int
            Total bytes written to disk.
        digest : hashlib._Hash
            Completed `sha256` hashing object.
        fail : ErrorFactory
            Bound error factory instance.

        Raises
        ------
        VeritasRunnerError
            If final byte count is truncated or computed SHA-256 does not match.
        """
        if artefact.size is not None and written < artefact.size:
            raise fail(
                StatusClass.CHECKSUM_MISMATCH,
                f"Truncated download: expected {artefact.size} bytes, received {written}.",
            )

        computed = digest.hexdigest()
        if computed != artefact.sha256:
            raise fail(
                StatusClass.CHECKSUM_MISMATCH,
                f"SHA-256 mismatch: expected {artefact.sha256[:12]}…, got {computed[:12]}….",
            )

    @classmethod
    def _extract_archive(
        cls, 
        archive: Path, 
        destination: Path, 
        lower: str, 
        fail: ErrorFactory) -> None:
        """Extract a `.zip` or `.tar` (including `.tar.gz` and `.tgz`) archive into target destination safely.

        Parameters
        ----------
        archive_path : Path
            Path to compressed archive on disk.
        destination_path : Path
            Target destination directory path.
        lower : str
            Lowercased filename string for extension checking.
        fail : ErrorFactory
            Bound error factory instance.

        Raises
        ------
        VeritasRunnerError
            If archive extraction fails or contains unsafe paths (zip-slip).
        """
        try:
            if lower.endswith(".zip"):
                with zipfile.ZipFile(archive) as zf:
                    cls._check_zip_members_safe(zf, destination, fail)
                    zf.extractall(destination)
            else:
                with tarfile.open(archive, "r:*") as tf:
                    tf.extractall(destination, filter="data") ## TODO: Require Python 3.12+
        except (OSError, tarfile.TarError, zipfile.BadZipFile) as e:
            raise fail(StatusClass.ARTEFACT_INVALID, 
            f"Could not extract archive '{archive.name}': {e}",) from e

    @staticmethod
    def _check_zip_members_safe(zf: zipfile.ZipFile, extract_dir: Path, fail: ErrorFactory) -> None:
        """Validate zip file entries against path traversal (Zip-Slip) vulnerability.
           If one file fails validation, the extraction is halted.

        Parameters
        ----------
        zf : zipfile.ZipFile
            Open ZipFile object.
        extract_dir : Path
            Root extraction directory path.
        fail : ErrorFactory
            Bound error factory instance.

        Raises
        ------
        VeritasRunnerError
            If any archive member attempts to resolve outside `extract_dir`.
        """
        for member in zf.namelist():
            member_path = (extract_dir / member.lstrip("/\\")).resolve()
            if not member_path.is_relative_to(extract_dir):
                raise fail(StatusClass.ARTEFACT_INVALID, f"Unsafe zip member path: {member}")

    @staticmethod
    def _resolve_extracted_dir(extraction_dir: Path) -> Path:
        """Unwrap single root directory inside extraction directory, if present.

        Parameters
        ----------
        extraction_dir : Path
            Resolved extraction directory path.

        Returns
        -------
        Path
            Single first child directory path if present, otherwise `extraction_dir`.
        """
        entries = [e for e in extraction_dir.iterdir() if not e.name.startswith(".")]
        if len(entries) == 1 and entries[0].is_dir():
            return entries[0]
        return extraction_dir

    # ------------------------------------------------------------- download

    def download(
        self,
        artefact: ManifestFile,
        dest_path: Path,
        deadline: Optional[float] = None,
    ) -> str:
        """Stream one manifest artefact to disk, hashing as it goes.

        Writes to `<dest_path>.part` and renames only after size and SHA-256
        both verify.

        Skips the HTTP transfer entirely when `dest_path` already exists with
        a matching SHA-256 digest.

        Parameters
        ----------
        artefact : ManifestFile
            Manifest metadata entry containing target URL, expected SHA-256,
            and size.
        dest_path : str
            Final destination filesystem path for the downloaded file.
        deadline : float, optional
            Monotonic time limit (`time.monotonic()`). Checked between chunks so
            a slow transfer aborts as `DEADLINE_EXCEEDED` rather than hitting
            a hard process timeout.

        Returns
        -------
        Path
            The verified path to the downloaded file (`dest_path`).

        Raises
        ------
        VeritasRunnerError
            If HTTP transfer fails, checksum or size mismatches, deadline is
            exceeded, or filesystem write fails.
        """
        fail = self._error.bind(prefix=f"role={artefact.role}")
        dest_path = Path(dest_path)

        if self._is_cached(dest_path, artefact.sha256):
            logger.info("Skipping download role=%s; cached file verified at %s", artefact.role, dest_path)
            return dest_path

        tmp_path = dest_path.parent / f"{dest_path}.part"
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Downloading role=%s -> %s", artefact.role, dest_path)

        try:
            written, digest = self._stream_to_disk(artefact, tmp_path, deadline, fail)
            self._verify_written(artefact, written, digest, fail)
            tmp_path.replace(dest_path)
            logger.info("Verified role=%s (%d bytes)", artefact.role, written)
            return dest_path
        except Exception:
            self._discard_partial(tmp_path)
            raise

    def _is_cached(self, dest_path: Path, expected_sha256: str) -> bool:
        """Check if target path already exists on disk with matching content hash.

        Parameters
        ----------
        dest_path : Path
            Path to check on disk.
        expected_sha256 : str
            Expected SHA-256 hex digest string.

        Returns
        -------
        bool
            `True` if file exists and SHA-256 digest matches `expected_sha256`,
            `False` otherwise.
        """
        return dest_path.is_file() and self._file_sha256(dest_path) == expected_sha256

    def _stream_to_disk(
        self,
        artefact: ManifestFile,
        tmp_path: str,
        deadline: Optional[float],
        fail: ErrorFactory,
    ) -> tuple[int, "hashlib._Hash"]:
        """Perform the HTTP GET request and write response body to disk chunk by chunk.

        Parameters
        ----------
        artefact : ManifestFile
            Manifest metadata entry describing target artefact URL and size.
        tmp_path : str
            Temporary `.part` path to store in-progress download bytes.
        deadline : float or None
            Monotonic time limit threshold.
        fail : ErrorFactory
            Bound error factory for contextual exception raising.

        Returns
        -------
        tuple of (int, hashlib._Hash)
            A 2-element tuple containing:
            - `written` : Total count of bytes written to disk.
            - `digest` : Update-in-progress `hashlib` SHA-256 object.

        Raises
        ------
        VeritasRunnerError
            On HTTP error response, mid-transfer timeout, transport failure,
            or OS write error.
        """
        digest = hashlib.sha256()
        written = 0

        try:
            with self.session.get(
                artefact.url,
                stream=True,
                timeout=(self.connect_timeout_s, self.read_timeout_s),
                # Identity encoding is mandatory: if a proxy gzips the stream,
                # requests transparently inflates it and the SHA-256 we compute
                # no longer matches the one frozen in manifest.
                headers={"Accept-Encoding": "identity"},
                # Signed URLs carry their own auth; never leak OIDC bearer tokens.
                auth=None,
            ) as response:
                if response.status_code >= 400:
                    fail.raise_for_http(response.status_code, HttpSurface.ARTEFACT, context="artefact download")

                with open(tmp_path, "wb") as fh:
                    for chunk in response.iter_content(chunk_size=self.chunk_bytes):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        digest.update(chunk)
                        written += len(chunk)
                        self._check_stream_invariants(artefact, written, deadline, fail)
                    fh.flush()
                    os.fsync(fh.fileno())

        except VeritasRunnerError:
            raise
        except requests.exceptions.Timeout as e:
            raise fail(StatusClass.DOWNLOAD_FAILED, "Timed out mid-transfer.") from e
        except requests.exceptions.RequestException as e:
            raise fail(StatusClass.DOWNLOAD_FAILED, f"Transport failure: {type(e).__name__}") from e
        except OSError as e:
            raise fail(StatusClass.CONFIG_ERROR, f"Cannot write to disk: {e}") from e

        return written, digest

    
    # ---------------------------------------------------------- rtg sdf prep

    def prepare_rtg_sdf(
        self, 
        downloaded_path: str | Path, 
        extract_dir: str | Path) -> str:
        """Ensure `--rtg-reference` points at a valid RTG SDF directory structure.

        Accepts an already-extracted directory or an archive (`.tar.gz`, `.tgz`,
        `.tar`, `.zip`). Verifies presence of the `mainIndex` descriptor file required
        by RTG tools.

        Parameters
        ----------
        downloaded_path : str | Path
            Path to an extracted directory or downloaded archive file.
        extract_dir : str | Path
            Target extraction directory path when unpacking archives.

        Returns
        -------
        Path
            Verified absolute Path to the prepared RTG SDF directory.

        Raises
        ------
        VeritasRunnerError
            If directory is empty, archive format is unknown, archive unpacking
            fails, or extracted result is missing `mainIndex`.
        """
        fail = self._error.bind(prefix="rtg_sdf")
        archive = Path(downloaded_path)
        destination = Path(extract_dir).resolve()

        if archive.is_dir():
            if not (archive / "mainIndex").is_file():
                raise fail(
                    StatusClass.ARTEFACT_INVALID,
                    f"RTG SDF directory missing required 'mainIndex': {archive}",
                )
            return archive.resolve()

        lower = archive.name.lower()
        if not any(lower.endswith(suffix) for suffix in self._ARCHIVE_SUFFIXES):
            raise fail(
                StatusClass.ARTEFACT_INVALID,
                f"rtg_sdf must be a directory or archive, got file: {archive}",
            )

        if destination.exists():
            shutil.rmtree(destination)
        os.makedirs(destination, exist_ok=True)

        self._extract_archive(archive, destination, lower, fail)

        sdf_dir = self._resolve_extracted_dir(destination)
        if not sdf_dir.is_dir() or not (sdf_dir / "mainIndex").is_file():
            raise fail(
                StatusClass.ARTEFACT_INVALID,
                f"Extracted RTG SDF directory is missing 'mainIndex': {sdf_dir}",
            )
        return sdf_dir ## TODO: refactor runner.py to take in sdf_dir as Path

    
    # -------------------------------------------------------- truth vcf index

    def install_truth_vcf_index(self, truth_vcf_path: str, tbi_path: str) -> None:
        """Place the truth VCF tabix index adjacent to the target VCF file.

        Parameters
        ----------
        truth_vcf_path : str
            Path to the truth VCF file (`.vcf.gz`).
        tbi_path : str
            Path to downloaded tabix index file (`.tbi`).
        """
        expected = f"{truth_vcf_path}.tbi"
        if os.path.abspath(tbi_path) == os.path.abspath(expected):
            return
        if os.path.exists(expected):
            os.remove(expected)
        shutil.copy2(tbi_path, expected)


    @staticmethod
    def _file_sha256(path: str) -> str:
        """Compute SHA-256 hex digest of an existing file using constant memory.

        Parameters
        ----------
        path : str
            File path on disk.

        Returns
        -------
        str
            Calculated SHA-256 hex string.
        """
        with open(path, "rb") as fh:
            return hashlib.file_digest(fh, "sha256").hexdigest()

    @staticmethod
    def _discard_partial(path: Path) -> None:
        """Silently attempt to clean up temporary .part download artifacts on error.

        Parameters
        ----------
        path : Path
            Path to partial file to remove.
        """
        try:
            path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("Could not remove partial file %s: %s", path, e)