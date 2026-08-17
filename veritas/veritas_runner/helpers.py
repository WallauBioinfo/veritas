# veritas_runner/helpers.py
#
# Local preflight checks run before any PathoEQA network call (SPEC phase 1a).

from __future__ import annotations

import os
import shutil
import uuid
from urllib.parse import urlparse

from veritas_runner.exceptions import VeritasRunnerError
from veritas_runner.status import StatusClass


def _validate_prerequisites(
    attempt_id: str,
    workdir: str,
    api_url: str,
    oidc_token: str,
) -> None:
    """
    Validate local environment and caller inputs before any network I/O.

    Covers everything knowable from the invocation context alone: attempt_id
    shape, PathoEQA endpoint configuration, OIDC credential presence, workspace
    writability, and the external tools veritas validate requires on PATH.
    """
    if not attempt_id or not attempt_id.strip():
        raise VeritasRunnerError(
            StatusClass.INVALID_INPUT,
            "attempt_id is missing or empty.",
            attempt_id=attempt_id or None,
        )

    try:
        uuid.UUID(attempt_id)
    except ValueError:
        raise VeritasRunnerError(
            StatusClass.INVALID_INPUT,
            f"attempt_id is not a valid UUID: {attempt_id!r}",
            attempt_id=attempt_id,
        )

    if not api_url or not api_url.strip():
        raise VeritasRunnerError(
            StatusClass.CONFIG_ERROR,
            "PATHOEQA_API_URL is not set in the runner environment.",
            attempt_id=attempt_id,
        )

    parsed = urlparse(api_url.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        raise VeritasRunnerError(
            StatusClass.CONFIG_ERROR,
            f"PATHOEQA_API_URL must be an HTTPS URL, got: {api_url!r}",
            attempt_id=attempt_id,
        )

    if not oidc_token or not oidc_token.strip():
        raise VeritasRunnerError(
            StatusClass.CONFIG_ERROR,
            "GITHUB_OIDC_TOKEN is not set; OIDC exchange must run first.",
            attempt_id=attempt_id,
        )

    for tool in ("veritas", "rtg", "bcftools"):
        if shutil.which(tool) is None:
            raise VeritasRunnerError(
                StatusClass.CONFIG_ERROR,
                f"Required tool '{tool}' not found on PATH.",
                attempt_id=attempt_id,
            )

    if not workdir or not workdir.strip():
        raise VeritasRunnerError(
            StatusClass.CONFIG_ERROR,
            "workdir is missing or empty.",
            attempt_id=attempt_id,
        )

    try:
        os.makedirs(workdir, exist_ok=True)
    except OSError as e:
        raise VeritasRunnerError(
            StatusClass.CONFIG_ERROR,
            f"Could not create workspace '{workdir}': {e}",
            attempt_id=attempt_id,
        ) from e

    if not os.access(workdir, os.W_OK):
        raise VeritasRunnerError(
            StatusClass.CONFIG_ERROR,
            f"Workspace '{workdir}' is not writable.",
            attempt_id=attempt_id,
        )

def _validate_workdir(
    attempt_id: str,
    workdir: str | Path,
    api_url: str,
    oidc_token: str,
) -> Path:
    """Validate environment prerequisites and return a canonical workdir Path."""
    if not attempt_id:
        raise VeritasRunnerError(
            failure_class=StatusClass.CONFIG_ERROR,
            message="attempt_id is required.",
        )

    workdir_path = Path(workdir).resolve()

    try:
        workdir_path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise VeritasRunnerError(
            failure_class=StatusClass.CONFIG_ERROR,
            message=f"Cannot create workdir at '{workdir_path}': {e}",
        ) from e

    if not workdir_path.is_dir():
        raise VeritasRunnerError(
            failure_class=StatusClass.CONFIG_ERROR,
            message=f"workdir is not a directory: {workdir_path}",
        )

    # Validate API URL and tokens...

    return workdir_path