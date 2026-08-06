# veritas/exceptions.py

from veritas.status import StatusClass

class VeritasRunnerError(Exception):
    """Base exception for all expected Veritas runner failures."""
    def __init__(self, status_class: StatusClass, message: str):
        self.status_class = status_class
        self.message = message
        super().__init__(f"[{status_class.value}] {message}")


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