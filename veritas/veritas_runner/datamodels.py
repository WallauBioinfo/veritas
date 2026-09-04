from typing import List, Optional, Self, Literal, Dict
from pydantic import BaseModel, Field, model_validator, field_validator
from dataclasses import dataclass, field
from status import StatusClass

class ManifestFile(BaseModel):
    role: str
    url: str
    sha256: str
    size: Optional[int] = None #TODO: check if always known upfront

    @field_validator("sha256")
    @classmethod
    def check_sha256_hex(cls, v: str) -> str:
        if len(v) != 64:
            raise ValueError("sha256 string must be 64 characters long")
        try:
            bytes.fromhex(v)
        except ValueError:
            raise ValueError(f"invalid hex string: {v!r}")
        return v.lower()

    @field_validator("url")
    @classmethod
    def check_https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError(f"url must use HTTPS, got: {v!r}")
        return v


class TruthBundle(BaseModel):
    id: str
    version: str
    truth_genome_sha256: Optional[str] = None  # TODO: ID/Hash for reference only, never downloaded. Here is optional. Check.
    files: List[ManifestFile] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_required_roles_present(self) -> Self: #TODO require Python 3.11+
        found_roles = {f.role for f in self.files}
        required_truth_roles = {"truth_vcf", "truth_tbi", "rtg_sdf"} # TODO: low_cov.bed as requirement?
        missing_roles = required_truth_roles - found_roles
        
        if missing_roles:
            raise ValueError(
                f"Truth bundle '{self.id}' is missing required role(s): {sorted(missing_roles)}"
            )
        return self

    @property
    def files_by_role(self) -> Dict[str, ManifestFile]:
        """Maps all truth files into a dictionary by role"""
        return {f.role: f for f in self.files}


class ExecutorConfig(BaseModel):
    veritas_commit: str
    environment_digest: str
    parser_version: str


class RegionAnnotations(BaseModel):
    primer_bed: Optional[ManifestFile] = None
    mask_bed: Optional[ManifestFile] = None
    low_cov_truth_bed: Optional[ManifestFile] = None
    low_cov_query_bed: Optional[ManifestFile] = None

    @model_validator(mode="after")
    def check_roles_match_fields(self) -> Self:
        expected = {
            "primer_bed": self.primer_bed, #TODO: Veritas uses primerd.bed. Correct Veritas.
            "mask_bed": self.mask_bed,
            "low_cov_truth_bed": self.low_cov_truth_bed,
            "low_cov_query_bed": self.low_cov_query_bed,
        }
        for expected_role, file in expected.items():
            if file is not None and file.role != expected_role:
                raise ValueError(
                    f"{expected_role} field has mismatched role: {file.role!r}"
                )
        return self


class SampleInput(BaseModel):
    sample_run_id: str
    sample_order: int
    query_type: Literal["fasta", "vcf"]
    truth_bundle: TruthBundle
    query_input: ManifestFile
    region_annotations: Optional[RegionAnnotations] = None

    @model_validator(mode="after")
    def check_input_role_and_fasta(self) -> Self:
        expected_role = f"query_{self.query_type}"
        if self.query_input.role != expected_role:
            raise ValueError(
                f"query_type is '{self.query_type}' but query_input.role is "
                f"'{self.query_input.role}' (expected '{expected_role}')"
            )
        if self.query_type == "fasta" and "reference_fasta" not in self.truth_bundle.files_by_role:
            raise ValueError("query_type 'fasta' requires 'reference_fasta' in truth_bundle.files")
        return self

    @property
    def total_size(self) -> dict[str, int | None]:
        t_size = [self.query_input.size, *[f.size for f in self.truth_bundle.files]]
        return {
            "total_files":len(t_size),
            "missing_size_count": t_size.count(None),
            "known_size": sum(s for s in t_size if s is not None)}
    
class Manifest(BaseModel):
    schema_version: str
    attempt_id: str
    execution_id: str
    operational_deadline_seconds: int
    hard_timeout_seconds: int
    executor: ExecutorConfig
    samples: List[SampleInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_at_least_one_sample(self) -> Self:
        if not self.samples:
            raise ValueError("Manifest must contain at least one sample")
        return self

    @model_validator(mode="after")
    def check_samples_are_unique(self) -> Self:
        run_ids: List[str] = [s.sample_run_id for s in self.samples]
        dupes = [r for r in set(run_ids) if run_ids.count(r) > 1]
        if dupes:
            raise ValueError(f"Duplicate sample_run_id(s) in manifest: {sorted(dupes)}")

        orders: List[int] = [s.sample_order for s in self.samples]
        dupe_orders = [o for o in set(orders) if orders.count(o) > 1]
        if dupe_orders:
            raise ValueError(f"Duplicate sample_order value(s) in manifest: {sorted(dupe_orders)}")

        return self
    
    #TODO: def validate_compatibility: check SUPPORTED_MANIFEST_VERSIONS, SUPPORTED_POLICY_VERSIONS, SUPPORTED_TRUTH_PACKAGE_VERSIONS

    
class CallbackEnvelope(BaseModel):
    schema_version: str
    event_id: str
    attempt_id: str
    workflow_run_id: int
    event_type: Literal[
        "attempt_started", "attempt_completed", "attempt_partial", "attempt_failed",
        "sample_started", "sample_completed", "sample_completed_with_warnings",
        "sample_not_evaluable", "sample_failed",
    ]
    occurred_at: str
    sample_run_id: Optional[str] = None
    payload: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_sample_run_id(self) -> Self:
        if self.event_type.startswith("sample_") and not self.sample_run_id:
            raise ValueError(f"sample_run_id is required for event_type '{self.event_type}'")
        if self.event_type.startswith("attempt_") and self.sample_run_id is not None:
            raise ValueError(f"sample_run_id must be None for event_type '{self.event_type}'")
        return self

class CallbackPayload(BaseModel):
    """Inner payload for reporting failure."""
    failure_class: str
    detail: str
    duration_seconds: Optional[float] = None

SampleState = Literal[
    "sample_completed",
    "sample_completed_with_warnings",
    "sample_not_evaluable",
    "sample_failed",
]

@dataclass
class SampleOutcome:
    sample_run_id: str
    status: StatusClass
    terminal_state: SampleState = "sample_completed"
    message: str = ""
    duration_ms: int = 0

    @property
    def success(self) -> bool:
        return self.status is StatusClass.SUCCESS

    @property
    def is_completed(self) -> bool:
        """Returns True if the sample produced valid/inspectable results."""
        return self.terminal_state in (
            "sample_completed",
            "sample_completed_with_warnings",
            "sample_not_evaluable",
        )


@dataclass
class AttemptResult:
    attempt_id: str
    terminal_state: Literal["attempt_completed", "attempt_partial", "attempt_failed"]
    duration_ms: int
    veritas_version: str
    samples: List[SampleOutcome] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 0 if not "attempt_failed" else 1 # TODO: Check correctedness