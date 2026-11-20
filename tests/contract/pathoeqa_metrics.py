"""Bounded local TSV contract for PV-04; not an external delivery API."""

import csv
from dataclasses import dataclass, field
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import hashlib
from io import StringIO
import re

from django.core.exceptions import ValidationError

from .result_diagnostics import ResultDiagnostic


METRICS_MAX_BYTES = 256 * 1024
METRICS_MAX_ROWS = 128
METRICS_PARSER_VERSION = "pathoeqa-tsv-v1-decimal28"
METRICS_FORMAT_VERSION = "pathoeqa-metrics-tsv-1"
METRIC_RATIO_ABSOLUTE_TOLERANCE = Decimal("1e-15")
HEADER = ("Category", "Region", "TP", "FP", "FN", "Precision", "Recall", "F1-Score")
EXPECTED_KEYS = frozenset((c, r) for c in ("SNV", "INDEL") for r in ("ALL", "PRIMER BED"))
PRIVATE_EXTRA_REGIONS = frozenset(("MASK BED", "LOW_COV_TRUTH BED", "LOW_COV_QUERY BED"))
_INTEGER = re.compile(r"[0-9]+")
_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?(?:[eE]([+-]?[0-9]+))?")


class VeritasMetricsError(ValidationError):
    def __init__(self, code="invalid_metrics_tsv", **location):
        super().__init__("Veritas metrics do not satisfy the local TSV contract.", code=code)
        self.diagnostic = ResultDiagnostic(code, **location)


@dataclass(frozen=True)
class MetricRow:
    category: str
    region: str
    tp: int
    fp: int
    fn: int
    precision_raw: str
    recall_raw: str
    f1_raw: str
    precision_evaluable: bool
    recall_evaluable: bool
    f1_evaluable: bool

    def as_model_fields(self):
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class ParsedMetrics:
    raw_bytes: bytes = field(repr=False)
    sha256: str
    byte_size: int
    metrics: tuple[MetricRow, ...]
    parser_version: str = METRICS_PARSER_VERSION
    format_version: str = METRICS_FORMAT_VERSION


def _count(text, **location):
    if len(text) > 80 or _INTEGER.fullmatch(text) is None:
        raise VeritasMetricsError("invalid_metric_count", **location)
    value = int(text)
    if value > 2**63 - 1:
        raise VeritasMetricsError("invalid_metric_count", **location)
    return value


def _ratio(text, **location):
    if not text:
        raise VeritasMetricsError("metric_required", **location)
    if len(text) > 80:
        raise VeritasMetricsError("invalid_metric_ratio", **location)
    match = _DECIMAL.fullmatch(text)
    if match is None:
        raise VeritasMetricsError("invalid_metric_ratio", **location)
    if match[1] is not None and not -80 <= int(match[1]) <= 80:
        raise VeritasMetricsError("invalid_metric_ratio", **location)
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise VeritasMetricsError("invalid_metric_ratio", **location) from None
    if not value.is_finite() or not 0 <= value <= 1:
        raise VeritasMetricsError("invalid_metric_ratio", **location)
    return value


def metric_ratio_matches(reported, expected, *, evaluable):
    """Compare a reported decimal with the canonical count-derived ratio."""
    if not reported.is_finite() or not 0 <= reported <= 1:
        return False
    if not evaluable:
        return reported == 0
    # Keep the tolerance comparison exact across the bounded TSV mantissa and exponent.
    with localcontext(Context(prec=192)):
        return abs(reported - expected) <= METRIC_RATIO_ABSOLUTE_TOLERANCE


def parse_metrics_tsv(raw):
    """Validate the TSV contract and preserve the reported bytes and values."""
    if type(raw) is not bytes or not 0 < len(raw) <= METRICS_MAX_BYTES:
        raise VeritasMetricsError()
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise VeritasMetricsError() from None
    reader = csv.reader(StringIO(text, newline=""), delimiter="\t", strict=True)
    rows, keys = [], set()
    try:
        if tuple(next(reader, ())) != HEADER:
            raise VeritasMetricsError("invalid_tsv_header", row=1)
        for index, cells in enumerate(reader, 1):
            if index > METRICS_MAX_ROWS or len(cells) != len(HEADER) or any(len(c) > 256 for c in cells):
                raise VeritasMetricsError("invalid_tsv_row", row=index + 1)
            category, region, tp_text, fp_text, fn_text, *ratios = cells
            key = (category, region)
            if category not in {"SNV", "INDEL"} or region not in {"ALL", "PRIMER BED"} | PRIVATE_EXTRA_REGIONS:
                raise VeritasMetricsError("unknown_metric_row", row=index + 1)
            location = dict(row=index + 1, category=category, region=region)
            if key in keys:
                raise VeritasMetricsError("duplicate_metric_row", **location)
            keys.add(key)
            tp, fp, fn = (_count(value, column=column, **location)
                          for column, value in zip(HEADER[2:5], (tp_text, fp_text, fn_text)))
            if region in PRIVATE_EXTRA_REGIONS:
                for column, ratio in zip(HEADER[5:], ratios):
                    if ratio:
                        _ratio(ratio, column=column, **location)
                continue
            values = tuple(_ratio(ratio, column=column, **location)
                           for column, ratio in zip(HEADER[5:], ratios))
            denominators = (tp + fp, tp + fn, 2 * tp + fp + fn)
            with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
                expected = tuple(Decimal(n) / d if d else Decimal(0)
                    for n, d in zip((tp, tp, 2 * tp), denominators))
            for column, value, target, denominator in zip(HEADER[5:], values, expected, denominators):
                if not metric_ratio_matches(value, target, evaluable=bool(denominator)):
                    code = "metric_counts_mismatch" if denominator else "zero_denominator_metric"
                    raise VeritasMetricsError(code, column=column, **location)
            rows.append(MetricRow(category, region, tp, fp, fn, *ratios,
                *(bool(d) for d in denominators)))
    except csv.Error:
        raise VeritasMetricsError() from None
    missing = EXPECTED_KEYS - keys
    if missing:
        category, region = min(missing)
        raise VeritasMetricsError("metrics_incomplete", category=category, region=region)
    return ParsedMetrics(raw, hashlib.sha256(raw).hexdigest(), len(raw), tuple(rows))
