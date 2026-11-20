"""Check a metrics.tsv against PathoEQA's real parser.

Usage:  python -m tests.contract.check_metrics_tsv path/to/metrics.tsv
"""
import sys
from pathlib import Path

from .metrics_contract import evaluate

if __name__ == "__main__":
    parsed, diagnostic = evaluate(Path(sys.argv[1]).read_bytes())
    if diagnostic is None:
        print(f"ACCEPTED: {len(parsed.metrics)} metric rows, sha256={parsed.sha256}")
    else:
        print(f"REJECTED: {diagnostic}")
        sys.exit(1)
