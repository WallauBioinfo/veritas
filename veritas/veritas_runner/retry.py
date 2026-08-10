# veritas_runner/retry.py
#
# Request-level automatic retry, isolated in one file so it can be switched off
# or deleted without touching orchestration logic.
#
# SCOPE (the specs are ambiguous, so this module states the contract):
#   * IN SCOPE  - re-issuing ONE HTTP request inside the CURRENT ExecutionAttempt
#                 after a classified-transient infrastructure failure
#                 (connection reset, read timeout, 502/503/504, 429).
#                 Same attempt_id, no new DB row, no workflow_dispatch.
#   * OUT OF SCOPE - attempt-level retry and continuation. A new "tentativa"
#                 means a new attempt_id and a new workflow_dispatch, decided by
#                 PathoEQA after this runner reports a terminal state. The
#                 runner never simulates, mocks or triggers that.
#
# HOW TO REMOVE IT
#   Option A (runtime, no code change): set VERITAS_AUTO_RETRY=0 (or
#     VERITAS_MAX_AUTO_RETRIES=0). with_auto_retry degrades to a plain call:
#     the operation runs once and any failure propagates unchanged.
#   Option B (permanent): delete this file and replace the import in runner.py
#     with `def with_auto_retry(op, **_): return op()`. No other call site or
#     signature changes.

from __future__ import annotations

import logging
import os
import time
from typing import Callable, Optional, TypeVar

from veritas_runner.status import StatusClass
from veritas_runner.exceptions import VeritasRunnerError

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Feature flag. Off -> every operation runs exactly once.
AUTO_RETRY_ENABLED = os.environ.get("VERITAS_AUTO_RETRY", "1").lower() not in (
    "0",
    "false",
    "no",
)
MAX_AUTO_RETRIES = int(os.environ.get("VERITAS_MAX_AUTO_RETRIES", 2))
RETRY_BASE_DELAY_S = float(os.environ.get("VERITAS_RETRY_BASE_DELAY", 2))


def retry_budget(max_retries: Optional[int] = None) -> int:
    """Effective number of EXTRA attempts, after applying the feature flag."""
    if not AUTO_RETRY_ENABLED:
        return 0
    return MAX_AUTO_RETRIES if max_retries is None else max_retries


def with_auto_retry(
    operation: Callable[[], T],
    *,
    description: str,
    deadline: Optional[float] = None,
    max_retries: Optional[int] = None,
) -> T:
    """
    Run `operation`, retrying only classified-transient failures, at most
    `retry_budget()` extra times (spec default: 2), with exponential backoff.

    Never retried: 4xx, schema violations, checksum mismatches, scientific
    incompatibility, config errors. Those are deterministic - a second identical
    request returns the same answer and only burns the 75-minute budget.

    The deadline is honoured before each try and before each sleep, so retrying
    can never push the attempt past the operational deadline; it raises
    DEADLINE_EXCEEDED instead, which the caller turns into `Partial`.
    """
    budget = retry_budget(max_retries)
    last_error: Optional[VeritasRunnerError] = None

    for attempt_no in range(budget + 1):
        if deadline is not None and time.monotonic() > deadline:
            raise VeritasRunnerError(
                failure_class=StatusClass.DEADLINE_EXCEEDED,
                message=f"Operational deadline reached before {description}.",
            )
        try:
            return operation()
        except VeritasRunnerError as e:
            last_error = e
            if not e.failure_class.transient or attempt_no == budget:
                raise
            delay = RETRY_BASE_DELAY_S * (2**attempt_no)
            if deadline is not None and time.monotonic() + delay > deadline:
                raise
            logger.warning(
                "%s failed transiently (%s); retry %d/%d in %.1fs",
                description,
                e.failure_class.value,
                attempt_no + 1,
                budget,
                delay,
            )
            time.sleep(delay)

    assert last_error is not None
    raise last_error
