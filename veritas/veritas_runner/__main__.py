# veritas_runner/__main__.py
#
# CLI entrypoint. Invoked by .github/workflows/veritas-executor.yml as:
#
#     python -m veritas_runner run-attempt --attempt-id "$ATTEMPT_ID"
#
# Contract:
#   * stdout  -> one JSON object (the attempt result). Machine-readable.
#   * stderr  -> human/CI logs.
#   * exit    -> StatusClass.exit_code bucket (0 success/partial, >0 failure class).

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Optional

import requests

from .exceptions import ErrorFactory, VeritasRunnerError
from .runner import ExecutionAttempt
from .status import StatusClass

logger = logging.getLogger("veritas_runner")

OIDC_DEFAULT_AUDIENCE = "pathoeqa"


# --------------------------------------------------------------------- helpers

def _configure_logging(verbosity: str) -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, verbosity.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _env_int(name: str, default: int = 0) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        return int(raw)
    except ValueError:
        return default


def _resolve_oidc_token(
    explicit: Optional[str],
    audience: str,
    fail: ErrorFactory,
) -> str:
    """
    Obtain the GitHub OIDC ID token this attempt authenticates to PathoEQA with.

    Order of resolution:
      1. --oidc-token / GITHUB_OIDC_TOKEN  (self-hosted or local testing)
      2. The Actions ID-token service, using ACTIONS_ID_TOKEN_REQUEST_URL and
         ACTIONS_ID_TOKEN_REQUEST_TOKEN, which GitHub injects into every step of
         a job declaring `permissions: id-token: write`.

    The token is never logged.
    """
    if explicit and explicit.strip():
        logger.info("Using OIDC token supplied by the caller.")
        return explicit.strip()

    req_url = os.getenv("ACTIONS_ID_TOKEN_REQUEST_URL")
    req_token = os.getenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not req_url or not req_token:
        raise fail(
            StatusClass.CONFIG_ERROR,
            "No OIDC token available: neither --oidc-token/GITHUB_OIDC_TOKEN nor "
            "ACTIONS_ID_TOKEN_REQUEST_URL/TOKEN are set. In GitHub Actions the job "
            "must declare `permissions: id-token: write`.",
        )

    try:
        response = requests.get(
            req_url,
            params={"audience": audience},
            headers={"Authorization": f"Bearer {req_token}"},
            timeout=(10, 15),
        )
        response.raise_for_status()
        token = response.json().get("value")
    except requests.RequestException as e:
        raise fail(
            StatusClass.UPSTREAM_UNAVAILABLE,
            f"Could not reach the GitHub OIDC token service: {e}",
        ) from e
    except ValueError as e:
        raise fail(
            StatusClass.INTERNAL_ERROR,
            f"GitHub OIDC token service returned a non-JSON body: {e}",
        ) from e

    if not token:
        raise fail(
            StatusClass.CONFIG_ERROR,
            "GitHub OIDC token service returned an empty token value.",
        )

    logger.info("Minted GitHub OIDC ID token (audience=%s).", audience)
    return token


def _emit(record: dict, exit_code: int) -> int:
    """Write the single stdout JSON line and mirror key fields into Actions."""
    line = json.dumps(record, separators=(",", ":"))
    print(line, flush=True)

    gh_output = os.getenv("GITHUB_OUTPUT")
    if gh_output:
        try:
            with open(gh_output, "a", encoding="utf-8") as fh:
                fh.write(f"attempt_id={record.get('attempt_id', '')}\n")
                fh.write(f"terminal_state={record.get('terminal_state', '')}\n")
                fh.write(f"failure_class={record.get('failure_class', '')}\n")
                fh.write(f"exit_code={exit_code}\n")
        except OSError as e:  # never let reporting break the exit contract
            logger.warning("Could not write GITHUB_OUTPUT: %s", e)

    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(f"### veritas-runner\n\n```json\n{line}\n```\n")
        except OSError:
            pass

    return exit_code


# ------------------------------------------------------------------ arg parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="veritas_runner")
    parser.add_argument(
        "--log-level",
        default=os.getenv("VERITAS_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="stderr log verbosity (Env: VERITAS_LOG_LEVEL)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run-attempt",
        help="Fetch the manifest for an attempt and run every sample in order.",
    )
    run_parser.add_argument(
        "--attempt-id",
        required=True,
        help="PathoEQA ExecutionAttempt UUID",
    )
    run_parser.add_argument(
        "--workdir",
        "--output-dir",
        dest="workdir",
        default=os.getenv("VERITAS_OUTPUT_DIR", "./output"),
        help="Directory for workspace, logs and reports (Env: VERITAS_OUTPUT_DIR)",
    )
    run_parser.add_argument(
        "--api-url",
        default=os.getenv("PATHOEQA_API_URL"),
        help="PathoEQA base URL, HTTPS (Env: PATHOEQA_API_URL)",
    )
    run_parser.add_argument(
        "--oidc-token",
        default=os.getenv("GITHUB_OIDC_TOKEN"),
        help="Pre-minted OIDC ID token; omit inside Actions to mint one "
             "(Env: GITHUB_OIDC_TOKEN)",
    )
    run_parser.add_argument(
        "--oidc-audience",
        default=os.getenv("PATHOEQA_OIDC_AUDIENCE", OIDC_DEFAULT_AUDIENCE),
        help="Audience claim PathoEQA validates (Env: PATHOEQA_OIDC_AUDIENCE)",
    )
    run_parser.add_argument(
        "--workflow-run-id",
        type=int,
        default=_env_int("GITHUB_RUN_ID"),
        help="GitHub workflow run ID (Env: GITHUB_RUN_ID)",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve, download and validate inputs only; do not run Veritas",
    )
    return parser


# ------------------------------------------------------------------------ main

def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.log_level)

    attempt_id = args.attempt_id
    fail = ErrorFactory(attempt_id=attempt_id)
    attempt: Optional[ExecutionAttempt] = None

    try:
        oidc_token = _resolve_oidc_token(args.oidc_token, args.oidc_audience, fail)

        attempt = ExecutionAttempt(
            attempt_id=attempt_id,
            workdir=args.workdir,
            api_url=args.api_url,
            oidc_token=oidc_token,
            workflow_run_id=args.workflow_run_id,
            dry_run=args.dry_run,
        )
        result = attempt.run_attempt(fail)

        record = {
            "attempt_id": result.attempt_id,
            "terminal_state": result.terminal_state,
            "failure_class": StatusClass.SUCCESS.value,
            "duration_ms": result.duration_ms,
            "veritas_version": result.veritas_version,
            "samples_total": len(result.samples),
            "samples_completed": sum(1 for s in result.samples if s.is_completed),
            "dry_run": args.dry_run,
        }
        return _emit(record, result.exit_code)

    except VeritasRunnerError as e:
        logger.error("%s", e)
        record = {
            "attempt_id": attempt_id,
            "terminal_state": "attempt_failed",
            "failure_class": e.failure_class.value,
            "spec_failure_class": e.failure_class.spec_failure_class,
            "duration_ms": None,
            "message": str(e),
            "dry_run": getattr(args, "dry_run", False),
        }
        return _emit(record, e.exit_code)

    except KeyboardInterrupt:
        logger.error("Interrupted.")
        return _emit(
            {
                "attempt_id": attempt_id,
                "terminal_state": "attempt_failed",
                "failure_class": StatusClass.PROCESSING_TIMEOUT.value,
                "message": "Interrupted by signal (job cancelled or timed out).",
            },
            StatusClass.PROCESSING_TIMEOUT.exit_code,
        )

    except Exception as e:  # noqa: BLE001 - last resort, must not escape
        logger.exception("Unhandled exception in veritas_runner.")
        return _emit(
            {
                "attempt_id": attempt_id,
                "terminal_state": "attempt_failed",
                "failure_class": StatusClass.INTERNAL_ERROR.value,
                "message": f"{type(e).__name__}: {e}",
            },
            StatusClass.INTERNAL_ERROR.exit_code,
        )

    finally:
        session = getattr(attempt, "session", None)
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    sys.exit(main())
