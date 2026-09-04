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

import csv
import logging
import os
import resource
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, List, Optional
import requests

from .datamodels import (
    Manifest,
    ManifestFile,
    SampleInput,
    SampleOutcome,
    AttemptResult,
)
from .exceptions import ErrorFactory, VeritasRunnerError
from .helpers import validate_prerequisites, normalize_workdir
from .pathoeqa import PathoEQAClient
from .retry import CONTROL_PLANE, DATA_PLANE, VERITAS_ENGINE
from .status import StatusClass
from .artefacts import ArtefactClass
from .reporter import AttemptReporter

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
        
        validate_prerequisites(attempt_id, workdir, api_url, oidc_token)
        
        self.attempt_id = attempt_id
        self.workdir = normalize_workdir(workdir)
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
            Mapping of artefact roles (e.g., `'truth_vcf'`, `'query_fasta'`,
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

    def _get_dir_size_mb(self, path: Path) -> float:
        """
        Calculate the total disk space consumed by a directory in megabytes.

        Parameters
        ----------
        path : Path
            Target directory path to inspect.

        Returns
        -------
        float
            Total accumulated file size in MiB rounded to two decimal places, or
            0.0 if the directory does not exist.
        """
        if not path.exists():
            return 0.0
        total_bytes = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        return round(total_bytes / (1024 ** 2), 2)


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

        materialize_duration_ms: int = 0
        veritas_duration_ms: int = 0
        peak_memory_mb: float = 0.0

        # TODO: implement non_evaluable and completed_with_warnings sample classification
        # TODO: implement (N-proportion etc.)
        undefined_metrics: list[dict[str, str]] = []
        warnings_found: list[str] = []
                                         
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

                mem_before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
                veritas_started = time.monotonic()

                VERITAS_ENGINE.run(
                    lambda: _run_veritas(paths, output_dir, per_sample_timeout_s, fail),
                    description=f"veritas validate {sample_run_id}",
                    deadline=self.deadline,
                )

                veritas_duration_ms = int((time.monotonic() - veritas_started) * 1000)
                mem_after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
                peak_memory_mb = round(max(mem_before, mem_after) / 1024, 2)
            
            
            # TODO: logic below is currently unreachable
            
            undefined_metrics = _scan_metrics_for_undefined(output_dir / "metrics.tsv")

            if undefined_metrics:
                status = StatusClass.NOT_EVALUABLE
                state = "sample_not_evaluable"
                detail = "; ".join(
                    f"{m['variant_type']}/{m['metric']}" for m in undefined_metrics
                )
                message = f"Undefined due to genuine zero-denominator: {detail}."
            elif warnings_found:
                status = StatusClass.SUCCESS  # TODO: needs its own StatusClass
                state = "sample_completed_with_warnings"
                message = f"Sample evaluated with warnings: {'; '.join(warnings_found)}"
            else:
                status = StatusClass.SUCCESS
                state = "sample_completed"
                message = ""

            outcome = SampleOutcome(
                sample_run_id,
                StatusClass.SUCCESS,
                "sample_completed", # TODO: replace with state
                "message")

        except VeritasRunnerError as e:
            outcome = SampleOutcome(
                sample_run_id,
                e.failure_class,
                "sample_failed",
                str(e))

        except Exception as e:
            wrapped = VeritasRunnerError.wrap(
                e, context=f"processing sample {sample_run_id}",
                attempt_id=self.attempt_id, sample_run_id=sample_run_id,
            )
            outcome = SampleOutcome(
                sample.sample_run_id,
                wrapped.failure_class,
                "sample_failed",
                str(wrapped))

        outcome.duration_ms = int((time.monotonic() - started) * 1000)

        disk_used_mb = self._get_dir_size_mb(output_dir.parent)

        payload: dict[str, Any] = {
            "duration_ms": outcome.duration_ms,
            "resource_usage": {
                "materialize_duration_ms": materialize_duration_ms,
                "veritas_duration_ms": veritas_duration_ms,
                "peak_memory_mb": peak_memory_mb,
                "disk_used_mb": disk_used_mb,
            },
        }

        if not outcome.success:
            logger.error(
                "Sample %s execution failed with internal status '%s': %s",
                sample_run_id,
                outcome.status.value,
                outcome.message,
            )
            payload["failure_class"] = outcome.status.spec_failure_class
            payload["detail"] = outcome.message[:2000]

        self.reporter.emit(
            event_type = outcome.terminal_state, 
            sample_run_id=sample_run_id,
            payload=payload,
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
        fail = fail.bind(attempt_id=self.attempt_id)

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

            for sample in sorted(samples, key=lambda s: s.sample_order):
                if time.monotonic() > self.deadline:
                    stopped_early = fail(
                        StatusClass.DEADLINE_EXCEEDED,
                        (
                            f"Operational deadline reached after {len(outcomes)}/"
                            f"{len(samples)} samples; remaining samples left pending."
                        )
                    )
                    logger.warning(str(stopped_early))
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
                    )
                    logger.warning(str(stopped_early))
                    break

                outcome = self._process_sample(
                    sample,
                    per_sample_timeout_s=max(1, int(self.deadline - time.monotonic())),
                )

                outcomes.append(outcome)

                if outcome.status in (StatusClass.CONFIG_ERROR, StatusClass.AUTH_REJECTED):
                    stopped_early = fail(
                        failure_class=outcome.status,
                        message=f"Aborting attempt: {outcome.message}",
                    )
                    logger.warning(str(stopped_early))
                    break

            completed_outcomes = [o for o in outcomes if o.is_completed]
            total_samples = len(manifest.samples)
            num_completed = len(completed_outcomes)

            if num_completed == total_samples:
                event_type = "attempt_completed"
            elif num_completed == 0:
                event_type = "attempt_failed"
            else:
                event_type = "attempt_partial"
            
            duration_ms = int((time.monotonic() - started) * 1000)
            veritas_ver = _veritas_version()

            payload: dict[str, Any] = {
                "duration_ms": duration_ms,
                "veritas_version": veritas_ver,
                "samples_total": total_samples,
                "samples_completed": num_completed,
                "samples_failed_or_pending": total_samples - num_completed,
            }

            self.reporter.emit(
                event_type,
                payload=payload
            )

            return AttemptResult(
                attempt_id=self.attempt_id,
                terminal_state=event_type,
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

        # TODO: error mapping below is invalid. Build custom Click exception classes in Veritas.
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
            StatusClass.VERITAS_CRASHED,
            f"Veritas process crashed unexpectedly ({detail}).",
        ) from e

    metrics_file = output_dir / "metrics.tsv" #TODO restrict output_dir type hint: only path
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


_UNDEFINED_METRIC_SENTINEL = "NA"  # TODO: PROVISIONAL: Relies on Veritas zero-denominator fix


def _scan_metrics_for_undefined(metrics_file: Path) -> list[dict[str, str]]:
    """
    Scan a sample's metrics report for undefined precision or recall calculations.

    Inspects a TSV metrics report for cells matching the undefined metric sentinel,
    indicating a zero-denominator condition (e.g., zero truth variants or zero query calls).

    Parameters
    ----------
    metrics_file : Path
        Path to the sample's ``metrics.tsv`` file produced by Veritas.

    Returns
    -------
    list of dict of str to str
        A list of dictionaries identifying each undefined cell found in the global summary.
        Each dictionary contains:

        * ``"variant_type"`` : str
            The variant classification (e.g., ``"SNV"``, ``"INDEL"``).
        * ``"metric"`` : str
            The undefined metric name (``"Precision"`` or ``"Recall"``).

    Notes
    -----
    * **Upstream Dependency**: Current versions of Veritas write ``"0.0"`` for zero
      denominators due to ``safe_div``. This function searches for
      ``_UNDEFINED_METRIC_SENTINEL`` (``"NA"``), which Veritas will write once
      the upstream zero-denominator fix lands. Until then, this returns ``[]``.
    * **Regional Scope**: Only inspects rows where ``Region == "ALL"``. Optional BED
      sub-regions (``PRIMER``, ``MASK``, etc.) are sparse by design, so an empty
      sub-region should not flag an entire sample as ``sample_not_evaluable``.
    * **F1-Score Excluded**: F1-Score is derived from Precision and Recall and is
      omitted to avoid redundant error reporting.
    """
    if not metrics_file.is_file():
        return []

    undefined: list[dict[str, str]] = []
    with open(metrics_file, newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            if row.get("Region") != "ALL":
                continue
            for metric in ("Precision", "Recall"):
                if row.get(metric) == _UNDEFINED_METRIC_SENTINEL:
                    undefined.append(
                        {"variant_type": row.get("Category", "?"), "metric": metric}
                    )
    return undefined