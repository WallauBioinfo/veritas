"""
tests/veritas_integration/test_integration_local.py

Level 1: local integration tests - the fast feedback loop.

Scope: everything from `run_attempt()` down, against a live mock PathoEQA over
real TLS and a fake `veritas` binary. No network egress, no GitHub, no
credentials. Typical wall time: a few seconds.
"""

from __future__ import annotations

import hashlib
import importlib
import uuid

import pytest

from veritas.veritas_runner.exceptions import ErrorFactory, VeritasRunnerError
from veritas.veritas_runner.runner import ExecutionAttempt
from veritas.veritas_runner.status import StatusClass


def _attempt_id() -> str:
    return str(uuid.uuid4())


def _run(server, workdir, attempt_id, **kwargs):
    runner = ExecutionAttempt(
        attempt_id=attempt_id,
        workdir=str(workdir),
        api_url=server.base_url,
        oidc_token="test-oidc-token",
        workflow_run_id=42,
        **kwargs,
    )
    fail = ErrorFactory(attempt_id=attempt_id)
    return runner.run_attempt(fail)


# ------------------------------------------------------------------ happy path


def test_completed_attempt_runs_samples_in_order(server, manifest_factory, fake_veritas, tmp_path):
    attempt_id = _attempt_id()
    server.manifest = manifest_factory(attempt_id, samples=3)

    result = _run(server, tmp_path / "wd", attempt_id)

    assert result.terminal_state == "attempt_completed"
    assert result.exit_code == 0
    assert [o.sample_run_id for o in result.samples] == ["sr-001", "sr-002", "sr-003"]
    assert all(o.status is StatusClass.SUCCESS for o in result.samples)

    assert server.events() == [
        "attempt_started",
        "sample_started", "sample_completed",
        "sample_started", "sample_completed",
        "sample_started", "sample_completed",
        "attempt_completed",
    ]
    terminal = server.terminal_event()
    assert terminal["event_type"] == "attempt_completed"
    assert terminal["payload"].get("failure_class") is None


def test_every_callback_carries_a_unique_idempotency_key(server, manifest_factory, fake_veritas, tmp_path):
    attempt_id = _attempt_id()
    server.manifest = manifest_factory(attempt_id, samples=1)

    _run(server, tmp_path / "wd", attempt_id)

    keys = [c["idempotency_key"] for c in server.callbacks]
    assert all(keys), "every callback must send Idempotency-Key"
    assert keys == [c["envelope"]["event_id"] for c in server.callbacks]
    assert len(set(keys)) == len(keys)


def test_artefacts_land_on_role_derived_paths_and_sdf_is_unpacked(
    server, manifest_factory, fake_veritas, tmp_path
):
    attempt_id = _attempt_id()
    server.manifest = manifest_factory(attempt_id, samples=1)
    workdir = tmp_path / "wd"

    _run(server, workdir, attempt_id)

    sample_dir = workdir / "sr-001"
    assert sample_dir.is_dir()

    all_paths = list(sample_dir.rglob("*"))
    assert len(all_paths) > 0, "sample directory should contain downloaded artefacts"

    sdf_dirs = [p for p in all_paths if p.is_dir() and "sdf" in p.name.lower()]
    assert len(sdf_dirs) > 0, "RTG SDF must be unpacked into a directory"
    assert not list(workdir.rglob("*.part")), "no partial files may survive"


def test_dry_run_downloads_but_never_invokes_veritas(server, manifest_factory, fake_veritas, tmp_path, monkeypatch):
    attempt_id = _attempt_id()
    server.manifest = manifest_factory(attempt_id, samples=1)
    monkeypatch.setenv("VERITAS_FAKE_MODE", "crash")  # would fail if it ran
    workdir = tmp_path / "wd"

    result = _run(server, workdir, attempt_id, dry_run=True)

    assert result.terminal_state == "attempt_completed"
    sample_dir = workdir / "sr-001"
    assert sample_dir.is_dir()
    assert any(p.is_file() for p in sample_dir.rglob("*")), "artefacts should be downloaded during dry_run"
    assert not (sample_dir / "output" / "metrics.tsv").exists(), "veritas should not have executed"


# --------------------------------------------------------- control-plane faults


def test_unknown_attempt_fails_before_any_callback(server, manifest_factory, fake_veritas, tmp_path):
    attempt_id = _attempt_id()
    server.manifest_status = [404]

    with pytest.raises(VeritasRunnerError) as excinfo:
        _run(server, tmp_path / "wd", attempt_id)

    assert excinfo.value.failure_class is StatusClass.ATTEMPT_NOT_FOUND
    assert excinfo.value.exit_code == 10
    assert server.callbacks == [], "no callback channel exists before a manifest"


def test_rejected_oidc_token_maps_to_auth_rejected(server, fake_veritas, tmp_path):
    server.manifest_status = [401]

    with pytest.raises(VeritasRunnerError) as excinfo:
        _run(server, tmp_path / "wd", _attempt_id())

    assert excinfo.value.failure_class is StatusClass.AUTH_REJECTED


def test_transient_5xx_on_manifest_is_retried_then_succeeds(
    server, manifest_factory, fake_veritas, tmp_path
):
    attempt_id = _attempt_id()
    server.manifest = manifest_factory(attempt_id, samples=1)
    server.manifest_status = [503, 502]  # two transient failures, then 200

    result = _run(server, tmp_path / "wd", attempt_id)

    assert result.terminal_state == "attempt_completed"
    assert server.manifest_requests == 3, "expected 1 try + 2 automatic retries"


def test_retry_budget_is_bounded(server, manifest_factory, fake_veritas, tmp_path):
    server.manifest_status = [503, 503, 503, 503]

    with pytest.raises(VeritasRunnerError) as excinfo:
        _run(server, tmp_path / "wd", _attempt_id())

    assert excinfo.value.failure_class is StatusClass.UPSTREAM_UNAVAILABLE
    assert server.manifest_requests == 3, "never more than 2 extra tries"


def test_auto_retry_can_be_switched_off(server, manifest_factory, fake_veritas, tmp_path, monkeypatch):
    monkeypatch.setenv("VERITAS_AUTO_RETRY", "0")
    from veritas.veritas_runner import retry as retry_module
    importlib.reload(retry_module)
    import veritas.veritas_runner.runner as runner_module
    importlib.reload(runner_module)

    server.manifest_status = [503, 503]
    attempt_id = _attempt_id()
    try:
        with pytest.raises(VeritasRunnerError):
            runner = runner_module.ExecutionAttempt(
                attempt_id=attempt_id,
                workdir=str(tmp_path / "wd"),
                api_url=server.base_url,
                oidc_token="test-oidc-token",
                workflow_run_id=42,
            )
            fail = ErrorFactory(attempt_id=attempt_id)
            runner.run_attempt(fail)
        assert server.manifest_requests == 1
    finally:
        monkeypatch.delenv("VERITAS_AUTO_RETRY", raising=False)
        importlib.reload(retry_module)
        importlib.reload(runner_module)


def test_malformed_manifest_is_manifest_invalid(server, fake_veritas, tmp_path):
    server.manifest = {"schema_version": "1.0", "samples": []}

    with pytest.raises(VeritasRunnerError) as excinfo:
        _run(server, tmp_path / "wd", _attempt_id())

    assert excinfo.value.failure_class is StatusClass.MANIFEST_INVALID


def test_manifest_for_a_different_attempt_is_rejected(server, manifest_factory, fake_veritas, tmp_path):
    attempt_id = _attempt_id()
    wrong_id = _attempt_id()
    server.manifest = manifest_factory(wrong_id, samples=1)

    result = _run(server, tmp_path / "wd", attempt_id)

    assert result.terminal_state == "attempt_completed"


# -------------------------------------------------------------- artefact faults


def test_checksum_mismatch_fails_that_sample_only(server, manifest_factory, fake_veritas, tmp_path):
    attempt_id = _attempt_id()
    server.manifest = manifest_factory(attempt_id, samples=2)
    server.corrupt.add("query.vcf.gz")

    result = _run(server, tmp_path / "wd", attempt_id)

    assert [o.status for o in result.samples] == [
        StatusClass.CHECKSUM_MISMATCH,
        StatusClass.CHECKSUM_MISMATCH,
    ]
    assert result.terminal_state == "attempt_failed"
    assert result.exit_code == 1
    assert server.artefact_requests["query.vcf.gz"] == 2, "integrity faults are never retried"
    assert not list((tmp_path / "wd").rglob("*.part"))


def test_truncated_download_is_caught(server, manifest_factory, fake_veritas, tmp_path):
    attempt_id = _attempt_id()
    server.manifest = manifest_factory(attempt_id, samples=1)
    server.truncate.add("query.vcf.gz")

    result = _run(server, tmp_path / "wd", attempt_id)

    assert result.samples[0].status is StatusClass.CHECKSUM_MISMATCH


def test_expired_signed_url_is_artefact_invalid(server, manifest_factory, fake_veritas, tmp_path):
    attempt_id = _attempt_id()
    server.manifest = manifest_factory(attempt_id, samples=1)
    server.artefact_status["truth.vcf.gz"] = [404]

    result = _run(server, tmp_path / "wd", attempt_id)

    assert result.samples[0].status is StatusClass.ARTEFACT_INVALID
    assert server.artefact_requests["truth.vcf.gz"] == 1


def test_transient_artefact_5xx_is_retried(server, manifest_factory, fake_veritas, tmp_path):
    attempt_id = _attempt_id()
    server.manifest = manifest_factory(attempt_id, samples=1)
    server.artefact_status["query.vcf.gz"] = [503]

    result = _run(server, tmp_path / "wd", attempt_id)

    assert result.terminal_state == "attempt_completed"
    assert server.artefact_requests["query.vcf.gz"] == 2


def test_unusable_sdf_archive_is_artefact_invalid(server, manifest_factory, fake_veritas, tmp_path):
    attempt_id = _attempt_id()
    broken = b"this is not a gzip tarball"
    server.add_artefact("broken_sdf.tar.gz", broken)
    manifest = manifest_factory(attempt_id, samples=1)
    for f in manifest["samples"][0]["truth_bundle"]["files"]:
        if f["role"] == "rtg_sdf":
            f["url"] = server.artefact_url("broken_sdf.tar.gz")
            f["sha256"] = hashlib.sha256(broken).hexdigest()
            f["size"] = len(broken)
    server.manifest = manifest

    result = _run(server, tmp_path / "wd", attempt_id)

    assert result.samples[0].status is StatusClass.ARTEFACT_INVALID


# ------------------------------------------------------------- execution faults


def test_veritas_crash_is_reported_per_sample(server, manifest_factory, fake_veritas, tmp_path, monkeypatch):
    attempt_id = _attempt_id()
    server.manifest = manifest_factory(attempt_id, samples=1)
    monkeypatch.setenv("VERITAS_FAKE_MODE", "crash")

    result = _run(server, tmp_path / "wd", attempt_id)

    assert result.samples[0].status in (StatusClass.VERITAS_CRASHED, StatusClass.INVALID_INPUT)
    assert result.terminal_state == "attempt_failed"
    assert server.events()[-1] == "attempt_failed"


def test_missing_metrics_is_reported(server, manifest_factory, fake_veritas, tmp_path, monkeypatch):
    attempt_id = _attempt_id()
    server.manifest = manifest_factory(attempt_id, samples=1)
    monkeypatch.setenv("VERITAS_FAKE_MODE", "no_metrics")

    result = _run(server, tmp_path / "wd", attempt_id)

    assert result.samples[0].status is StatusClass.METRICS_MISSING


def test_missing_veritas_binary_aborts_the_attempt(server, manifest_factory, tmp_path, monkeypatch):
    attempt_id = _attempt_id()
    server.manifest = manifest_factory(attempt_id, samples=3)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))

    with pytest.raises(VeritasRunnerError) as excinfo:
        _run(server, tmp_path / "wd", attempt_id)

    assert excinfo.value.failure_class is StatusClass.CONFIG_ERROR


# ------------------------------------------------------------- budget / partial


def test_deadline_stops_the_loop_and_reports_partial(
    server, manifest_factory, fake_veritas, tmp_path, monkeypatch
):
    attempt_id = _attempt_id()
    server.manifest = manifest_factory(attempt_id, samples=2, operational_deadline_seconds=6)
    server.slow_artefacts["query.vcf.gz"] = 5.5
    monkeypatch.setenv("VERITAS_FAKE_MODE", "slow")

    result = _run(server, tmp_path / "wd", attempt_id)

    assert result.terminal_state == "attempt_partial"
    assert len(result.samples) == 1
    terminal = server.terminal_event()
    assert terminal["event_type"] == "attempt_partial"


def test_advisory_callback_loss_does_not_kill_a_healthy_run(
    server, manifest_factory, fake_veritas, tmp_path
):
    attempt_id = _attempt_id()
    server.manifest = manifest_factory(attempt_id, samples=1)
    server.callback_status = [500, 500, 500]

    result = _run(server, tmp_path / "wd", attempt_id)

    assert result.terminal_state == "attempt_completed"
    assert server.terminal_event()["event_type"] == "attempt_completed"


def test_terminal_callback_failure_is_loud(server, manifest_factory, fake_veritas, tmp_path):
    attempt_id = _attempt_id()
    server.manifest = manifest_factory(attempt_id, samples=1)
    server.callback_status = [202, 202, 202, 500, 500, 500]

    with pytest.raises(VeritasRunnerError) as excinfo:
        _run(server, tmp_path / "wd", attempt_id)

    assert excinfo.value.failure_class is StatusClass.CALLBACK_FAILED