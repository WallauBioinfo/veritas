"""
Single source of truth for the runner's failure taxonomy.
"""

from enum import Enum


class StatusClass(str, Enum):
    """
    Failure taxonomy for one ExecutionAttempt.
    """

    SUCCESS = "success"

    INVALID_INPUT = "invalid_input"
    ATTEMPT_NOT_FOUND = "attempt_not_found" # NEW
    AUTH_REJECTED = "auth_rejected" # NEW
    MANIFEST_INVALID = "manifest_invalid" # NEW
    SCIENTIFIC_INCOMPATIBILITY = "scientific_incompatibility" # CURRENTLY POORLY MAPPED

    # Transient infrastructure. Eligible for in-process retry.
    UPSTREAM_UNAVAILABLE = "upstream_unavailable" # NEW
    DOWNLOAD_FAILED = "download_failed"
    CALLBACK_FAILED = "callback_failed" # NEW


    CHECKSUM_MISMATCH = "checksum_mismatch"
    ARTEFACT_INVALID = "artefact_invalid" # NEW

    CONFIG_ERROR = "config_error" # NEW
    EXECUTOR_UNAVAILABLE = "executor_unavailable"

    TIMEOUT = "processing_timeout"
    DEADLINE_EXCEEDED = "deadline_exceeded" # NEW
    VERITAS_CRASHED = "veritas_crashed" # NEW
    SYSTEM_RESOURCE_EXHAUSTED = "system_resource_exhausted"  # NEW: OOM-killed subprocess; see retry.py VERITAS_ENGINE

    METRICS_MISSING = "metrics_missing"
    CALLBACK_INVALID = "callback_invalid"

    # Other
    INTERNAL_ERROR = "internal_error"

    @property
    def transient(self) -> bool:
        """
        True when re-issuing the *same request* may succeed without any state
        change upstream. Drives the in-process retry helper (max 2 extra tries).
        Says nothing about whether the ExecutionAttempt should be re-dispatched.

        SYSTEM_RESOURCE_EXHAUSTED is deliberately excluded: blindly re-issuing
        the same call won't fix an OOM. It's retried only under a profile that
        explicitly opts it in (RetryProfile.allowed_status_classes), since a
        useful retry there means changing something (e.g. memory allocation),
        not repeating the identical operation.
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
            return 20 
        if self in {
            StatusClass.UPSTREAM_UNAVAILABLE,
            StatusClass.DOWNLOAD_FAILED
        }:
            return 30 
        if self in {StatusClass.CHECKSUM_MISMATCH, StatusClass.ARTEFACT_INVALID}:
            return 35
        if self in {StatusClass.TIMEOUT, StatusClass.DEADLINE_EXCEEDED}:
            return 38
        if self is StatusClass.SYSTEM_RESOURCE_EXHAUSTED:
            return 39
        if self in {StatusClass.VERITAS_CRASHED, StatusClass.EXECUTOR_UNAVAILABLE}:
            return 40
        if self in {
            StatusClass.OUTPUT_INVALID,
            StatusClass.METRICS_MISSING,
            StatusClass.CALLBACK_INVALID,
            StatusClass.CALLBACK_FAILED,
        }:
            return 50  # Completed, but delivery problem: esults may exist on disk
        return 60  # INTERNAL_ERROR and anything unmapped

    @property
    def spec_failure_class(self) -> str:
        """
        This class's failure_class as sent to PathoEQA, restricted to the
        nine categories SPEC-05/GUIA-14 document. Several internal classes
        map to a shared spec category by judgment call, noted inline; the
        detail these classes distinguish (e.g. ATTEMPT_NOT_FOUND vs
        AUTH_REJECTED) still lives in the callback's sanitized `detail` text,
        it's just not encoded in `failure_class` itself.
        """
        # TODO: check mapping if reasonable

        if self in {
            StatusClass.SUCCESS,
            StatusClass.INVALID_INPUT,
            StatusClass.SCIENTIFIC_INCOMPATIBILITY,
            StatusClass.UPSTREAM_UNAVAILABLE,  # -> mapped below to executor_unavailable
            StatusClass.DOWNLOAD_FAILED,
            StatusClass.CHECKSUM_MISMATCH,
            StatusClass.TIMEOUT,
            StatusClass.METRICS_MISSING,
            StatusClass.CALLBACK_INVALID,
            StatusClass.INTERNAL_ERROR,
            StatusClass.EXECUTOR_UNAVAILABLE,
        }:
            if self is StatusClass.UPSTREAM_UNAVAILABLE:
                return "executor_unavailable"
            return self.value

        if self in {
            StatusClass.ATTEMPT_NOT_FOUND,
            StatusClass.AUTH_REJECTED,
            StatusClass.MANIFEST_INVALID,
            StatusClass.ARTEFACT_INVALID,
        }:
            return "invalid_input"

        if self is StatusClass.DEADLINE_EXCEEDED:
            return "processing_timeout"

        if self is StatusClass.CALLBACK_FAILED:
            return "callback_invalid"
        return "internal_error"