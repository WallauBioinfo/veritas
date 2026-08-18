# veritas_runner/runner.py
#
# Orchestration for one ExecutionAttempt.
#
#   1. SEQUENTIAL SAMPLE LOOP, with per-sample callbacks and a deadline
#      guard so a stall becomes `Partial` rather than a silent hard kill.
#   2. Request-level retry lives in veritas_runner.retry and is optional: set
#      VERITAS_AUTO_RETRY=0 to disable it. Attempt-level retry (a new attempt_id and a
#      new workflow_dispatch) is PathoEQA's responsibility
#

from __future__ import annotations

import logging
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

import requests

from veritas_runner.artefacts import (
    download_artefact,
    enforce_truth_vcf_index,
    prepare_rtg_sdf,
)
from veritas_runner.datamodels import CallbackEnvelope, Manifest, SampleInput
from veritas_runner.exceptions import VeritasRunnerError
from veritas_runner.helpers import _validate_prerequisites
from veritas_runner.pathoeqa import PathoEQAClient
from veritas_runner.retry import with_auto_retry  # remove this line to drop retries
from veritas_runner.status import StatusClass

logger = logging.getLogger(__name__)

CALLBACK_SCHEMA_VERSION = os.environ.get("VERITAS_CALLBACK_SCHEMA_VERSION", "1.0")

# Optional region BED files — passed to veritas validate only when downloaded.
_BED_CLI_FLAGS = {
    "primer_bed": "--primerd-bed",
    "mask_bed": "--mask-bed",
    "low_cov_truth_bed": "--low-cov-truth-bed",
    "low_cov_query_bed": "--low-cov-query-bed",
}


# --------------------------------------------------------------------- results


@dataclass
class SampleOutcome:
    sample_run_id: str
    status: StatusClass
    message: str = ""
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status is StatusClass.SUCCESS


@dataclass
class AttemptResult:
    attempt_id: str
    terminal_state: str  # "Completed" | "Partial" | "Failed"
    status: StatusClass
    duration_ms: int
    veritas_version: str
    samples: List[SampleOutcome] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return self.status.exit_code


# ------------------------------------------------------------------- reporting


class AttemptReporter:
    """Builds and delivers CallbackEnvelopes for one attempt."""

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
            # Progress callbacks are advisory. Losing one must not abort a run
            # that is otherwise healthy; the terminal callback carries the truth.
            logger.warning("Dropped advisory callback %s", event_type)


# ------------------------------------------------------------- phases 1b / 2 / 3


def _materialize_sample(
    sample: SampleInput,
    workdir: str,
    session: requests.Session,
    attempt_id: str,
    deadline: Optional[float],
) -> dict:
    """
    Phase 1b, per sample. Downloads the truth bundle and the query input into a
    dedicated directory. Downloads are sequential and per-sample rather than
    up-front, so a Partial attempt never pays to fetch inputs for samples it
    will not reach.
    """
    sample_dir = os.path.join(workdir, sample.sample_run_id)
    paths: dict = {}

    to_fetch = list(sample.truth_bundle.files) + [sample.query_input]
    if sample.region_annotations is not None:
        to_fetch += [
            f
            for f in (
                sample.region_annotations.primer_bed,
                sample.region_annotations.mask_bed,
                sample.region_annotations.low_cov_truth_bed,
                sample.region_annotations.low_cov_query_bed,
            )
            if f is not None
        ]

    for artefact in to_fetch:
        dest = os.path.join(
            sample_dir,
            f"{artefact.role}_{os.path.basename(artefact.url.split('?')[0])}",
        )
        paths[artefact.role] = with_auto_retry(
            lambda a=artefact, d=dest: download_artefact(
                a, d, session=session, attempt_id=attempt_id, deadline=deadline
            ),
            description=f"download {artefact.role}",
            deadline=deadline,
        )

    paths["rtg_sdf"] = prepare_rtg_sdf(
        paths["rtg_sdf"],
        os.path.join(sample_dir, "rtg_sdf"),
        attempt_id=attempt_id,
    )

    if "truth_tbi" in paths:
        enforce_truth_vcf_index(paths["truth_vcf"], paths["truth_tbi"])

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
        "sample_completed" if outcome.ok else "sample_failed",
        sample_run_id=sample.sample_run_id,
        payload={
            "duration_ms": outcome.duration_ms,
            **({} if outcome.ok else {"failure_class": outcome.status.value, "detail": outcome.message[:2000]}),
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

    Terminal states, per SPEC-05: Completed (all samples ok), Partial (some ok,
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

        succeeded = [o for o in outcomes if o.ok]
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
