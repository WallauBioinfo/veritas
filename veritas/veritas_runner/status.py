from enum import Enum


class StatusClass(str, Enum):
    SUCCESS = "success"

    # Phase 1 — not retryable, caller/PathoEQA needs to fix the request.
    INVALID_INPUT = "invalid_input"
    ATTEMPT_NOT_FOUND = "attempt_not_found"
    AUTH_REJECTED = "auth_rejected"
    MANIFEST_INVALID = "manifest_invalid"

    # Phase 1 — transient, safe to re-dispatch the whole attempt.
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    FETCH_FAILED = "fetch_failed"

    # Our own environment is broken — not retryable, needs a maintainer, not a re-dispatch.
    CONFIG_ERROR = "config_error"

    # Phase 2 — execution supervision (wrapping the veritas subprocess).
    TIMEOUT = "timeout"
    VERITAS_CRASHED = "veritas_crashed"

    # Phase 3 — callback: veritas ran to completion, this is about delivering results.
    # Format only — never a judgment on the quality of the analysis itself.
    OUTPUT_INVALID = "output_invalid"
    CALLBACK_FAILED = "callback_failed"

    # Genuinely unanticipated.
    INTERNAL_ERROR = "internal_error"

    @property
    def retryable(self) -> bool:
        """Safe to re-dispatch the entire attempt from scratch."""
        return self in {
            StatusClass.UPSTREAM_UNAVAILABLE,
            StatusClass.FETCH_FAILED,
            StatusClass.TIMEOUT,
        }

    @property
    def exit_code(self) -> int:
        if self is StatusClass.SUCCESS:
            return 0
        if self in {
            StatusClass.INVALID_INPUT,
            StatusClass.ATTEMPT_NOT_FOUND,
            StatusClass.AUTH_REJECTED,
            StatusClass.MANIFEST_INVALID,
        }:
            return 10  # caller/attempt-data problem
        if self is StatusClass.CONFIG_ERROR:
            return 20  # our environment — alert, don't retry
        if self.retryable:
            return 30  # transient — safe to re-dispatch
        if self is StatusClass.VERITAS_CRASHED:
            return 40  # died mid-run, cause unknown — investigate
        if self in {StatusClass.OUTPUT_INVALID, StatusClass.CALLBACK_FAILED}:
            return 50  # ran to completion, delivery problem — results may exist on disk
        return 60  # StatusClass.INTERNAL_ERROR and anything unclassified