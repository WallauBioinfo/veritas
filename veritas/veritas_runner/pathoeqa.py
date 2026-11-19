# veritas_runner/pathoeqa.py
#
# This module is the PathoEQA control plane: fetch the manifest for an
# ExecutionAttempt and post callbacks about it.

from __future__ import annotations

import logging
import os
from typing import Optional, Final

import icontract
import requests
from pydantic import ValidationError

from .datamodels import CallbackEnvelope, Manifest, ResultMetadata
from .exceptions import VeritasRunnerError, ErrorFactory, HttpSurface
from .status import StatusClass

logger = logging.getLogger(__name__)

MAX_MANIFEST_BYTES = int(os.environ.get("VERITAS_MANIFEST_MAX_BYTES", 256 * 1024))
MAX_CALLBACK_BYTES = int(os.environ.get("VERITAS_CALLBACK_MAX_BYTES", 1024 * 1024))
CONNECT_TIMEOUT_S = float(os.environ.get("VERITAS_HTTP_CONNECT_TIMEOUT", 10))
READ_TIMEOUT_S = float(os.environ.get("VERITAS_HTTP_READ_TIMEOUT", 15))
SUPPORTED_MANIFEST_SCHEMA_VERSIONS = frozenset({"2.0"})


class PathoEQAClient:
    """
    Attempt-scoped HTTP client for the PathoEQA control plane.

    Manages HTTP communication: manifest retrieval and status callbacks
    for a single execution attempt over a shared, pooled TLS session.

    Contract & Responsibilities:
    ----------------------------
    - Scope: Bound 1:1 to the lifecycle of a single ExecutionAttempt run.
    - Resource Management: Reuses a single `requests.Session` connection pool across
      all endpoints to minimize TCP/TLS handshake overhead.
    - The underlying `requests.Session` connection pool remains open until explicit instance disposal.


    Preconditions:
    ----------------------------------
    - `api_url`, `attempt_id` and `oidc_token` are non-empty, pre-validated strings.

    Class Invariants:
    -----------------
    - `api_url` and `attempt_id` `Final` attributes and cannot be reassigned.

    Exception & Failure Contract:
    -----------------------------
    - All network, HTTP, or parsing failures are translated into `VeritasRunnerError`.
    - Every raised exception guarantees a structured `StatusClass` attribute and `attempt_id`.
    - Transient failures (e.g. connection drops, 50x status codes, timeouts) set
      `status_class.transient == True`, signaling to the orchestrator that a retry
      attempt may be executed.
    """

    api_url: Final[str]
    attempt_id: Final[str]

    def __init__(
        self,
        api_url: str,
        oidc_token: str,
        attempt_id: str,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.attempt_id = attempt_id
        self._error = ErrorFactory(attempt_id=attempt_id)
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {oidc_token}"
            }
        )


    # ---------------------------------------------------------------- manifest

    @icontract.ensure(
        lambda result: result.schema_version in SUPPORTED_MANIFEST_SCHEMA_VERSIONS,
        "Manifest schema version is unsupported",
        error=lambda result, self: self._error(
            StatusClass.MANIFEST_INVALID,
            f"Unsupported schema_version '{result.schema_version}'; "
            f"this runner supports: {sorted(SUPPORTED_MANIFEST_SCHEMA_VERSIONS)}.",
        ),
    )

    def fetch_manifest(self) -> Manifest:
        """
        GET /attempts/{attempt_id}/manifest.

        Buffer the entirety of the Manifest JSON and validate its size (SPEC cap: 256 KB).
        """
        url = f"{self.api_url}/attempts/{self.attempt_id}/manifest"
        logger.info("Fetching manifest for attempt_id=%s", self.attempt_id)

        try:
            response = self.session.get(
                url,
                timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
            )
            self._error.raise_for_http(
                response.status_code,
                HttpSurface.CONTROL_PLANE,
                context="manifest fetch"
            )
            
            raw_bytes = response.content
            if len(raw_bytes) > MAX_MANIFEST_BYTES:
                raise self._error(
                    StatusClass.MANIFEST_INVALID,
                    f"Manifest payload ({len(raw_bytes)} bytes) exceeds limit of {MAX_MANIFEST_BYTES} bytes.",
                )    
        except VeritasRunnerError:
            raise
        except requests.exceptions.Timeout as e:
            raise self._error(
                StatusClass.UPSTREAM_UNAVAILABLE,
                "PathoEQA timed out during manifest fetch.",
            ) from e
        except requests.exceptions.ConnectionError as e:
            raise self._error(
                StatusClass.UPSTREAM_UNAVAILABLE,
                "Could not reach PathoEQA for manifest fetch.",
            ) from e
        except requests.exceptions.InvalidURL as e:
            raise self._error(
                StatusClass.CONFIG_ERROR, f"Malformed PathoEQA API URL: {e}"
            ) from e
        except requests.exceptions.RequestException as e:
            raise self._error(
                StatusClass.UPSTREAM_UNAVAILABLE,
                f"Transport failure during manifest fetch: {type(e).__name__}",
            ) from e

        return self._parse_manifest(raw_bytes)

    def _parse_manifest(self, raw: bytes) -> Manifest:
        try:
            return Manifest.model_validate_json(raw)
        except ValidationError as e:
            raise self._error(
                StatusClass.MANIFEST_INVALID,
                f"Manifest failed schema validation ({e.error_count()} error(s)).",
            ) from e
        except ValueError as e:
            raise self._error(
                StatusClass.MANIFEST_INVALID, f"Manifest is not valid JSON: {e}"
            ) from e
        except Exception as e:
            raise self._error(
                StatusClass.INTERNAL_ERROR,
                f"Internal failure parsing manifest: {type(e).__name__}",
            ) from e

    # ---------------------------------------------------------------- callback

    @icontract.require(
        lambda envelope, self: envelope.attempt_id == self.attempt_id,
        "Callback envelope attempt_id must match client attempt_id",
        error=lambda envelope, self: self._error(
            StatusClass.CALLBACK_INVALID,
            f"Callback attempt_id '{envelope.attempt_id}' does not match client attempt_id '{self.attempt_id}'.",
        ),
    )
    def send_callback(self, envelope: CallbackEnvelope) -> None:
        """
        POST /callbacks - Send a status or progress event to PathoEQA.

        Executes a single HTTP attempt without internal retries. Fails fast by 
        raising an exception on transport or server failure, halting execution.

        Idempotency Contract (VERITAS-003):
        ----------------------------------
        - Attaches `envelope.event_id` as the HTTP `Idempotency-Key` header.
        - Guarantees that if a network timeout drops the server's response, the caller
          can safely re-transmit the same envelope without causing duplicate server-side
          state updates or metric double-counting.

        Preconditions:
        --------------
        - `envelope.attempt_id` must match `self.attempt_id`.
        - Marshaled payload must not exceed `MAX_CALLBACK_BYTES`.
        """
        url = f"{self.api_url}/callbacks"
        body = envelope.model_dump_json(exclude_none=True).encode("utf-8")

        if len(body) > MAX_CALLBACK_BYTES:
            raise self._error(
                StatusClass.CALLBACK_INVALID,
                f"Callback payload ({len(body)} bytes) exceeds the "
                f"{MAX_CALLBACK_BYTES} byte cap; truncate payload before sending.",
            )

        logger.info(
            "Callback event_type=%s attempt_id=%s sample_run_id=%s event_id=%s",
            envelope.event_type,
            envelope.attempt_id,
            envelope.sample_run_id,
            envelope.event_id,
        )

        try:
            response = self.session.post(
                url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Idempotency-Key": envelope.event_id,
                },
                timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
            )
        except (
            requests.exceptions.SSLError,
            requests.exceptions.InvalidURL,
            requests.exceptions.MissingSchema,
            requests.exceptions.InvalidHeader,
        ) as e:
            raise self._error(
                StatusClass.CALLBACK_INVALID,
                f"Invalid callback configuration or SSL failure: {type(e).__name__} - {e}",
            ) from e
        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
        ) as e:
            raise self._error(
                StatusClass.CALLBACK_FAILED,
                f"Callback network transport failure: {type(e).__name__}",
            ) from e
        except requests.exceptions.RequestException as e:
            raise self._error(
                StatusClass.CALLBACK_INVALID,
                f"Callback request failed: {type(e).__name__} - {e}",
            ) from e
        self._error.raise_for_http(
            response.status_code,
            HttpSurface.CALLBACK,
            context="status callback"
        )

    # -------------------------------------------------- send results
    def send_result(
        self,
        sample_run_id: str,
        metrics: bytes,
        metadata: ResultMetadata,
    ) -> None:
        """
        POST /attempts/{attempt_id}/samples/{sample_run_id}/results - Deliver one sample's metrics TSV.

        Sends a two-part multipart request (a ``metadata`` JSON field and a ``metrics``
        file). Executes a single HTTP attempt without internal retries; the caller wraps
        it in the retry helper. `metadata.event_id` is also the `Idempotency-Key`, so a
        resend after a lost response is safe.

        Parameters
        ----------
        sample_run_id : str
            Sample run the metrics belong to.
        metrics : bytes
            Exact bytes of the sample's ``metrics.tsv``; PathoEQA hashes them, so
            they must not be re-encoded.
        metadata : ResultMetadata
            Validated delivery metadata built from `metrics`.

        Raises
        ------
        VeritasRunnerError
            On transport failure, on an HTTP error response, or if the response
            does not confirm the delivered metrics.
        """
        url = f"{self.api_url}/attempts/{self.attempt_id}/samples/{sample_run_id}/results"
        logger.info(
            "Result delivery attempt_id=%s sample_run_id=%s event_id=%s",
            self.attempt_id,
            sample_run_id,
            metadata.event_id,
        )

        try:
            response = self.session.post(
                url,
                data={"metadata": metadata.model_dump_json()},
                files={"metrics": ("metrics.tsv", metrics, "text/tab-separated-values")},
                headers={"Idempotency-Key": metadata.event_id},
                timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
            )
        except (
            requests.exceptions.SSLError,
            requests.exceptions.InvalidURL,
            requests.exceptions.MissingSchema,
            requests.exceptions.InvalidHeader,
        ) as e:
            raise self._error(
                StatusClass.CALLBACK_INVALID,
                f"Invalid result delivery configuration or SSL failure: {type(e).__name__}",
            ) from e
        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
        ) as e:
            raise self._error(
                StatusClass.CALLBACK_FAILED,
                f"Result delivery network transport failure: {type(e).__name__}",
            ) from e
        except requests.exceptions.RequestException as e:
            raise self._error(
                StatusClass.CALLBACK_INVALID,
                f"Result delivery request failed: {type(e).__name__}",
            ) from e

        self._error.raise_for_http(
            response.status_code,
            HttpSurface.CALLBACK,
            context="result delivery",
        )

        try:
            body = response.json()
        except ValueError as e:
            raise self._error(StatusClass.CALLBACK_INVALID, "Result delivery returned a non-JSON body.") from e
        if body.get("status") != "applied" or body.get("sha256") != metadata.sha256:
            raise self._error(
                StatusClass.CALLBACK_INVALID,
                "Result delivery response did not confirm the delivered metrics.",
            )
    
    # -------------------------------------------------close session

    def close(self) -> None:
        """Closes the underlying requests HTTP session."""
        self.session.close()

    def __enter__(self) -> PathoEQAClient:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()