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
import subprocess
import time
import shutil
from typing import Any, List, Optional
from pathlib import Path
import requests

from veritas_runner.datamodels import (
    Manifest,
    ManifestFile,
    SampleInput,
    SampleOutcome,
    AttemptResult,
)
from veritas_runner.exceptions import ErrorFactory, VeritasRunnerError
from veritas_runner.helpers import _validate_prerequisites
from veritas_runner.pathoeqa import PathoEQAClient
from veritas_runner.retry import CONTROL_PLANE, DATA_PLANE
from veritas_runner.status import StatusClass
from veritas_runner.artefacts import ArtefactClass
from veritas_runner.reporter import AttemptReporter

logger = logging.getLogger(__name__)


_BED_CLI_FLAGS = {
    "primer_bed": "--primerd-bed",
    "mask_bed": "--mask-bed",
    "low_cov_truth_bed": "--low-cov-truth-bed",
    "low_cov_query_bed": "--low-cov-query-bed",
}


# ------------------------------------------------------------------- reporting

class ExecutionAttempt:
    """Run an execution attempt and deliver callbackEnvelops"""

    def __init__(
        self, 
        attempt_id: str,
        workdir: str | Path, 
        api_url: str | None = None,
        oidc_token: str| None = None, 
        workflow_run_id: int = 0,
        dry_run: bool = False,
    ):

        api_url = api_url or os.getenv("PATHOEQA_API_URL", "")
        oidc_token = oidc_token or os.getenv("GITHUB_OIDC_TOKEN", "")
        
        _validate_prerequisites(attempt_id, workdir, api_url, oidc_token)
        
        self.attempt_id = attempt_id
        self.workdir = Path(workdir)
        self.dry_run = dry_run
        self.deadline: float | None = None

        self.session = requests.Session()
        self.client = PathoEQAClient(
            api_url, oidc_token, self.attempt_id, session=self.session
        )
        self.reporter = AttemptReporter(self.client, self.attempt_id, workflow_run_id)
        

    def _materialize_sample(
        self,
        sample: SampleInput,
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
        artefact_handler = ArtefactClass(session=self.session, attempt_id=self.attempt_id)

        sample_dir = self.workdir / sample.sample_run_id

        required_truth_roles = {"truth_vcf", "truth_tbi", "rtg_sdf"}
        if sample.query_type == "fasta":
            required_truth_roles.add("reference_fasta")

        to_fetch: list[ManifestFile] = [f for f in sample.truth_bundle.files if f.role in required_truth_roles] + [sample.query_input]

        if sample.region_annotations:
            ann = sample.region_annotations
            to_fetch.extend(filter(None, [ann.primer_bed, ann.mask_bed, ann.low_cov_truth_bed, ann.low_cov_query_bed]))

        paths: dict[str, Path] = {}

        for artefact in to_fetch:
            filename = artefact.url.split("?")[0].rsplit("/", 1)[-1]
            dest = sample_dir / f"{artefact.role}_{filename}"

            fetched = DATA_PLANE.run(
                lambda : artefact_handler.download(artefact, dest, deadline=self.deadline),
                description=f"download {artefact.role}",
                deadline=self.deadline,
            )
            paths[artefact.role] = fetched

        if "rtg_sdf" in paths:
            paths["rtg_sdf"] = artefact_handler.prepare_rtg_sdf(
                paths["rtg_sdf"],
                sample_dir / "rtg_sdf"
            )

        if "truth_tbi" in paths:
            artefact_handler.enforce_truth_vcf_index(paths["truth_vcf"], paths["truth_tbi"])

        return paths


    def _process_sample(
        self,
        sample: SampleInput,
        per_sample_timeout_s: int,
    ) -> SampleOutcome:
        """
        Execute the evaluation workflow for a single genomic sample.

        Emits a start notification before downloading and preparing the sample's input
        artefacts to disk. Runs the core Veritas benchmarking engine and validates
        generated metrics unless operating in dry-run mode. Catches all internal and
        unexpected exceptions, converting them into a standardized outcome model, and
        delivers a terminal completion or failure callback.

        Parameters
        ----------
        sample : SampleInput
            Manifest, file specifications, and configuration metadata for the sample run.
        per_sample_timeout_s : int
            Maximum execution time allocated for the Veritas subprocess in seconds.

        Returns
        -------
        SampleOutcome
            Encapsulated result containing execution success status, duration in
            milliseconds, and failure details or classification if an error occurred.
        """
        started = time.monotonic()
        sample_run_id = sample.sample_run_id
        output_dir = self.workdir.joinpath(sample_run_id, "output")

        self.reporter.emit(
            "sample_started",
            sample_run_id=sample_run_id,
            payload={"sample_order": sample.sample_order},
            deadline=self.deadline,
            best_effort=True,
        )

        try:
            materialize_stated = time.monotonic()
            paths = self._materialize_sample(sample)
            materialize_duration_ms = int((time.monotonic() - materialize_stated) * 1000)
            logger.debug("Materialized sample %s in %dms", sample_run_id, materialize_duration_ms)

            if not self.dry_run:
                output_dir.mkdir(exist_ok=True, parents=True)
                fail = ErrorFactory(attempt_id=self.attempt_id, sample_run_id=sample_run_id)
                _run_veritas(paths, output_dir, per_sample_timeout_s, fail)
            outcome = SampleOutcome(sample_run_id, StatusClass.SUCCESS)
        except VeritasRunnerError as e:
            outcome = SampleOutcome(sample_run_id, e.failure_class, str(e))
        except Exception as e:
            wrapped = VeritasRunnerError.wrap(
                e, context=f"processing sample {sample_run_id}",
                attempt_id=self.attempt_id, sample_run_id=sample_run_id,
            )
            outcome = SampleOutcome(sample.sample_run_id, wrapped.failure_class, str(wrapped))

        outcome.duration_ms = int((time.monotonic() - started) * 1000)

        self.reporter.emit(
            "sample_completed" if outcome.success else "sample_failed",
            sample_run_id=sample_run_id,
            payload={
                "duration_ms": outcome.duration_ms,
                **({} if outcome.success else {"failure_class": outcome.status.value, "detail": outcome.message[:2000]}),
            },
            deadline=self.deadline,
            best_effort=True,
        )
        return outcome

    def run_attempt(
        self,
        fail: ErrorFactory
   ) -> AttemptResult:
        """
        Execute one execution attempt end-to-end and return its terminal state.

        Orchestrates sample processing for the execution attempt while enforcing
        operational deadlines, pre-flight storage capacity validation, and lifecycle
        callback reporting. Terminal states follow SPEC-05 guidelines: "Completed"
        (all samples succeeded), "Partial" (some samples succeeded before stopping),
        or "Failed" (no usable sample processing). Once determined, terminal states
        are emitted once and treated as immutable.

        Parameters
        ----------
        fail : ErrorFactory
            Contextual exception factory bound to the active execution attempt.

        Returns
        -------
        AttemptResult
            Data model capturing terminal state classification, overall execution status,
            duration in milliseconds, runner software version, and list of per-sample
            outcomes.
        """
        started = time.monotonic()

        try:
            manifest: Manifest = CONTROL_PLANE.run(
                self.client.fetch_manifest, description="manifest fetch"
            )
            
            self.deadline = time.monotonic() + manifest.operational_deadline_seconds

            samples = manifest.samples

            self.reporter.emit(
                "attempt_started",
                payload={"sample_count": len(samples)},
                deadline=self.deadline,
                best_effort=True,
            )

            outcomes: List[SampleOutcome] = []
            stopped_early: Optional[VeritasRunnerError] = None

            for sample in sorted(samples, key=lambda s: s.sample_order):
                if time.monotonic() > self.deadline:
                    stopped_early = fail(
                        StatusClass.DEADLINE_EXCEEDED,
                        (
                            f"Operational deadline reached after {len(outcomes)}/"
                            f"{len(samples)} samples; remaining samples left pending."
                        )
                    ).bind(attempt_id=self.attempt_id)
                    
                    break

                capacity = _check_storage_capacity(self.workdir, sample.total_size)
                if not capacity["passed"]:
                    stopped_early = fail(
                        StatusClass.SYSTEM_RESOURCE_EXHAUSTED,
                        (
                            f"Insufficient disk space in '{self.workdir}'. "
                            f"Available: {capacity['free_GiB']:.2f} GiB, "
                            f"Required: {capacity['est_needed_GiB']:.2f} GiB."
                        ),
                    ).bind(attempt_id=self.attempt_id)
                    
                    break

                outcome = self._process_sample(
                    sample,
                    per_sample_timeout_s=max(1, int(self.deadline - time.monotonic())),
                )

                if outcome.status in (StatusClass.CONFIG_ERROR, StatusClass.AUTH_REJECTED):
                    stopped_early = fail(
                        failure_class=outcome.status,
                        message=f"Aborting attempt: {outcome.message}",
                    ).bind(attempt_id=self.attempt_id)
                    break

                outcomes.append(outcome)

            succeeded = [s for s in outcomes if s.success]
            unprocessed = len(samples) - len(outcomes)

            if succeeded and len(succeeded) == len(samples):
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

            self.reporter.emit(
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
                attempt_id=self.attempt_id,
                terminal_state=terminal_state,
                status=status,
                duration_ms=duration_ms,
                veritas_version=veritas_ver,
                samples=outcomes,
            )
        finally:
            self.client.close()



def _run_veritas(
        paths: dict[str, Path], 
        output_dir: str | Path, 
        timeout_s: int,
        fail: ErrorFactory
    ) -> None:
    """Triggers and supervises the Veritas subprocess.

    Relies on upstream boundary validation for input correctness; maps 
    subprocess exit codes and system failures to domain-specific 
    ``StatusClass`` exceptions via the provided error factory.

    Parameters
    ----------
    paths : dict of str to pathlib.Path
        Materialized file paths containing required artifacts (truth VCF, 
        RTG SDF, and query VCF or FASTA).
    output_dir : str or pathlib.Path
        Directory where Veritas will write output files and metrics.
    timeout_s : int
        Maximum execution time allocated for the subprocess in seconds.
    fail : ErrorFactory
        Callable factory pre-bound with attempt metadata to construct 
        standardized ``VeritasRunnerError`` exceptions.

    Raises
    ------
    VeritasRunnerError
        Raised when the Veritas binary is missing, times out, encounters 
        scientific incompatibility (exit code 2), invalid input (exit code 3), 
        crashes unexpectedly, or exits with code 0 without producing ``metrics.tsv``.
    """
    assert "truth_vcf" in paths and "rtg_sdf" in paths, "Input error: Missing truth artifacts"
    assert ("query_vcf" in paths) ^ ("query_fasta" in paths), "Input error: Ambiguous query input"
    assert "query_fasta" not in paths or "reference_fasta" in paths, "Input error: FASTA missing reference"

    str_paths = {k: str(v) for k,v in paths.items()}

    cmd = ["veritas", "validate", "--output-dir", str(output_dir)]
    if "query_vcf" in str_paths:
        cmd += ["--query-vcf", str_paths["query_vcf"]]
    if "query_fasta" in paths:
        cmd += ["--query-fasta", str_paths["query_fasta"], "--reference", str_paths["reference_fasta"]]
    cmd += ["--truth-vcf", str_paths["truth_vcf"], "--rtg-reference", str_paths["rtg_sdf"]]

    for role, flag in _BED_CLI_FLAGS.items():
        if role in paths:
            cmd += [flag, str_paths[role]]

    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=True)
    except FileNotFoundError as e:
        raise fail(
            StatusClass.EXECUTOR_UNAVAILABLE,
            "Veritas binary missing on host PATH.",
        ) from e
    
    except subprocess.TimeoutExpired as e:
        raise fail(
            StatusClass.PROCESSING_TIMEOUT,
            f"Veritas validate exceeded {timeout_s}s and was terminated.",
        ) from e

    except subprocess.CalledProcessError as e:
        detail = e.stderr.strip().splitlines()[-1] if e.stderr else f"Exit code {e.returncode}"

        if e.returncode == 2:
            raise fail(
                StatusClass.SCIENTIFIC_INCOMPATIBILITY,
                f"Veritas rejected sample alignment or genome build: {detail}",
            ) from e
        if e.returncode == 3:
            raise fail(
                StatusClass.INVALID_INPUT,
                f"Veritas reported invalid arguments or inputs: {detail}",
            ) from e
        raise fail(
            StatusClass.INTERNAL_ERROR,
            f"Veritas process crashed unexpectedly ({detail}).",
        ) from e

    metrics_file = output_dir / "metrics.tsv"
    if not metrics_file.is_file() or metrics_file.stat().st_size == 0:
        raise fail(
            StatusClass.METRICS_MISSING,
            "Veritas exited with status 0 but failed to generate non-empty metrics.tsv.",
        )


def _veritas_version() -> str:
    try:
        res = subprocess.run(
            ["veritas", "--version"], capture_output=True, text=True, check=True, timeout=30
        )
        return res.stdout.strip()
    except Exception:
        return "unknown"


def _check_storage_capacity(
    target_dir: Path,
    size_summary: dict[str, Any],
    *,
    safety_multiplier: float = 2.5,
    fallback_min_gb: float = 50.0,
) -> dict[str, float | bool]:
    """
    Verify that the target filesystem has sufficient free disk space.

    Evaluates available disk space against estimated required space calculated
    from sample manifest size metadata, applying a safety headroom multiplier
    for intermediate extraction and output files. Logs a warning if manifest
    metadata contains unspecified file sizes (None).

    Parameters
    ----------
    target_dir : Path
        Target working directory where downloaded artifacts, extracted archives,
        and operational outputs will be stored.
    size_summary : dict of str to Any
        Summary dictionary (typically from ``SampleInput.total_size``) containing
        keys ``total_files``, ``missing_count``, and ``known_bytes``.
    safety_multiplier : float, default=2.5
        Headroom factor applied to ``known_bytes`` to account for temporary
        ``.part`` files, archive decompression (e.g., RTG SDF), and output files.
    fallback_min_gb : float, default=50.0
        Minimum floor limit in GiB enforced if ``known_bytes`` is zero or if
        one or more artifact sizes are missing (None).

    Returns
    -------
    dict of str to (float or bool)
        Capacity evaluation result containing:

        * ``"est_needed_GiB"`` : float
            Estimated disk space required in GiB.
        * ``"free_GiB"`` : float
            Available free space on the target filesystem in GiB.
        * ``"passed"`` : bool
            True if available space meets or exceeds estimated requirements.
    """
    total_files = size_summary["total_files"]
    missing_count = size_summary["missing_count"]
    known_bytes = size_summary["known_bytes"]

    if missing_count > 0:
        logger.warning(
            "Manifest metadata incomplete: %d of %d artifact(s) have unspecified sizes (None). "
            "Capacity estimation may underestimate disk requirements; enforcing fallback minimum of %.1f GiB.",
            missing_count,
            total_files,
            fallback_min_gb,
        )

    if known_bytes > 0:
        needed_bytes = int(known_bytes * safety_multiplier)
        needed_bytes = max(needed_bytes, int(fallback_min_gb * (1024**3)))
    else:
        needed_bytes = int(fallback_min_gb * (1024**3))

    check_dir = target_dir
    while not check_dir.exists() and check_dir.parent != check_dir:
        check_dir = check_dir.parent

    free_bytes = shutil.disk_usage(check_dir).free
    passed = free_bytes >= needed_bytes

    if passed:
        logger.info(
            "Storage check passed for '%s': %.2f GiB free (required estimate: %.2f GiB)",
            target_dir,
            free_bytes / (1024**3),
            needed_bytes / (1024**3),
        )

    return {
        "est_needed_GiB": needed_bytes / (1024**3),
        "free_GiB": free_bytes / (1024**3),
        "passed": passed,
    }