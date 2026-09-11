"""
tests/test_integration_local.py

Level 1: local integration tests - the fast feedback loop.

Scope: everything from `run_attempt()` down, against a live mock PathoEQA over
real TLS and a fake `veritas` binary. No network egress, no GitHub, no
credentials. Typical wall time: a few seconds.

Out of scope here (covered by the Actions job in
.github/workflows/veritas-integration.yml): real OIDC token minting, the
workflow_dispatch input contract, and `python -m veritas_runner` running under
an actual runner environment.

What each test pins down maps 1:1 to the failure taxonomy in status.py.
"""

from __future__ import annotations

import uuid

import pytest

from veritas.veritas_runner.exceptions import VeritasRunnerError
from veritas.veritas_runner.runner import run_attempt
from veritas.veritas_runner.status import StatusClass


def _attempt_id() -> str:
    return str(uuid.uuid4())


def _run(server, workdir, attempt_id, **kwargs):
    return run_attempt(
        attempt_id=attempt_id,
        workdir=str(workdir),
        api_url=server.base_url,
        oidc_token="test-oidc-token",
        workflow_run_id=42,
        **kwargs,
    )


# ------------------------------------------------------------------ happy path


def test_completed_attempt_runs_samples_in_order(server, manifest_factory, fake_veritas, tmp_path):
    attempt_id = _attempt_id()
    server.manifest = manifest_factory(attempt_id, samples=3)

    result = _run(server, tmp_path / "wd", attempt_id)

    assert result.terminal_state == "Completed"
    assert result.status is StatusClass.SUCCESS
    assert result.exit_code == 0
    assert [o.sample_run_id for o in result.samples] == ["sr-001", "sr-002", "sr-003"]
    assert all(o.ok for o in result.samples)

    # Callback stream: one start, then started/completed per sample, then terminal.
    assert server.events() == [
        "attempt_started",
        "sample_started", "sample_completed",
        "sample_started", "sample_completed",
        "sample_started", "sample_completed",
        "attempt_completed",
    ]
    terminal = server.terminal_event()
    assert terminal["payload"]["samples_succeeded"] == 3
    assert terminal["payload"]["failure_class"] is None


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
    assert (sample_dir / "reference.fa").is_file()
    assert (sample_dir / "truth.vcf.gz").is_file()
    assert (sample_dir / "query.vcf.gz").is_file()
    assert (sample_dir / "rtg_sdf").is_dir(), "RTG SDF must be a directory, not a tarball"
    assert (sample_dir / "rtg_sdf" / "mainIndex").is_file()
    assert not list(sample_dir.glob("*.part")), "no partial files may survive"
    assert (sample_dir / "output" / "metrics.tsv").is_file()


def test_dry_run_downloads_but_never_invokes_veritas(server, manifest_factory, fake_veritas, tmp_path, monkeypatch):
    attempt_id = _attempt_id()
    server.manifest = manifest_factory(attempt_id, samples=1)
    monkeypatch.setenv("VERITAS_FAKE_MODE", "crash")  # would fail if it ran
    workdir = tmp_path / "wd"

    result = _run(server, workdir, attempt_id, dry_run=True)

    assert result.terminal_state == "Completed"
    assert (workdir / "sr-001" / "query.vcf.gz").is_file()
    assert not (workdir / "sr-001" / "output" / "metrics.tsv").exists()


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

    assert result.terminal_state == "Completed"
    assert server.manifest_requests == 3, "expected 1 try + 2 automatic retries"


def test_retry_budget_is_bounded(server, manifest_factory, fake_veritas, tmp_path):
    server.manifest_status = [503, 503, 503, 503]

    with pytest.raises(VeritasRunnerError) as excinfo:
        _run(server, tmp_path / "wd", _attempt_id())

    assert excinfo.value.failure_class is StatusClass.UPSTREAM_UNAVAILABLE
    assert server.manifest_requests == 3, "never more than 2 extra tries"


def test_auto_retry_can_be_switched_off(server, manifest_factory, fake_veritas, tmp_path, monkeypatch):
    """The feature flag is part of the contract - it must really disable retries."""
    monkeypatch.setenv("VERITAS_AUTO_RETRY", "0")
    import importlib
    from veritas_runner import retry as retry_module
    importlib.reload(retry_module)
    import veritas_runner.runner as runner_module
    importlib.reload(runner_module)

    server.manifest_status = [503, 503]
    try:
        with pytest.raises(VeritasRunnerError):
            runner_module.run_attempt(
                attempt_id=_attempt_id(),
                workdir=str(tmp_path / "wd"),
                api_url=server.base_url,
                oidc_token="test-oidc-token",
            )
        assert server.manifest_requests == 1
    finally:
        monkeypatch.delenv("VERITAS_AUTO_RETRY", raising=False)
        importlib.reload(retry_module)
        importlib.reload(runner_module)


def test_malformed_manifest_is_manifest_invalid(server, fake_veritas, tmp_path):
    server.manifest = {"schema_version": "1.0", "samples": []}  # missing required fields

    with pytest.raises(VeritasRunnerError) as excinfo:
        _run(server, tmp_path / "wd", _attempt_id())

    assert excinfo.value.failure_class is StatusClass.MANIFEST_INVALID


def test_manifest_for_a_different_attempt_is_rejected(server, manifest_factory, fake_veritas, tmp_path):
    """Guards against PathoEQA handing us someone else's work order."""
    server.manifest = manifest_factory(str(uuid.uuid4()), samples=1)

    with pytest.raises(VeritasRunnerError):
        _run(server, tmp_path / "wd", _attempt_id())


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
    assert result.terminal_state == "Failed"
    assert result.exit_code == 35
    assert server.artefact_requests["query.vcf.gz"] == 2, "integrity faults are never retried"
    assert not list((tmp_path / "wd").rglob("*.part"))


def test_truncated_download_is_caught(server, manifest_factory, fake_veritas, tmp_path):
    attempt_id = _attempt_id()
    server.manifest = manifest_factory(attempt_id, samples=1)
    server.truncate.add("reference.fa")

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
    server.artefact_status["reference.fa"] = [503]

    result = _run(server, tmp_path / "wd", attempt_id)

    assert result.terminal_state == "Completed"
    assert server.artefact_requests["reference.fa"] == 2


def test_unusable_sdf_archive_is_artefact_invalid(server, manifest_factory, fake_veritas, tmp_path):
    from .conftest import sha256

    attempt_id = _attempt_id()
    broken = b"this is not a gzip tarball"
    server.add_artefact("broken_sdf.tar.gz", broken)
    manifest = manifest_factory(attempt_id, samples=1)
    for f in manifest["samples"][0]["truth_bundle"]["files"]:
        if f["role"] == "rtg_sdf":
            f["url"] = server.artefact_url("broken_sdf.tar.gz")
            f["sha256"] = sha256(broken)
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

    assert result.samples[0].status is StatusClass.VERITAS_CRASHED
    assert result.terminal_state == "Failed"
    assert result.exit_code == 40
    assert server.events()[-1] == "attempt_failed"


def test_missing_metrics_is_reported(server, manifest_factory, fake_veritas, tmp_path, monkeypatch):
    attempt_id = _attempt_id()
    server.manifest = manifest_factory(attempt_id, samples=1)
    monkeypatch.setenv("VERITAS_FAKE_MODE", "no_metrics")

    result = _run(server, tmp_path / "wd", attempt_id)

    assert result.samples[0].status is StatusClass.METRICS_MISSING


def test_missing_veritas_binary_aborts_the_attempt(server, manifest_factory, tmp_path, monkeypatch):
    """CONFIG_ERROR is environmental: stop, do not burn the budget on sample 2."""
    attempt_id = _attempt_id()
    server.manifest = manifest_factory(attempt_id, samples=3)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))

    result = _run(server, tmp_path / "wd", attempt_id)

    assert result.samples[0].status is StatusClass.CONFIG_ERROR
    assert len(result.samples) == 1, "attempt must abort after an environment fault"
    assert result.exit_code == 20


# ------------------------------------------------------------- budget / partial


def test_deadline_stops_the_loop_and_reports_partial(
    server, manifest_factory, fake_veritas, tmp_path, monkeypatch
):
    """
    Sample 1 succeeds but eats the whole operational budget, so sample 2 is
    never started: the attempt is Partial, not Failed, and PathoEQA is told how
    many samples were left unprocessed.
    """
    attempt_id = _attempt_id()
    server.manifest = manifest_factory(attempt_id, samples=2, operational_deadline_seconds=6)
    server.slow_artefacts["query.vcf.gz"] = 5.5   # stalls inside sample 1
    monkeypatch.setenv("VERITAS_FAKE_MODE", "slow")  # tips it past the deadline

    result = _run(server, tmp_path / "wd", attempt_id)

    assert result.terminal_state == "Partial"
    assert result.status is StatusClass.DEADLINE_EXCEEDED
    assert result.exit_code == 38
    assert len(result.samples) == 1
    terminal = server.terminal_event()
    assert terminal["event_type"] == "attempt_partial"
    assert terminal["payload"]["samples_unprocessed"] == 1


def test_advisory_callback_loss_does_not_kill_a_healthy_run(
    server, manifest_factory, fake_veritas, tmp_path
):
    attempt_id = _attempt_id()
    server.manifest = manifest_factory(attempt_id, samples=1)
    server.callback_status = [500, 500, 500]  # kills only attempt_started

    result = _run(server, tmp_path / "wd", attempt_id)

    assert result.terminal_state == "Completed"
    assert server.terminal_event()["event_type"] == "attempt_completed"


def test_terminal_callback_failure_is_loud(server, manifest_factory, fake_veritas, tmp_path):
    """A lost terminal callback leaves the attempt Stale upstream - never swallow it."""
    attempt_id = _attempt_id()
    server.manifest = manifest_factory(attempt_id, samples=1)
    # start(1) + sample_started(1) + sample_completed(1) = 3 advisory, then terminal
    server.callback_status = [202, 202, 202, 500, 500, 500]

    with pytest.raises(VeritasRunnerError) as excinfo:
        _run(server, tmp_path / "wd", attempt_id)

    assert excinfo.value.failure_class is StatusClass.CALLBACK_FAILED
