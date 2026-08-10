# veritas_runner/pathoeqa.py
#
# Renamed from client.py. "client" said nothing about *whose* client it is,
# and the file had drifted into holding three unrelated concerns: the PathoEQA
# control-plane API, bulk artefact downloads, and env-var config.
#
# This module is now only the PathoEQA control plane: fetch the manifest for an
# ExecutionAttempt, post callbacks about it. Bulk artefact transfer lives in
# artefacts.py. Retry policy lives in runner.py - every function here performs
# exactly ONE HTTP request and raises a classified error. That separation is
# deliberate: the specs' "duas tentativas automáticas" is a policy decision
# about a transient operation, not a property of the endpoint.

from __future__ import annotations

import logging
import os
from typing import Optional

import requests
from pydantic import ValidationError

from veritas_runner.status import StatusClass
from veritas_runner.datamodels import CallbackEnvelope, Manifest
from veritas_runner.exceptions import VeritasRunnerError

logger = logging.getLogger(__name__)

MAX_MANIFEST_BYTES = int(os.environ.get("VERITAS_MANIFEST_MAX_BYTES", 256 * 1024))
MAX_CALLBACK_BYTES = int(os.environ.get("VERITAS_CALLBACK_MAX_BYTES", 1024 * 1024))
CONNECT_TIMEOUT_S = float(os.environ.get("VERITAS_HTTP_CONNECT_TIMEOUT", 10))
READ_TIMEOUT_S = float(os.environ.get("VERITAS_HTTP_READ_TIMEOUT", 15))
SUPPORTED_SCHEMA_MAJOR = os.environ.get("VERITAS_SUPPORTED_SCHEMA_MAJOR", "1")


class PathoEQAClient:
    """
    Single-shot HTTP client for the PathoEQA control plane.

    One instance per ExecutionAttempt, holding one requests.Session so the
    manifest fetch, the artefact downloads and every callback reuse the same
    TLS connection pool.

    No method here retries. No method here has a urllib3 Retry adapter mounted.
    If a call fails transiently it raises with a StatusClass whose `.transient`
    is True, and the caller decides whether to try again.
    """

    def __init__(
        self,
        api_url: str,
        oidc_token: str,
        attempt_id: str,
        session: Optional[requests.Session] = None,
    ) -> None:
        if not api_url:
            raise VeritasRunnerError(
                failure_class=StatusClass.CONFIG_ERROR,
                message="PATHOEQA_API_URL is not configured.",
                attempt_id=attempt_id,
            )
        if not oidc_token:
            raise VeritasRunnerError(
                failure_class=StatusClass.CONFIG_ERROR,
                message="No GitHub OIDC token available for PathoEQA calls.",
                attempt_id=attempt_id,
            )

        self.api_url = api_url.rstrip("/")
        self.attempt_id = attempt_id
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {oidc_token}",
                "Accept": "application/json",
                "User-Agent": "veritas-runner",
            }
        )

    # ---------------------------------------------------------------- manifest

    def fetch_manifest(self) -> Manifest:
        """
        GET /attempts/{attempt_id}/manifest - one request, no retry.

        Streams the body so an oversized payload is rejected on the wire rather
        than after buffering it whole (SPEC cap: 256 KB).
        """
        url = f"{self.api_url}/attempts/{self.attempt_id}/manifest"
        logger.info("Fetching manifest for attempt_id=%s", self.attempt_id)

        try:
            with self.session.get(
                url,
                timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
                stream=True,
            ) as response:
                self._raise_for_status(response.status_code)
                raw = self._read_capped(response, MAX_MANIFEST_BYTES, "Manifest")
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

        return self._parse_manifest(raw)

    def _parse_manifest(self, raw: bytes) -> Manifest:
        try:
            manifest = Manifest.model_validate_json(raw)
        except ValidationError as e:
            raise self._error(
                StatusClass.MANIFEST_INVALID,
                f"Manifest failed schema validation ({e.error_count()} error(s)).",
            ) from e
        except ValueError as e:
            # model_validate_json raises ValueError for malformed JSON too.
            raise self._error(
                StatusClass.MANIFEST_INVALID, f"Manifest is not valid JSON: {e}"
            ) from e
        except Exception as e:
            raise self._error(
                StatusClass.INTERNAL_ERROR,
                f"Internal failure parsing manifest: {type(e).__name__}",
            ) from e

        major = manifest.schema_version.split(".", 1)[0]
        if major != SUPPORTED_SCHEMA_MAJOR:
            raise self._error(
                StatusClass.MANIFEST_INVALID,
                f"Unsupported schema_version '{manifest.schema_version}'; "
                f"this runner speaks major version {SUPPORTED_SCHEMA_MAJOR}.",
            )

        if manifest.attempt_id != self.attempt_id:
            raise self._error(
                StatusClass.MANIFEST_INVALID,
                "Manifest attempt_id does not match the dispatched attempt_id.",
            )

        return manifest

    # ---------------------------------------------------------------- callback

    def send_callback(self, envelope: CallbackEnvelope) -> None:
        """
        POST /callbacks - one request, no retry.

        Raises rather than swallowing: the caller owns delivery policy, and a
        silently dropped callback is how an attempt becomes `Stale`.
        `event_id` makes the POST idempotent, so a retry by the caller cannot
        duplicate metrics (VERITAS-003).
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
        except requests.exceptions.RequestException as e:
            raise self._error(
                StatusClass.CALLBACK_FAILED,
                f"Callback transport failure: {type(e).__name__}",
            ) from e

        if response.status_code in (409, 422):
            # PathoEQA rejected the shape, or the attempt is already terminal.
            # Not transient - retrying cannot change the answer.
            raise self._error(
                StatusClass.CALLBACK_INVALID,
                f"PathoEQA rejected callback with HTTP {response.status_code}.",
            )
        if response.status_code >= 400:
            raise self._error(
                StatusClass.CALLBACK_FAILED,
                f"Callback rejected with HTTP {response.status_code}.",
            )

    # ----------------------------------------------------------------- helpers

    def _error(self, failure_class: StatusClass, message: str) -> VeritasRunnerError:
        return VeritasRunnerError(
            failure_class=failure_class, message=message, attempt_id=self.attempt_id
        )

    def _raise_for_status(self, status_code: int) -> None:
        if status_code < 400:
            return
        if status_code in (401, 403):
            raise self._error(
                StatusClass.AUTH_REJECTED,
                f"PathoEQA rejected the OIDC token (HTTP {status_code}).",
            )
        if status_code == 404:
            raise self._error(
                StatusClass.ATTEMPT_NOT_FOUND,
                "PathoEQA has no such attempt (HTTP 404).",
            )
        if status_code in (408, 429) or status_code >= 500:
            raise self._error(
                StatusClass.UPSTREAM_UNAVAILABLE,
                f"PathoEQA is unavailable (HTTP {status_code}).",
            )
        raise self._error(
            StatusClass.INVALID_INPUT, f"PathoEQA returned HTTP {status_code}."
        )

    def _read_capped(self, response, cap: int, what: str) -> bytes:
        buf = bytearray()
        for chunk in response.iter_content(chunk_size=16 * 1024):
            buf.extend(chunk)
            if len(buf) > cap:
                raise self._error(
                    StatusClass.MANIFEST_INVALID,
                    f"{what} payload exceeds the {cap} byte cap.",
                )
        return bytes(buf)
