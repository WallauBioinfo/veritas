import uuid

import os
import shutil
import uuid


def _validate_prerequisites(
    attempt_id: str,
    output_dir: str,
    api_url: str,
    oidc_token: str,
) -> None:
    """
    Validates the runner's local environment and required inputs before any
    network call is made. Nothing in this function performs I/O against
    PathoEQA — it only checks local system state and the values about to
    be used to construct that call.
    """
    if not attempt_id or not attempt_id.strip():
        raise VeritasRunnerError(StatusClass.MISSING_ATTEMPT_ID, "attempt_id is missing or empty.")

    try:
        uuid.UUID(attempt_id)
    except ValueError:
        raise VeritasRunnerError(
            StatusClass.MISSING_ATTEMPT_ID,
            f"attempt_id is not a valid UUID: {attempt_id!r}"
        )

    if not api_url or not api_url.strip():
        raise VeritasRunnerError(StatusClass.CONFIG_ERROR, "PATHOEQA_API_URL is not set in the runner environment.")

    if not oidc_token or not oidc_token.strip():
        raise VeritasRunnerError(StatusClass.CONFIG_ERROR, "GITHUB_OIDC_TOKEN is not set; OIDC exchange must run first.")

    if shutil.which("veritas") is None:
        raise VeritasRunnerError(StatusClass.CONFIG_ERROR, "'veritas' executable not found on PATH.")

    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as e:
        raise VeritasRunnerError(StatusClass.CONFIG_ERROR, f"Could not create output directory '{output_dir}': {e}")

    if not os.access(output_dir, os.W_OK):
        raise VeritasRunnerError(StatusClass.CONFIG_ERROR, f"Output directory '{output_dir}' is not writable.")