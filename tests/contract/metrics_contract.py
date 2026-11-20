"""Run PathoEQA's own metrics TSV parser locally.

`pathoeqa_metrics.py` (next to this file) must be an UNMODIFIED copy of PathoEQA's
`metrics.py`. Only its two framework imports (Django's ValidationError and
`result_diagnostics`) are replaced at load time, so the validation rules are
exactly PathoEQA's. Re-copy that file whenever PathoEQA changes its TSV contract.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

VENDORED = Path(__file__).with_name("pathoeqa_metrics.py")


class _ValidationError(Exception):
    def __init__(self, message="", code=None):
        super().__init__(message)
        self.code = code


class _ResultDiagnostic:
    def __init__(self, code, **location):
        self.code = code
        self.location = location

    def __repr__(self):
        where = ", ".join(f"{k}={v!r}" for k, v in self.location.items())
        return f"{self.code} ({where})" if where else self.code


def load():
    """Return PathoEQA's metrics module, importable without Django."""
    source = VENDORED.read_text(encoding="utf-8")
    source = source.replace(
        "from django.core.exceptions import ValidationError", "ValidationError = _ValidationError"
    ).replace(
        "from .result_diagnostics import ResultDiagnostic", "ResultDiagnostic = _ResultDiagnostic"
    )
    module = types.ModuleType("pathoeqa_metrics")
    module.__dict__.update(_ValidationError=_ValidationError, _ResultDiagnostic=_ResultDiagnostic)
    sys.modules["pathoeqa_metrics"] = module
    exec(compile(source, str(VENDORED), "exec"), module.__dict__)
    return module


def evaluate(raw: bytes):
    """Return (parsed, None) if PathoEQA would accept the TSV, else (None, diagnostic)."""
    module = load()
    try:
        return module.parse_metrics_tsv(raw), None
    except module.VeritasMetricsError as exc:
        return None, exc.diagnostic
