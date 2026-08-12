# veritas_runner/exceptions.py
#
# Centralised error mapping for the package.
#
# Three things live here, and nothing else:
#   1. VeritasRunnerError - Exception type the runner raises on purpose.
#   2. http_failure_class() - HTTP status becomes a StatusClass.
#   3. ErrorFactory - context binding (attempt_id / sample_run_id / role) 

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar, Optional

from veritas_runner.status import StatusClass
from veritas_runner.datamodels import CallbackPayload

_RETRYABLE_CODES = frozenset[int]({408, 425, 429, 500, 502, 503, 504})


class VeritasRunnerError(Exception):
    """
    Encapsulates error context and maps exceptions to standardized
    failure categories, process exit codes, and callback payload content.

    Class Attributes
    ----------------
    max_detail_chars : int
        Maximum character length (2000) for the error detail string in callback 
        payloads.

    Attributes
    ----------
    failure_class : StatusClass
        Custom failure category driving process exit codes, terminal states,
        and callback failure types.
    message : str
        Human-readable summary description of the error.
    attempt_id : str | None
        Identifier for the associated ExecutionAttempt, if set.
    sample_run_id : str | None
        Identifier for the specific sample run when the failure is sample-scoped.
    """

    max_detail_chars: ClassVar[int] = 2000

    def __init__(
        self,
        failure_class: StatusClass,
        message: str,
        attempt_id: Optional[str] = None,
        sample_run_id: Optional[str] = None,
    ) -> None:
        self.failure_class = failure_class
        self.message = message
        self.attempt_id = attempt_id
        self.sample_run_id = sample_run_id

        ctx = [f"class={failure_class.value}"]
        if attempt_id:
            ctx.append(f"attempt_id={attempt_id}")
        if sample_run_id:
            ctx.append(f"sample_run_id={sample_run_id}")
        super().__init__(f"{message} [{', '.join(ctx)}]")

    @property
    def exit_code(self) -> int:
        return self.failure_class.exit_code

    @property
    def terminal_state(self) -> str:
        return self.failure_class.terminal_state

    @property
    def transient(self) -> bool:
        return self.failure_class.transient

    @classmethod
    def wrap(
        cls,
        exc: BaseException,
        *,
        failure_class: StatusClass = StatusClass.INTERNAL_ERROR,
        context: str = "",
        attempt_id: Optional[str] = None,
        sample_run_id: Optional[str] = None,
    ) -> VeritasRunnerError:
        """
        Wrap a raw `BaseException` into custom failure classes, keeping the original one-liner summary message of the exception.
        """
        where = f"{context}: " if context else ""
        return cls(
            failure_class=failure_class,
            message=f"{where}{type(exc).__name__}: {exc}",
            attempt_id=attempt_id,
            sample_run_id=sample_run_id,
        )

    def as_callback_payload(
        self,
        *,
        duration_s: Optional[float] = None,
    ) -> CallbackPayload:
        """Constructs a structured CallbackPayload model for failure callbacks.

        Truncates `self.message` to `max_detail_chars` and rounds the elapsed
        execution time to millisecond precision (3 decimal places).

        Parameters
        ----------
        duration_s : float | None, optional
            Elapsed execution time in seconds for the failed operation.

        Returns
        -------
        CallbackPayload
            Validated inner payload containing the string failure class, truncated
            detail, and optional duration in seconds.
        """
        return CallbackPayload(
            failure_class=self.failure_class.value,
            detail=self.message[:self.max_detail_chars],
            duration_seconds=round(duration_s, 3) if duration_s is not None else None,
        )


class HttpSurface(str, Enum):
    """
    Identifies the HTTP network surface where an operation or failure occurred:
    
    - CONTROL_PLANE: API endpoints for manifests, tokens, and attempt reads.
    - ARTEFACT: Signed object storage (S3/GCS) downloads for sample files.
    - CALLBACK: Status webhook posts back to PathoEQA.
    """

    CONTROL_PLANE = "control_plane"
    ARTEFACT = "artefact"
    CALLBACK = "callback"

class HttpFailureClassifier:
    """
    Classifier for mapping HTTP response statuses and surfaces to domain status classes.

    Retryability Logic
    ------------------
    A status code is considered retryable if it matches either condition:
    1. It is explicitly listed in `retryable_codes` (transient 4xx client errors).
    2. It is any server-side failure (`status_code >= 500`).

    Class Attributes
    ----------------
    retryable_codes : frozenset[int]
        Specific transient 4xx status codes (e.g., 408 Request Timeout, 425 Too Early,
        429 Too Many Requests) that are safe to retry.

    """

    retryable_codes: ClassVar[frozenset[int]] = frozenset[int]({408, 425, 429, 500, 502, 503, 504})

    @classmethod
    def classify(cls, status_code: int, surface: HttpSurface) -> Optional[StatusClass]:
        """
        Map an HTTP status code and network surface to a domain StatusClass.

        Parameters
        ----------
        status_code : int
            The HTTP response status code returned by the server.
        surface : HttpSurface
            The network surface category where the HTTP request was made
            (e.g., CONTROL_PLANE, ARTEFACT, or CALLBACK).

        Returns
        -------
        StatusClass | None
            The mapped failure taxonomy category for error status codes (>= 400),
            or None for non-error status codes (< 400).
        """
        if status_code < 400:
            return None

        retryable = status_code in cls.retryable_codes or status_code >= 500

        match surface:
            case HttpSurface.CALLBACK:
                if status_code in (404, 409, 422):
                    return StatusClass.CALLBACK_INVALID
                if status_code in (401, 403):
                    return StatusClass.AUTH_REJECTED
                return StatusClass.CALLBACK_FAILED if retryable else StatusClass.CALLBACK_INVALID

            case HttpSurface.ARTEFACT:
                if status_code in (401, 403, 404, 410):
                    return StatusClass.ARTEFACT_INVALID
                return StatusClass.DOWNLOAD_FAILED if retryable else StatusClass.ARTEFACT_INVALID

            case HttpSurface.CONTROL_PLANE:
                if status_code in (401, 403):
                    return StatusClass.AUTH_REJECTED
                if status_code == 404:
                    return StatusClass.ATTEMPT_NOT_FOUND
                if retryable:
                    return StatusClass.UPSTREAM_UNAVAILABLE
                return StatusClass.INVALID_INPUT

#--------------------------------------------- Binding

@dataclass(frozen=True)
class ErrorFactory:
    """
    Pre-bound error constructor. Hold one as an attribute
    (`self._error = ErrorFactory(attempt_id=...)`) or pass one into a free
    function.

        fail = ErrorFactory(attempt_id=attempt_id, prefix=f"role={role}")
        raise fail(StatusClass.CHECKSUM_MISMATCH, "sha256 does not match")
        fail.raise_for_http(response.status_code, HttpSurface.ARTEFACT)
    """

    attempt_id: Optional[str] = None
    sample_run_id: Optional[str] = None
    prefix: str = ""

    def __call__(self, failure_class: StatusClass, message: str) -> VeritasRunnerError:
        head = f"[{self.prefix}] " if self.prefix else ""
        return VeritasRunnerError(
            failure_class=failure_class,
            message=f"{head}{message}",
            attempt_id=self.attempt_id,
            sample_run_id=self.sample_run_id,
        )

    def bind(
        self,
        *,
        attempt_id: Optional[str] = None,
        sample_run_id: Optional[str] = None,
        prefix: Optional[str] = None,
    ) -> "ErrorFactory":
        """Narrow the context, e.g. `client._error.bind(sample_run_id=sr.id)`."""
        return ErrorFactory(
            attempt_id=attempt_id if attempt_id is not None else self.attempt_id,
            sample_run_id=sample_run_id if sample_run_id is not None else self.sample_run_id,
            prefix=prefix if prefix is not None else self.prefix,
        )

    def raise_for_http(
        self,
        status_code: int,
        surface: HttpSurface,
        *,
        context: str = "",
    ) -> None:
        """Raise the mapped error for a >= 400 response; return silently otherwise."""
        failure_class = http_failure_class(status_code, surface)
        if failure_class is None:
            return
        where = f" during {context}" if context else ""
        raise self(failure_class, f"HTTP {status_code}{where} ({surface.value}).")

    def wrap(
        self,
        exc: BaseException,
        *,
        failure_class: StatusClass = StatusClass.INTERNAL_ERROR,
        context: str = "",
    ) -> VeritasRunnerError:
        return VeritasRunnerError.wrap(
            exc,
            failure_class=failure_class,
            context=f"{self.prefix} {context}".strip(),
            attempt_id=self.attempt_id,
            sample_run_id=self.sample_run_id,
        )