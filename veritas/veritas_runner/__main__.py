import argparse
import json
import os
import sys

from veritas_runner.exceptions import ErrorFactory, VeritasRunnerError
from veritas_runner.runner import ExecutionAttempt
from veritas_runner.status import StatusClass


def main() -> int:
    parser = argparse.ArgumentParser(prog="veritas_runner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run-attempt")
    run_parser.add_argument(
        "--attempt-id",
        required=True,
        help="The unique ID of the analysis attempt",
    )
    run_parser.add_argument(
        "--workdir",
        "--output-dir",
        dest="workdir",
        default=os.getenv("VERITAS_OUTPUT_DIR", "./output"),
        help="Directory for workspace, logs, and reports (Env: VERITAS_OUTPUT_DIR)",
    )
    run_parser.add_argument(
        "--api-url",
        default=os.getenv("PATHOEQA_API_URL"),
        help="PathoEQA API URL (Env: PATHOEQA_API_URL)",
    )
    run_parser.add_argument(
        "--oidc-token",
        default=os.getenv("GITHUB_OIDC_TOKEN"),
        help="GitHub OIDC token (Env: GITHUB_OIDC_TOKEN)",
    )
    run_parser.add_argument(
        "--workflow-run-id",
        type=int,
        default=int(os.getenv("GITHUB_RUN_ID", "0")),
        help="GitHub workflow run ID (Env: GITHUB_RUN_ID)",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve and validate inputs only, do not run Veritas",
    )

    args = parser.parse_args()

    attempt_id = args.attempt_id

    try:
        fail = ErrorFactory(attempt_id=attempt_id)
        attempt = ExecutionAttempt(
            attempt_id=attempt_id,
            workdir=args.workdir,
            api_url=args.api_url,
            oidc_token=args.oidc_token,
            workflow_run_id=args.workflow_run_id,
            dry_run=args.dry_run,
        )
        result = attempt.run_attempt(fail)

        print(
            json.dumps({
                "attempt_id": result.attempt_id,
                "terminal_state": result.terminal_state,
                "duration_ms": result.duration_ms,
                "veritas_version": result.veritas_version,
            })
        )
        return result.exit_code

    except VeritasRunnerError as e:
        status_val = e.failure_class.value if hasattr(e, "failure_class") else "RUNNER_ERROR"
        exit_code = getattr(e.failure_class, "exit_code", 1) if hasattr(e, "failure_class") else 1

        print(
            json.dumps({
                "attempt_id": attempt_id,
                "status": status_val,
                "duration_ms": None,
                "message": str(e),
            })
        )
        print(str(e), file=sys.stderr)
        return exit_code

    except Exception as e:
        status_val = StatusClass.INTERNAL_ERROR.value if hasattr(StatusClass, "INTERNAL_ERROR") else "INTERNAL_ERROR"
        exit_code = getattr(StatusClass.INTERNAL_ERROR, "exit_code", 1) if hasattr(StatusClass, "INTERNAL_ERROR") else 1

        print(
            json.dumps({
                "attempt_id": attempt_id,
                "status": status_val,
                "duration_ms": None,
                "message": str(e),
            })
        )
        print(f"Unknown exception: {e}", file=sys.stderr)
        return exit_code


if __name__ == "__main__":
    sys.exit(main())