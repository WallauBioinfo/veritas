# veritas_runner/runner.py
#
# Orchestration for one ExecutionAttempt.
#
#   1. PHASE BOUNDARIES. Phase 1a (manifest fetch) failure exits via code.
#      Phase 1b+ failures are reported via PathoEQA callbacks.
#   2. SEQUENTIAL SAMPLE LOOP, with per-sample callbacks and deadline guard.
#   3. Request-level retry lives in veritas_runner.retry.
#

from __future__ import annotations

import logging
import os
from posix import P_ALL
import subprocess
from sys import path
import time
import uuid
from datetime import datetime, timezone
from typing import List, Optional
from pathlib import Path
from veritas.veritas_runner.datamodels import ManifestFile

import requests

from veritas_runner.artefacts import (
    download_artefact,
    enforce_truth_vcf_index,
    prepare_rtg_sdf,
)
from veritas_runner.datamodels import CallbackEnvelope, Manifest, SampleInput, SampleOutcome, AttemptResult
from veritas_runner.exceptions import VeritasRunnerError
from veritas_runner.helpers import _validate_prerequisites
from veritas_runner.pathoeqa import PathoEQAClient
from veritas_runner.retry import with_auto_retry
from veritas_runner.status import StatusClass
from veritas_runner.artefacts import ArtefactClass

logger = logging.getLogger(__name__)

CALLBACK_SCHEMA_VERSION = os.environ.get("VERITAS_CALLBACK_SCHEMA_VERSION", "1.0")

_BED_CLI_FLAGS = {
    "primer_bed": "--primerd-bed",
    "mask_bed": "--mask-bed",
    "low_cov_truth_bed": "--low-cov-truth-bed",
    "low_cov_query_bed": "--low-cov-query-bed",
}


# ------------------------------------------------------------------- reporting


class AttemptReporter:
    """
    Build and deliver CallbackEnvelopes for a single execution attempt.

    Parameters
    ----------
    client : PathoEQAClient
        Active API client instance used to transmit HTTP callback envelopes.
    attempt_id : str
        Unique identifier binding emitted events to the current execution attempt.
    workflow_run_id : int
        Platform workflow execution identifier (e.g., GitHub Actions run ID).

    """"""Builds and delivers CallbackEnvelopes for one attempt."""

    def __init__(self, client: PathoEQAClient, attempt_id: str, workflow_run_id: int):
        self.client = client
        self.attempt_id = attempt_id
        self.workflow_run_id = workflow_run_id

    def emit(
        self,
        event_type: str,
        *,
        sample_run_id: Optional[str] = None,
        payload: Optional[dict] = None,
        deadline: Optional[float] = None,
        best_effort: bool = False,
    ) -> None:
        """
        Send a structured callback envelope to PathoEQA.

        Parameters
        ----------
        event_type : str
            Lifecycle event name (e.g., 'sample_completed').
        sample_run_id : str, optional
            Target sample run ID, if applicable.
        payload : dict, optional
            Event payload data.
        deadline : float, optional
            Monotonic time threshold for retries.
        best_effort : bool, default=False
            If True, drop delivery failures as warnings instead of raising.

        Raises
        ------
        VeritasRunnerError
            If delivery fails and `best_effort` is False.
        """

        envelope = CallbackEnvelope(
            schema_version=CALLBACK_SCHEMA_VERSION,
            event_id=str(uuid.uuid4()),
            attempt_id=self.attempt_id,
            workflow_run_id=self.workflow_run_id,
            event_type=event_type,
            occurred_at=datetime.now(timezone.utc).isoformat(),
            sample_run_id=sample_run_id,
            payload=payload or {},
        )
        try:
            with_auto_retry(
                lambda: self.client.send_callback(envelope),
                description=f"callback {event_type}",
                deadline=deadline,
            )
        except VeritasRunnerError:
            if not best_effort:
               raise
            logger.warning("Dropped advisory callback %s", event_type)


# ------------------------------------------------------------- phases 1b / 2 / 3


def _materialize_sample(
    sample: SampleInput,
    workdir: str,
    session: requests.Session,
    attempt_id: str,
    deadline: Optional[float],
) -> dict[str, Path]:
    """
    Download and prepare all input artefacts for a single sample execution.

    Materializes the truth bundle, query input, and optional region annotations
    into a dedicated sample subdirectory. Inputs are fetched lazily at sample
    execution time, and artefacts are downloaded sequentially for each sample.
    Local caching, file existence checks, and deduplication are handled internally
    by `ArtefactClass.download`. Also manages post-download preparation for RTG
    SDF reference directories and VCF index verification.

    Parameters
    ----------
    sample : SampleInput
        Manifest and metadata for the sample run to materialize.
    workdir : str
        Base directory path under which the sample-specific directory is created.
    session : requests.Session
        Active HTTP session used for artefact network downloads.
    attempt_id : str
        Unique identifier for the current execution attempt.
    deadline : float, optional
        Monotonic time threshold (`time.monotonic()`) for download retries.

    Returns
    -------
    dict[str, Path]
        Mapping of artefact roles (e.g., `'truth_vcf'`, `'query_input'`,
        `'rtg_sdf'`) to their resolved local `Path` locations on disk.

    Raises
    ------
    VeritasRunnerError
        If any artefact download fails after exhausting retries or if RTG SDF
        preparation fails.
    """
    artefact_handler = ArtefactClass(session=session, attempt_id=attempt_id)
    
    sample_dir = Path(workdir) / sample.sample_run_id
    
    to_fetch: list[ManifestFile] = [*sample.truth_bundle.files, sample.query_input]

    if sample.region_annotations:
        ann = sample.region_annotations
        to_fetch.extend(filter(None, [ann.primer_bed, ann.mask_bed, ann.low_cov_truth_bed, ann.low_cov_query_bed]))

    paths: dict[str, Path] = {}
    
    for artefact in to_fetch:
        filename = artefact.url.split("?")[0].rsplit("/", 1)[-1]
        dest = sample_dir / f"{artefact.role}_{filename}"
        
        fetched = with_auto_retry(
            lambda : artefact_handler.download(artefact, dest, deadline=deadline),
            description=f"download {artefact.role}",
            deadline=deadline,
        )
        paths[artefact.role] = fetched

    if "rtg_sdf" in paths:
        paths["rtg_sdf"] = prepare_rtg_sdf(
            paths["rtg_sdf"],
            sample_dir / "rtg_sdf",
            attempt_id=attempt_id,
        )

    if "truth_tbi" in paths:
        artefact_handler.enforce_truth_vcf_index(paths["truth_vcf"], paths["truth_tbi"])

    return paths


def _run_veritas(paths: dict, output_dir: str, timeout_s: int) -> None:
    """
    Phase 2. Supervises the veritas subprocess. Concerned only with what a
    supervisor can observe from outside; veritas's own scientific errors are
    surfaced through its exit code and the metrics it does or does not write.
    """
    cmd = ["veritas", "validate", "--output-dir", output_dir]
    if "query_vcf" in paths:
        cmd += ["--query-vcf", paths["query_vcf"]]
    if "query_fasta" in paths:
        cmd += [
            "--query-fasta",
            paths["query_fasta"],
            "--reference",
            paths["reference_fasta"],
        ]
    cmd += ["--truth-vcf", paths["truth_vcf"], "--rtg-reference", paths["rtg_sdf"]]

    for role, flag in _BED_CLI_FLAGS.items():
        if role in paths:
            cmd += [flag, paths[role]]

    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=True)
    except subprocess.TimeoutExpired as e:
        raise VeritasRunnerError(
            failure_class=StatusClass.TIMEOUT,
            message=f"veritas validate exceeded {timeout_s}s and was terminated.",
        ) from e
    except FileNotFoundError as e:
        raise VeritasRunnerError(
            failure_class=StatusClass.CONFIG_ERROR,
            message="veritas executable not found on PATH.",
        ) from e
    except subprocess.CalledProcessError as e:
        raise VeritasRunnerError(
            failure_class=StatusClass.VERITAS_CRASHED,
            message=f"veritas validate exited with code {e.returncode}.",
        ) from e


def _check_metrics(output_dir: str) -> None:
    """Phase 3, output half. Structural only - presence and shape, never quality."""
    expected = ["metrics.tsv"]
    missing = [f for f in expected if not os.path.exists(os.path.join(output_dir, f))]
    if missing:
        raise VeritasRunnerError(
            failure_class=StatusClass.METRICS_MISSING,
            message=f"Expected output(s) missing: {', '.join(missing)}",
        )


def _veritas_version() -> str:
    try:
        res = subprocess.run(
            ["veritas", "--version"], capture_output=True, text=True, check=True, timeout=30
        )
        return res.stdout.strip()
    except Exception:
        return "unknown"


# --------------------------------------------------------------- the sample loop


def _process_sample(
    sample: SampleInput,
    workdir: str,
    session: requests.Session,
    attempt_id: str,
    reporter: AttemptReporter,
    deadline: Optional[float],
    per_sample_timeout_s: int,
    dry_run: bool,
) -> SampleOutcome:
    started = time.monotonic()
    output_dir = os.path.join(workdir, sample.sample_run_id, "output")

    reporter.emit(
        "sample_started",
        sample_run_id=sample.sample_run_id,
        payload={"sample_order": sample.sample_order},
        deadline=deadline,
        best_effort=True,
    )

    try:
        paths = _materialize_sample(sample, workdir, session, attempt_id, deadline)
        if not dry_run:
            os.makedirs(output_dir, exist_ok=True)
            _run_veritas(paths, output_dir, per_sample_timeout_s)
            _check_metrics(output_dir)
        outcome = SampleOutcome(sample.sample_run_id, StatusClass.SUCCESS)
    except VeritasRunnerError as e:
        outcome = SampleOutcome(sample.sample_run_id, e.failure_class, str(e))
    except Exception as e:
        wrapped = VeritasRunnerError.wrap(
            e, context=f"processing sample {sample.sample_run_id}",
            attempt_id=attempt_id, sample_run_id=sample.sample_run_id,
        )
        outcome = SampleOutcome(sample.sample_run_id, wrapped.failure_class, str(wrapped))

    outcome.duration_ms = int((time.monotonic() - started) * 1000)

    reporter.emit(
        "sample_completed" if outcome.success else "sample_failed",
        sample_run_id=sample.sample_run_id,
        payload={
            "duration_ms": outcome.duration_ms,
            **({} if outcome.success else {"failure_class": outcome.status.value, "detail": outcome.message[:2000]}),
        },
        deadline=deadline,
        best_effort=True,
    )
    return outcome


def run_attempt(
    attempt_id: str,
    workdir: str,
    api_url: Optional[str] = None,
    oidc_token: Optional[str] = None,
    workflow_run_id: int = 0,
    dry_run: bool = False,
) -> AttemptResult:
    """
    Execute one ExecutionAttempt end to end and return its terminal state.

    Terminal states, per SPEC-05: Completed (all samples success), Partial (some success,
    then a stop), Failed (nothing usable). Terminal is immutable - the runner
    reports once and exits. It never re-dispatches itself.
    """
    started = time.monotonic()
    api_url = api_url or os.environ.get("PATHOEQA_API_URL", "")
    oidc_token = oidc_token or os.environ.get("GITHUB_OIDC_TOKEN", "")

    _validate_prerequisites(attempt_id, workdir, api_url, oidc_token)

    session = requests.Session()
    client = PathoEQAClient(api_url, oidc_token, attempt_id, session=session)

    try:
        # --- Phase 1a: no manifest yet, so no callback channel exists. Failures
        # here can only be signalled through the process exit code.
        manifest: Manifest = with_auto_retry(
            client.fetch_manifest, description="manifest fetch"
        )

        deadline = time.monotonic() + manifest.operational_deadline_seconds
        reporter = AttemptReporter(client, attempt_id, workflow_run_id)

        # --- Phase 1b onward: a manifest exists, so every outcome is reportable.
        reporter.emit(
            "attempt_started",
            payload={"sample_count": len(manifest.samples)},
            deadline=deadline,
            best_effort=True,
        )

        outcomes: List[SampleOutcome] = []
        stopped_early: Optional[VeritasRunnerError] = None

        for sample in sorted(manifest.samples, key=lambda s: s.sample_order):
            if time.monotonic() > deadline:
                stopped_early = VeritasRunnerError(
                    failure_class=StatusClass.DEADLINE_EXCEEDED,
                    message=(
                        f"Operational deadline reached after {len(outcomes)}/"
                        f"{len(manifest.samples)} samples; remaining samples left pending."
                    ),
                    attempt_id=attempt_id,
                )
                break

            outcome = _process_sample(
                sample,
                workdir,
                session,
                attempt_id,
                reporter,
                deadline,
                per_sample_timeout_s=max(1, int(deadline - time.monotonic())),
                dry_run=dry_run,
            )
            outcomes.append(outcome)

            # An environment fault is not sample-specific - every remaining sample
            # would hit it too. Stop and let PathoEQA decide, rather than burning
            # the budget producing identical failures.
            if outcome.status in (StatusClass.CONFIG_ERROR, StatusClass.AUTH_REJECTED):
                stopped_early = VeritasRunnerError(
                    failure_class=outcome.status,
                    message=f"Aborting attempt: {outcome.message}",
                    attempt_id=attempt_id,
                )
                break

        succeeded = [s for s in outcomes if s.success]
        unprocessed = len(manifest.samples) - len(outcomes)

        if succeeded and len(succeeded) == len(manifest.samples):
            terminal_state, status, event = "Completed", StatusClass.SUCCESS, "attempt_completed"
        elif succeeded:
            terminal_state, event = "Partial", "attempt_partial"
            status = stopped_early.failure_class if stopped_early else StatusClass.SUCCESS
        else:
            terminal_state, event = "Failed", "attempt_failed"
            status = (
                stopped_early.failure_class
                if stopped_early
                else (outcomes[0].status if outcomes else StatusClass.INTERNAL_ERROR)
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        veritas_ver = _veritas_version()

        # Terminal callback is NOT best-effort. If PathoEQA never learns the
        # outcome the attempt goes `Stale`, which is worse than a loud failure.
        reporter.emit(
            event,
            payload={
                "terminal_state": terminal_state,
                "duration_ms": duration_ms,
                "veritas_version": veritas_ver,
                "samples_total": len(manifest.samples),
                "samples_succeeded": len(succeeded),
                "samples_unprocessed": unprocessed,
                "failure_class": None if status is StatusClass.SUCCESS else status.value,
            },
        )

        return AttemptResult(
            attempt_id=attempt_id,
            terminal_state=terminal_state,
            status=status,
            duration_ms=duration_ms,
            veritas_version=veritas_ver,
            samples=outcomes,
        )
    finally:
        client.close()
