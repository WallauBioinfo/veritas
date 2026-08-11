from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from veritas_runner.status import StatusClass

# Long subprocess stderr / validation dumps must not blow up a callback body.
MAX_DETAIL_CHARS = 2000


class VeritasRunnerError(Exception):
    """
    A classified runner failure.

    Attributes
    ----------
    failure_class : StatusClass
        Drives the callback payload, the ExecutionAttempt terminal state and
        the process exit code. Never invent a string outside the enum.
    attempt_id : str | None
        The ExecutionAttempt this failure belongs to. Set for control-plane and
        artefact errors; often unset for pure-local errors, which runner.py
        backfills via `.scoped()` before reporting.
    sample_run_id : str | None
        Set only when the failure is scoped to one sample, so a per-sample
        failure is never reported as an attempt-wide one.
    """

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

    # ------------------------------------------------------------- properties
    # All three delegate to StatusClass. Duplicating any of this here is how a
    # second, contradictory authority on retryability gets born.

    @property
    def exit_code(self) -> int:
        """Passthrough so the CLI never reaches into the enum itself."""
        return self.failure_class.exit_code

    @property
    def terminal_state(self) -> str:
        """PathoEQA-facing ExecutionAttempt state implied by this failure."""
        return self.failure_class.terminal_state

    @property
    def transient(self) -> bool:
        """Whether retry.py may re-issue the same request. Enum decides, not us."""
        return self.failure_class.transient

    # ---------------------------------------------------------- constructors

    @classmethod
    def wrap(
        cls,
        exc: BaseException,
        *,
        failure_class: StatusClass = StatusClass.INTERNAL_ERROR,
        context: str = "",
        attempt_id: Optional[str] = None,
        sample_run_id: Optional[str] = None,
    ) -> "VeritasRunnerError":
        """
        Turn an unclassified exception into a classified one without losing the
        cause. Use at catch-all boundaries:

            except VeritasRunnerError:
                raise
            except Exception as e:
                raise VeritasRunnerError.wrap(e, context="unpacking SDF") from e
        """
        where = f"{context}: " if context else ""
        return cls(
            failure_class=failure_class,
            message=f"{where}{type(exc).__name__}: {exc}",
            attempt_id=attempt_id,
            sample_run_id=sample_run_id,
        )

    def scoped(
        self,
        *,
        attempt_id: Optional[str] = None,
        sample_run_id: Optional[str] = None,
    ) -> "VeritasRunnerError":
        """
        Return the same failure with identifiers filled in. Lets a low-level
        helper raise without knowing the attempt, and the orchestrator attach
        the ids on the way out instead of rewriting the message.
        """
        return VeritasRunnerError(
            failure_class=self.failure_class,
            message=self.message,
            attempt_id=attempt_id or self.attempt_id,
            sample_run_id=sample_run_id or self.sample_run_id,
        )

    # ------------------------------------------------------------- reporting

    def as_callback_detail(self) -> dict[str, Any]:
        """Failure fields merged into the callback envelope posted to PathoEQA."""
        payload: dict[str, Any] = {
            "failure_class": self.failure_class.value,
            "detail": self.message[:MAX_DETAIL_CHARS],
        }
        if self.sample_run_id:
            payload["sample_run_id"] = self.sample_run_id
        return payload


# ---------------------------------------------------------------------------
# HTTP status -> StatusClass
# ---------------------------------------------------------------------------
#
# The same status code means different things depending on which call failed,
# so the mapping is parameterised by surface instead of being copy-pasted per
# caller. Example: a 404 fetching the manifest is ATTEMPT_NOT_FOUND (PathoEQA
# does not know this attempt); a 404 on a signed artefact URL is a dead/expired
# link, i.e. DOWNLOAD_FAILED; a 404 on a callback is a broken callback_url,
# which is CALLBACK_INVALID and must never be retried into the void.


class HttpSurface(str, Enum):
    CONTROL_PLANE = "control_plane"  # PathoEQA manifest / attempt reads
    ARTEFACT = "artefact"  # signed object-storage downloads
    CALLBACK = "callback"  # status posts back to PathoEQA


_RETRYABLE_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


def http_failure_class(status_code: int, surface: HttpSurface) -> Optional[StatusClass]:
    """
    Translate an HTTP status into the taxonomy. Returns None for < 400 so the
    caller can use it as a guard. Pure function: no logging, no raising, no
    self - which is what makes it testable as a table.
    """
    if status_code < 400:
        return None

    retryable = status_code in _RETRYABLE_CODES or status_code >= 500

    if surface is HttpSurface.CALLBACK:
        # 409 = attempt already terminal, 422 = payload rejected. Both are
        # final answers: re-posting the same body cannot change them.
        if status_code in (409, 422):
            return StatusClass.CALLBACK_INVALID
        if status_code in (401, 403):
            return StatusClass.AUTH_REJECTED
        if status_code == 404:
            return StatusClass.CALLBACK_INVALID
        return StatusClass.CALLBACK_FAILED if retryable else StatusClass.CALLBACK_INVALID

    if surface is HttpSurface.ARTEFACT:
        # Signed URLs: 401/403 usually means the signature expired, not that our
        # OIDC token is bad - still not retryable, the manifest must be reissued.
        if status_code in (401, 403, 404, 410):
            return StatusClass.ARTEFACT_INVALID
        return StatusClass.DOWNLOAD_FAILED if retryable else StatusClass.ARTEFACT_INVALID

    # CONTROL_PLANE
    if status_code in (401, 403):
        return StatusClass.AUTH_REJECTED
    if status_code == 404:
        return StatusClass.ATTEMPT_NOT_FOUND
    if retryable:
        return StatusClass.UPSTREAM_UNAVAILABLE
    return StatusClass.INVALID_INPUT


# ---------------------------------------------------------------------------
# Context binding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ErrorFactory:
    """
    Pre-bound error constructor. Replaces the mixin: hold one as an attribute
    (`self.fail = ErrorFactory(attempt_id=...)`) or pass one into a free
    function - both work, whereas a mixin only works via inheritance.

        fail = ErrorFactory(attempt_id=attempt_id, prefix=f"role={role}")
        raise fail(StatusClass.CHECKSUM_MISMATCH, "sha256 does not match")
        fail.for_http(response.status_code, HttpSurface.ARTEFACT)  # raises or returns
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

    def bind(self, **overrides: Any) -> "ErrorFactory":
        """Narrow the context, e.g. `client.fail.bind(sample_run_id=sr.id)`."""
        return ErrorFactory(
            attempt_id=overrides.get("attempt_id", self.attempt_id),
            sample_run_id=overrides.get("sample_run_id", self.sample_run_id),
            prefix=overrides.get("prefix", self.prefix),
        )

    def for_http(
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