# veritas_runner/helpers.py
#
# Local checks performed before any PathoEQA network call.

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from urllib.parse import urlparse

from .exceptions import VeritasRunnerError
from .status import StatusClass


def validate_prerequisites(
    attempt_id: str,
    workdir: str | Path,
    api_url: str,
    oidc_token: str,
) -> Path:
    """
    Validate local environment and caller inputs before any network I/O,
    and return the canonicalized workdir Path for the caller to use.

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

    if not workdir or (isinstance(workdir, str) and not workdir.strip()):
        raise VeritasRunnerError(
            StatusClass.CONFIG_ERROR,
            "workdir is missing or empty.",
            attempt_id=attempt_id,
        )


def normalize_workdir(workdir: str | Path, attempt_id: str) -> Path:
    workdir_path = Path(workdir).resolve()

    try:
        workdir_path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise VeritasRunnerError(
            StatusClass.CONFIG_ERROR,
            f"Could not create workspace '{workdir_path}': {e}",
            attempt_id=attempt_id,
        ) from e

    if not os.access(workdir_path, os.W_OK):
        raise VeritasRunnerError(
            StatusClass.CONFIG_ERROR,
            f"Workspace '{workdir_path}' is not writable.",
            attempt_id=attempt_id,
        )

    return workdir_path