import argparse
import json
import sys
import os

from veritas_runner.exceptions import VeritasRunnerError
from veritas_runner.failures import FailureClass
from veritas_runner.runner import run_attempt


def main() -> int:
    parser = argparse.ArgumentParser(prog="veritas_runner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run-attempt")
    run_parser.add_argument("--attempt-id", required=True, help="The unique ID of the analysis attempt")
    run_parser.add_argument("--output-dir", os.getenv("VERITAS_OUTPUT_DIR", "./output"), 
        help="Directory for logs and reports (Env: VERITAS_OUTPUT_DIR)")
    run_parser.add_argument("--timeout", type=int, 
        default=int(os.getenv("VERITAS_TIMEOUT", "900")), 
        help="Execution timeout in seconds (Env: VERITAS_TIMEOUT)")
    run_parser.add_argument("--dry-run", action="store_true",  help="Resolve and validate inputs only, do not run Veritas")

    args = parser.parse_args()

    try:
        result = run_attempt(
            attempt_id=args.attempt_id,
            output_dir=args.output_dir,
            timeout_seconds=args.timeout,
            dry_run=args.dry_run,
        )
        print(json.dumps({
            "attempt_id": result.attempt_id,
            "class": result.failure_class.value,
            "duration_ms": result.duration_ms,
            "veritas_version": result.veritas_version,
        }))
        return 0

    except VeritasRunnerError as e:
        print(json.dumps({
            "attempt_id": args.attempt_id,
            "class": e.failure_class.value,
            "duration_ms": None,
            "message": str(e),
        }))
        print(str(e), file=sys.stderr)
        return 1

    except Exception as e:
        # Safety net: anything unclassified still exits 1 and still
        # emits the JSON line, falling back to INTERNAL_ERROR.
        print(json.dumps({
            "attempt_id": args.attempt_id,
            "class": FailureClass.INTERNAL_ERROR.value,
            "duration_ms": None,
            "message": str(e),
        }))
        print(f"unhandled exception: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())