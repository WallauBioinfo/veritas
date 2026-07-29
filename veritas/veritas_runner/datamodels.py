from typing import List, Optional
from typing import Literal
from pydantic import BaseModel, Field, model_validator

class ManifestFile(BaseModel):
    role: str
    url: str
    sha256: str
    size: Optional[int] = None #TODO: check if always known upfront


class TruthPackage(BaseModel):
    id: str
    version: str
    truth_genome_sha256: Optional[str] = None  # ID/Hash for reference only, never downloaded
    files: List[ManifestFile] = Field(default_factory=list)


class ExecutorConfig(BaseModel):
    veritas_commit: str
    environment_digest: str
    parser_version: str


class SampleInput(BaseModel):
    sample_run_id: str
    sample_order: int
    query_type: Literal["fasta", "vcf"]
    truth_package: TruthPackage
    query_input: ManifestFile


class Manifest(BaseModel):
    schema_version: str
    attempt_id: str
    execution_id: str
    operational_deadline_seconds: int
    hard_timeout_seconds: int
    executor: ExecutorConfig
    samples: List[SampleInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_query_input_role_matches_type(self) -> "SampleInput":
        expected_role = f"query_{self.query_type}"
        if self.query_input.role != expected_role:
            raise ValueError(
                f"query_type is '{self.query_type}' but query_input.role is "
                f"'{self.query_input.role}' (expected '{expected_role}')"
            )
        return self


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