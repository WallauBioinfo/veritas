"""
Single source of truth for the runner's failure taxonomy.
"""

from enum import Enum


class StatusClass(str, Enum):
    """
    Failure taxonomy for one ExecutionAttempt.
    """

    SUCCESS = "success"

    ## TODO: Fix all this. Class values do not align with SPEC.

    # Phase 1 - preflight: resolving attempt_id, auth, manifest, downloading inputs.
    # Caller/attempt-data problem - PathoEQA must fix the request; no retry helps.
    INVALID_INPUT = "invalid_input"
    ATTEMPT_NOT_FOUND = "attempt_not_found"
    AUTH_REJECTED = "auth_rejected"
    MANIFEST_INVALID = "manifest_invalid"
    SCIENTIFIC_INCOMPATIBILITY = "scientific_incompatibility"

    # Phase 1 - transient infrastructure. Eligible for in-process retry.
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    DOWNLOAD_FAILED = "download_failed"

    # Integrity - the bytes arrived but are wrong or unusable. Never retried:
    # the frozen input or the recorded hash is wrong, and both need a human.
    CHECKSUM_MISMATCH = "checksum_mismatch"
    ARTEFACT_INVALID = "artefact_invalid"  # correct bytes, unusable shape (bad tar, empty SDF)

    # Our own environment is broken - needs a maintainer, not a re-dispatch.
    CONFIG_ERROR = "config_error"

    # Phase 2 - execution supervision (wrapping the veritas subprocess).
    TIMEOUT = "processing_timeout"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    VERITAS_CRASHED = "veritas_crashed"

    # Phase 3 - delivery. Format only, never a judgment on the analysis itself.
    OUTPUT_INVALID = "output_invalid"  # veritas exited 0 but wrote unreadable/absent results
    METRICS_MISSING = "metrics_missing"
    CALLBACK_INVALID = "callback_invalid"
    CALLBACK_FAILED = "callback_failed"

    INTERNAL_ERROR = "internal_error"

    @property
    def transient(self) -> bool:
        """
        True when re-issuing the *same request* may succeed without any state
        change upstream. Drives the in-process retry helper (max 2 extra tries).
        Says nothing about whether the ExecutionAttempt should be re-dispatched.
        """
        return self in {
            StatusClass.UPSTREAM_UNAVAILABLE,
            StatusClass.DOWNLOAD_FAILED,
            StatusClass.CALLBACK_FAILED,
        }

    @property
    def terminal_state(self) -> str:
        """PathoEQA-facing ExecutionAttempt state for this class."""
        return "Completed" if self is StatusClass.SUCCESS else "Failed"

    @property
    def exit_code(self) -> int:
        if self is StatusClass.SUCCESS:
            return 0
        if self in {
            StatusClass.INVALID_INPUT,
            StatusClass.ATTEMPT_NOT_FOUND,
            StatusClass.AUTH_REJECTED,
            StatusClass.MANIFEST_INVALID,
            StatusClass.SCIENTIFIC_INCOMPATIBILITY,
        }:
            return 10  # caller/attempt-data problem
        if self is StatusClass.CONFIG_ERROR:
            return 20  # our environment - alert, do not re-dispatch
        if self in {
            StatusClass.UPSTREAM_UNAVAILABLE,
            StatusClass.DOWNLOAD_FAILED,
        }:
            return 30  # transient, already retried in-process and still failing
        if self in {StatusClass.CHECKSUM_MISMATCH, StatusClass.ARTEFACT_INVALID}:
            return 35  # integrity - frozen input or recorded hash is wrong
        if self in {StatusClass.TIMEOUT, StatusClass.DEADLINE_EXCEEDED}:
            return 38  # ran out of budget - continuation, not retry
        if self is StatusClass.VERITAS_CRASHED:
            return 40  # died mid-run, cause unknown - investigate
        if self in {
            StatusClass.OUTPUT_INVALID,
            StatusClass.METRICS_MISSING,
            StatusClass.CALLBACK_INVALID,
            StatusClass.CALLBACK_FAILED,
        }:
            return 50  # completed, delivery problem - results may exist on disk
        return 60  # INTERNAL_ERROR and anything unclassified