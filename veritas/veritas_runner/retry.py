# veritas_runner/retry.py
#
# Atomic, request-level retry for CONTROL-PLANE calls only
# (manifest GET, callback POST). Same attempt_id, no workflow_dispatch.
#
# Data-plane downloads do NOT use this helper. Restarting a multi-GB stream
# from byte zero is the failure mode this module exists to avoid. Resume
# lives in artefacts.ArtefactClass.download.
#
# Disable: VERITAS_AUTO_RETRY=0 (or VERITAS_MAX_AUTO_RETRIES=0).

from __future__ import annotations

import logging
import os
import time
from typing import Callable, Optional, TypeVar

from veritas_runner.exceptions import VeritasRunnerError
from veritas_runner.status import StatusClass
from veritas_runner.datamodels import RetryProfile

logger = logging.getLogger(__name__)

T = TypeVar("T")

AUTO_RETRY_ENABLED = os.environ.get("VERITAS_AUTO_RETRY", "1").lower() not in (
    "0",
    "false",
    "no",
)
MAX_AUTO_RETRIES = int(os.environ.get("VERITAS_MAX_AUTO_RETRIES", 2))


class Profiles:
    """Named policies. Do not add a DATA_TRANSFER profile here.

    Heavy artefact IO retries inside ArtefactClass so a drop can resume
    with Range rather than repeating the whole GET.
    """

    FAST_CALLBACK = RetryProfile(initial_backoff_s=0.1, max_backoff_s=1.0)
    CONTROL_PLANE = RetryProfile(initial_backoff_s=0.5, max_backoff_s=5.0)


def retry_budget(max_retries: Optional[int] = None) -> int:
    if not AUTO_RETRY_ENABLED:
        return 0
    return MAX_AUTO_RETRIES if max_retries is None else max_retries




def with_auto_retry(
    operation: Callable[[], T],
    *,
    description: str,
    deadline: Optional[float] = None,
    max_retries: Optional[int] = None,
    profile: RetryProfile = Profiles.CONTROL_PLANE,
) -> T:
    """
    Re-issue an atomic callable after classified-transient failures.

    Never retried: 4xx, schema/checksum errors, scientific incompatibility,
    config errors. Those are deterministic.

    Deadline is checked before each try and before each sleep. Exhausting
    the budget raises DEADLINE_EXCEEDED rather than sleeping past it.
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
            delay = _sleep_delay(profile, attempt_no)
            if deadline is not None and time.monotonic() + delay > deadline:
                raise VeritasRunnerError(
                    failure_class=StatusClass.DEADLINE_EXCEEDED,
                    message=f"Operational deadline reached before retrying {description}.",
                )
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
