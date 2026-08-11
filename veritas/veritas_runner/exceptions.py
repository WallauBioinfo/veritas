# veritas_runner/exceptions.py

from __future__ import annotations

from typing import Optional
from veritas_runner.status import StatusClass


class VeritasRunnerError(Exception):
    """Base domain exception for all expected Veritas runner failures."""

    def __init__(
        self,
        status_class: StatusClass,
        message: str,
        attempt_id: Optional[str] = None,
        attempt_number: Optional[int] = None,
    ):
        self.status_class = status_class
        self.message = message
        self.attempt_id = attempt_id
        self.attempt_number = attempt_number

        ctx = []
        if attempt_id:
            ctx.append(f"attempt_id={attempt_id}")
        if attempt_number:
            ctx.append(f"try={attempt_number}/3")

        prefix = f" [{', '.join(ctx)}]" if ctx else ""
        super().__init__(f"{message}{prefix}")

    @property
    def is_retryable(self) -> bool:
        """Determines if the error warrants an in-runner or attempt retry."""
        return self.status_class in {
            StatusClass.UPSTREAM_UNAVAILABLE,
            StatusClass.UNKNOWN_HTTP_ERROR,
            StatusClass.DOWNLOAD_FAILED,
        }


def raise_for_http_status(
    status_code: int,
    context_id: str,
    attempt_id: Optional[str] = None,
) -> None:
    """
    Unified HTTP status code translator for PathoEQA endpoints and Asset Storage.
    """
    if status_code < 400:
        return

    if status_code in (401, 403):
        raise VeritasRunnerError(
            status_class=StatusClass.AUTH_REJECTED,
            message=f"Authentication/permission rejected for '{context_id}' (HTTP {status_code}).",
            attempt_id=attempt_id,
        )
    if status_code == 404:
        raise VeritasRunnerError(
            status_class=StatusClass.ATTEMPT_NOT_FOUND,
            message=f"Resource or attempt not found for '{context_id}' (HTTP 404).",
            attempt_id=attempt_id,
        )
    if status_code == 409:
        raise VeritasRunnerError(
            status_class=StatusClass.INVALID_INPUT,
            message=f"Conflict state for '{context_id}' (HTTP 409).",
            attempt_id=attempt_id,
        )
    if status_code == 429 or status_code >= 500:
        raise VeritasRunnerError(
            status_class=StatusClass.UPSTREAM_UNAVAILABLE,
            message=f"Upstream service unavailable or rate limited for '{context_id}' (HTTP {status_code}).",
            attempt_id=attempt_id,
        )
    raise VeritasRunnerError(
        status_class=StatusClass.UNKNOWN_HTTP_ERROR,
        message=f"Unexpected HTTP status {status_code} for '{context_id}'.",
        attempt_id=attempt_id,
    )