# veritas_runner/retry.py
"""
Retry management module for veritas_runner.

Provides atomic and resumable retry handlers configured via Pydantic policy profiles.
Supports exponential backoff with optional jitter, status classification, deadline
enforcement, and dynamic memory multiplier scaling for engine retries.

Notes
-----
How to disable retries:
- Feature flag: Set ``VERITAS_AUTO_RETRY=0`` or ``VERITAS_MAX_AUTO_RETRIES=0``.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import os
import random
import time
from typing import Any, Callable, Literal, Optional, TypeVar, Union

from pydantic import BaseModel, Field, PositiveFloat

from veritas_runner.exceptions import VeritasRunnerError
from veritas_runner.status import StatusClass

logger = logging.getLogger(__name__)

T = TypeVar("T")

AUTO_RETRY_ENABLED = os.environ.get("VERITAS_AUTO_RETRY", "1").lower() not in (
    "0",
    "false",
    "no",
)
_MAX_AUTO_RETRIES_ENV = os.environ.get("VERITAS_MAX_AUTO_RETRIES")
MAX_AUTO_RETRIES = int(_MAX_AUTO_RETRIES_ENV) if _MAX_AUTO_RETRIES_ENV is not None else None

PRESETS: dict[str, dict[str, Any]] = {
    "control_plane": {
        "initial_backoff_s": 0.5,
        "max_backoff_s": 5.0,
        "jitter": False,
    },
    "data_plane": {
        "initial_backoff_s": 2.0,
        "max_backoff_s": 30.0,
        "jitter": True,
    },
    "veritas_engine": {
        "initial_backoff_s": 1.0,
        "max_backoff_s": 5.0,
        "max_retries": 2,
        "jitter": False,
        "memory_multiplier": 2.0,
        "allowed_status_classes": (StatusClass.SYSTEM_RESOURCE_EXHAUSTED,),
    },
}


class RetryProfile(BaseModel):
    """
    A validated data model representing a retry policy configuration.

    Pure policy methods (e.g., `is_retryable`, `sleep_delay`) read from this instance's
    fields. Global execution overrides (such as feature flags and environment variables)
    are evaluated dynamically in `retry_budget`.

    Attributes
    ----------
    profile_type : {'control_plane', 'data_plane', 'veritas_engine'}, default='control_plane'
        Identifier for the policy variant.
    initial_backoff_s : PositiveFloat, default=0.5
        Base delay before the first retry attempt, in seconds.
    max_backoff_s : PositiveFloat, default=5.0
        Upper bound ceiling for exponential backoff delay, in seconds.
    backoff_factor : float, default=2.0
        Multiplier applied to the delay on each subsequent retry attempt. Must be >= 1.0.
    max_retries : int, default=2
        Default maximum retry budget for this profile. Must be >= 0.
    jitter : bool, default=False
        If True, applies random jitter in the range [0.5x, 1.5x) to backoff delays.
    allowed_status_classes : tuple of StatusClass, default=()
        Additional failure classifications permitted to retry under this profile,
        supplementing any classification with ``transient=True``.
    memory_multiplier : PositiveFloat, default=1.0
        Resource scaling multiplier applied to memory allocations on engine retries.
    """

    profile_type: Literal["control_plane", "data_plane", "veritas_engine"] = "control_plane"
    initial_backoff_s: PositiveFloat = 0.5
    max_backoff_s: PositiveFloat = 5.0
    backoff_factor: float = Field(default=2.0, ge=1.0)
    max_retries: int = Field(default=2, ge=0)
    jitter: bool = False
    allowed_status_classes: tuple[StatusClass, ...] = ()
    memory_multiplier: PositiveFloat = 1.0

    def is_retryable(self, failure_class: StatusClass) -> bool:
        """
        Determine whether a given failure classification qualifies for a retry attempt.

        Parameters
        ----------
        failure_class : StatusClass
            The error status classification associated with the caught exception.

        Returns
        -------
        bool
            True if the status is marked as transient or exists in `allowed_status_classes`.
        """
        return failure_class.transient or failure_class in self.allowed_status_classes

    def sleep_delay(self, retry_no: int = 0) -> float:
        """
        Calculate the sleep duration prior to a specific retry attempt.

        Parameters
        ----------
        retry_no : int, default=0
            Zero-indexed retry attempt number.

        Returns
        -------
        float
            Delay time in seconds, capped at `max_backoff_s` with optional jitter applied.
        """
        delay = min(
            self.initial_backoff_s * (self.backoff_factor ** retry_no),
            self.max_backoff_s,
        )
        if self.jitter:
            delay *= 0.5 + random.random()
        return delay

    def retry_budget(self, max_retries: Optional[int] = None) -> int:
        """
        Compute the effective number of additional attempts permitted.

        Applies configuration precedence in the following order:
        1. Feature Flag (`VERITAS_AUTO_RETRY=0` returns 0)
        2. Call-site override (`max_retries` argument)
        3. Environment variable override (`VERITAS_MAX_AUTO_RETRIES`)
        4. Profile default (`self.max_retries`)

        Parameters
        ----------
        max_retries : int or None, optional
            Explicit call-site override for the maximum retry count.

        Returns
        -------
        int
            The effective retry budget limit.
        """
        if not AUTO_RETRY_ENABLED:
            return 0
        if max_retries is not None:
            return max_retries
        if MAX_AUTO_RETRIES is not None:
            return MAX_AUTO_RETRIES
        return self.max_retries


def _invoke_operation(
    operation: Union[Callable[[], T], Callable[[int], T]], retry_no: int
) -> T:
    """
    Invoke an operation callable, supplying the retry index if accepted by signature.

    Parameters
    ----------
    operation : callable
        Callable object accepting either zero arguments or a single retry attempt integer.
    retry_no : int
        Zero-indexed current retry attempt number.

    Returns
    -------
    T
        The evaluation result returned by the callable.
    """
    try:
        sig = inspect.signature(operation)
        if len(sig.parameters) > 0:
            return operation(retry_no)  # type: ignore[call-arg]
    except (ValueError, TypeError):
        pass
    return operation()  # type: ignore[call-arg]


class AtomicRetrier:
    """
    Retry execution handler for stateless, idempotent operational calls.

    Designed for control-plane calls and local engine invocations where
    no partial progress state needs to be preserved between attempts.

    Parameters
    ----------
    profile : RetryProfile
        The retry profile configuration governing execution retries.
    """

    def __init__(self, profile: RetryProfile):
        self.profile = profile

    def run(
        self,
        operation: Union[Callable[[], T], Callable[[int], T]],
        *,
        description: str,
        deadline: Optional[float] = None,
        max_retries: Optional[int] = None,
    ) -> T:
        """
        Execute an operation with exponential backoff retries.

        Parameters
        ----------
        operation : callable
            The work unit to execute. Can be a 0-argument callable or a 1-argument
            callable accepting `retry_no` (int) for attempt-dependent scaling.
        description : str
            Human-readable operation description used for logging and error reporting.
        deadline : float or None, optional
            Monotonic timestamp threshold after which execution will be aborted.
        max_retries : int or None, optional
            Call-site override for maximum permitted retry attempts.

        Returns
        -------
        T
            The value returned by the successful execution of `operation`.

        Raises
        ------
        VeritasRunnerError
            If the operation fails with a non-retryable error, exhausts its retry budget,
            or breaches the specified deadline timestamp.
        """
        budget = self.profile.retry_budget(max_retries)
        last_error: Optional[VeritasRunnerError] = None

        for retry_no in range(budget + 1):
            if deadline is not None and time.monotonic() > deadline:
                raise VeritasRunnerError(
                    StatusClass.DEADLINE_EXCEEDED,
                    f"Operational deadline reached before {description}.",
                )
            try:
                return _invoke_operation(operation, retry_no)
            except VeritasRunnerError as e:
                last_error = e
                if not self.profile.is_retryable(e.failure_class) or retry_no == budget:
                    raise

                delay = self.profile.sleep_delay(retry_no)
                if deadline is not None and time.monotonic() + delay > deadline:
                    raise VeritasRunnerError(
                        StatusClass.DEADLINE_EXCEEDED,
                        f"Operational deadline reached before retrying {description}.",
                    )

                logger.warning(
                    "%s failed transiently (%s); retry %d/%d in %.1fs",
                    description,
                    e.failure_class.value,
                    retry_no + 1,
                    budget,
                    delay,
                )
                time.sleep(delay)

        assert last_error is not None
        raise last_error


class ResumableDownloadRetrier:
    """
    Retry execution handler for chunked, resumable byte downloads.

    Supports resuming stream transfers via HTTP Range requests using partial state
    tracking rather than restarting transfers from byte zero.

    Parameters
    ----------
    profile : RetryProfile
        The retry profile configuration governing execution retries.
    """

    def __init__(self, profile: RetryProfile):
        self.profile = profile

    def run(
        self,
        attempt: Callable[[int, Any], tuple[int, Any]],
        *,
        description: str,
        deadline: Optional[float] = None,
        max_retries: Optional[int] = None,
    ) -> tuple[int, Any]:
        """
        Execute a resumable download attempt loop across network failures.

        Parameters
        ----------
        attempt : callable
            Callable accepting `(resume_from_byte, digest)` and returning updated
            `(total_bytes_written, updated_digest)`.
        description : str
            Human-readable description of the file download for logging.
        deadline : float or None, optional
            Monotonic timestamp threshold after which transfer execution will be aborted.
        max_retries : int or None, optional
            Call-site override for maximum permitted retry attempts.

        Returns
        -------
        tuple of (int, Any)
            Tuple containing total bytes written and the final hash digest object.

        Raises
        ------
        VeritasRunnerError
            If the download fails non-transiently, exhausts the retry budget, or
            exceeds the operational deadline.
        """
        budget = self.profile.retry_budget(max_retries)
        last_error: Optional[VeritasRunnerError] = None
        written, digest = 0, hashlib.sha256()

        for retry_no in range(budget + 1):
            if deadline is not None and time.monotonic() > deadline:
                raise VeritasRunnerError(
                    StatusClass.DEADLINE_EXCEEDED,
                    f"Operational deadline reached before {description}.",
                )
            try:
                written, digest = attempt(written, digest)
                return written, digest
            except VeritasRunnerError as e:
                last_error = e
                if not self.profile.is_retryable(e.failure_class) or retry_no == budget:
                    raise

                delay = self.profile.sleep_delay(retry_no)
                if deadline is not None and time.monotonic() + delay > deadline:
                    raise VeritasRunnerError(
                        StatusClass.DEADLINE_EXCEEDED,
                        f"Operational deadline reached before retrying {description}.",
                    )

                logger.warning(
                    "%s failed transiently (%s); retry %d/%d in %.1fs, resuming from byte %d",
                    description,
                    e.failure_class.value,
                    retry_no + 1,
                    budget,
                    delay,
                    written,
                )
                time.sleep(delay)

        assert last_error is not None
        raise last_error


# Pre-instantiated profiles for export
CONTROL_PLANE = AtomicRetrier(
    RetryProfile(profile_type="control_plane", **PRESETS["control_plane"])
)
DATA_PLANE = ResumableDownloadRetrier(
    RetryProfile(profile_type="data_plane", **PRESETS["data_plane"])
)
VERITAS_ENGINE = AtomicRetrier(
    RetryProfile(profile_type="veritas_engine", **PRESETS["veritas_engine"])
)