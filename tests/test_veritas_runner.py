from pathlib import Path
from unittest.mock import MagicMock, patch
import subprocess
import pytest

from veritas.veritas_runner.exceptions import (
    ErrorFactory,
    HttpFailureClassifier,
    HttpSurface,
    VeritasRunnerError,
)
from veritas.veritas_runner.runner import _check_storage_capacity, _run_veritas, _scan_metrics_for_undefined
from veritas.veritas_runner.status import StatusClass


# --- 1. HttpFailureClassifier Tests (exceptions.py) ---

def test_http_classifier_callback_surface_conflict_maps_to_invalid_callback():
    """HTTP 409 Conflict on CALLBACK surface must map to StatusClass.CALLBACK_INVALID."""
    status = HttpFailureClassifier.classify(409, HttpSurface.CALLBACK)
    assert status == StatusClass.CALLBACK_INVALID


def test_http_classifier_artefact_surface_not_found_maps_to_artefact_invalid():
    """HTTP 404 on ARTEFACT surface must map to StatusClass.ARTEFACT_INVALID."""
    status = HttpFailureClassifier.classify(404, HttpSurface.ARTEFACT)
    assert status == StatusClass.ARTEFACT_INVALID


def test_http_classifier_control_plane_503_maps_to_upstream_unavailable():
    """HTTP 503 Server Error on CONTROL_PLANE surface must map to StatusClass.UPSTREAM_UNAVAILABLE."""
    status = HttpFailureClassifier.classify(503, HttpSurface.CONTROL_PLANE)
    assert status == StatusClass.UPSTREAM_UNAVAILABLE


# --- 2. Veritas Subprocess Supervision Tests (runner.py) ---

def test_run_veritas_missing_binary_raises_executor_unavailable():
    """Missing 'veritas' binary on host PATH must raise EXECUTOR_UNAVAILABLE."""
    fail = ErrorFactory(attempt_id="att-123")
    paths = {
        "truth_vcf": Path("/tmp/truth.vcf.gz"),
        "rtg_sdf": Path("/tmp/rtg_sdf"),
        "query_vcf": Path("/tmp/query.vcf.gz"),
    }

    with patch("subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(VeritasRunnerError) as exc_info:
            _run_veritas(paths, "/tmp/out", timeout_s=30, fail=fail)
        assert exc_info.value.failure_class == StatusClass.EXECUTOR_UNAVAILABLE


def test_run_veritas_timeout_raises_processing_timeout():
    """Subprocess timeout must raise StatusClass.PROCESSING_TIMEOUT."""
    fail = ErrorFactory(attempt_id="att-123")
    paths = {
        "truth_vcf": Path("/tmp/truth.vcf.gz"),
        "rtg_sdf": Path("/tmp/rtg_sdf"),
        "query_vcf": Path("/tmp/query.vcf.gz"),
    }

    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="veritas", timeout=30)):
        with pytest.raises(VeritasRunnerError) as exc_info:
            _run_veritas(paths, "/tmp/out", timeout_s=30, fail=fail)
        assert exc_info.value.failure_class == StatusClass.PROCESSING_TIMEOUT


def test_run_veritas_missing_metrics_tsv_raises_metrics_missing(tmp_path):
    """Exit code 0 without producing metrics.tsv must raise StatusClass.METRICS_MISSING."""
    fail = ErrorFactory(attempt_id="att-123")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    paths = {
        "truth_vcf": Path("/tmp/truth.vcf.gz"),
        "rtg_sdf": Path("/tmp/rtg_sdf"),
        "query_vcf": Path("/tmp/query.vcf.gz"),
    }

    mock_completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=mock_completed):
        with pytest.raises(VeritasRunnerError) as exc_info:
            _run_veritas(paths, out_dir, timeout_s=30, fail=fail)
        assert exc_info.value.failure_class == StatusClass.METRICS_MISSING


# --- 3. Disk Space Pre-flight Capacity Tests (runner.py) ---

def test_check_storage_capacity_passes_when_disk_space_sufficient(tmp_path):
    """Validates storage capacity calculation using mock disk usage."""
    size_summary = {"total_files": 3, "missing_count": 0, "known_bytes": 1024 * 1024 * 100}  # 100 MB

    mock_usage = MagicMock()
    mock_usage.free = 100 * (1024 ** 3)  # 100 GiB free

    with patch("shutil.disk_usage", return_value=mock_usage):
        result = _check_storage_capacity(tmp_path, size_summary)
        assert result["passed"] is True


def test_check_storage_capacity_fails_when_disk_space_exhausted(tmp_path):
    """Storage check must fail if free space is below required bytes."""
    size_summary = {"total_files": 3, "missing_count": 0, "known_bytes": 100 * (1024 ** 3)}  # 100 GB

    mock_usage = MagicMock()
    mock_usage.free = 1 * (1024 ** 3)  # Only 1 GiB free

    with patch("shutil.disk_usage", return_value=mock_usage):
        result = _check_storage_capacity(tmp_path, size_summary)
        assert result["passed"] is False


# --- 4. Zero-Denominator Sentinel Metric Scan (runner.py) ---

def test_scan_metrics_for_undefined_detects_na_sentinel(tmp_path):
    """Scans metrics.tsv for 'NA' sentinel in Region 'ALL' rows."""
    metrics_file = tmp_path / "metrics.tsv"
    tsv_content = (
        "Category\tRegion\tPrecision\tRecall\n"
        "SNV\tALL\tNA\t1.0\n"
        "INDEL\tPRIMER BED\tNA\t0.5\n"
    )
    metrics_file.write_text(tsv_content)

    undefined = _scan_metrics_for_undefined(metrics_file)
    assert len(undefined) == 1
    assert undefined[0] == {"variant_type": "SNV", "metric": "Precision"}