from enum import Enum


class StatusClass(str, Enum):
    SUCCESS = "success"

    # user / input errors — not retryable
    INVALID_INPUT = "invalid_input"
    ATTEMPT_ID_NOT_FOUND = "attempt_id_not_found"
    CONFIG_ERROR = "config_error"

    # analysis outcomes — not retryable
    VERITAS_ANALYSIS_FAILED = "veritas_analysis_failed"
    VERITAS_REPORT_INVALID = "veritas_report_invalid"
    TRUTH_INCOMPATIBLE = "truth_incompatible"

    # transient / retryable
    TIMEOUT = "timeout"
    TRANSIENT_IO = "transient_io"

    # infra / unexpected — investigate
    VERITAS_CRASHED = "veritas_crashed" #????
    INTERNAL_ERROR = "internal_error"

    @property
    def retryable(self) -> bool:
        return self in {StatusClass.TIMEOUT, StatusClass.TRANSIENT_IO}

    @property
    def exit_code(self) -> int:
        if self == StatusClass.SUCCESS:
            return 0
        if self in {StatusClass.INVALID_INPUT, StatusClass.ATTEMPT_ID_NOT_FOUND, StatusClass.CONFIG_ERROR}:
            return 11 # Config / Input Error
        if self in {StatusClass.VERITAS_ANALYSIS_FAILED, StatusClass.VERITAS_REPORT_INVALID, StatusClass.TRUTH_INCOMPATIBLE}:
            return 20 # Analysis Failure
        if self in {StatusClass.TIMEOUT, StatusClass.TRANSIENT_IO}:
            return 30 # Timeout
        return 10 # Fallback for crashed / internal / transient error #????