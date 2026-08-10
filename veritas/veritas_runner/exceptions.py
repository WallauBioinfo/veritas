# veritas/exceptions.py

from veritas_runner.status import StatusClass

class VeritasRunnerError(Exception):
    """Base exception for all expected Veritas runner failures."""

    def __init__(
        self,
        status_class: StatusClass,
        message: str,
        attempt_id: Optional[str] = None,
        attempt_number: Optional[int] = None,  # 1, 2, or 3
    ):
        self.status_class = status_class
        self.message = message
        self.attempt_id = attempt_id
        self.attempt_number = attempt_number

        # Format clean log representation
        ctx = []
        if attempt_id:
            ctx.append(f"attempt_id={attempt_id}")
        if attempt_number:
            ctx.append(f"try={attempt_number}/3")
            
        prefix = f" [{', '.join(ctx)}]" if ctx else ""
        super().__init__(f"{message}{prefix}")

    @property
    def is_retryable(self) -> bool:
        """Determines if the error warrants consuming another retry attempt."""
        return self.status_class in {
            StatusClass.SERVER_UNAVAILABLE,
            StatusClass.UNKNOWN_HTTP_ERROR,
        }


def raise_for_http_status(status_code: int, context_id: str) -> None:
    """
    Translates HTTP error status codes into spec-compliant VeritasRunnerErrors.
    """
    if status_code < 400:
        return

    if status_code in (404, 409):
        raise VeritasRunnerError(
            StatusClass.INVALID_INPUT,
            f"PathoEQA rejected request for '{context_id}' (HTTP {status_code})."
        )
    if status_code in (401, 403):
        raise VeritasRunnerError(
            StatusClass.AUTHENTICATION_ERROR,
            f"OIDC authentication rejected by PathoEQA (HTTP {status_code})."
        )
    if status_code == 429 or status_code >= 500:
        raise VeritasRunnerError(
            StatusClass.SERVER_UNAVAILABLE,
            f"PathoEQA unavailable or rate limited (HTTP {status_code})."
        )
    raise VeritasRunnerError(
        StatusClass.UNKNOWN_HTTP_ERROR,
        f"Unexpected HTTP status {status_code} for '{context_id}'."
    )