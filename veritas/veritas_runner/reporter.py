import logging
import uuid
from datetime import datetime, timezone
from typing import Optional
from pydantic import ValidationError

from .datamodels import CallbackEnvelope, ResultMetadata
from .exceptions import ErrorFactory, VeritasRunnerError
from .pathoeqa import PathoEQAClient
from .retry import CONTROL_PLANE
from .status import StatusClass

logger = logging.getLogger(__name__)


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
    schema_version : str or None, optional
        Callback schema version override. Defaults to "1.0".
    """

    CALLBACK_SCHEMA_VERSION = "1.0"

    def __init__(
        self,
        client: PathoEQAClient,
        attempt_id: str,
        workflow_run_id: int,
        schema_version: Optional[str] = None,
    ):
        self.client = client
        self.attempt_id = attempt_id
        self.workflow_run_id = workflow_run_id
        self.schema_version = (
            schema_version if schema_version is not None else self.CALLBACK_SCHEMA_VERSION
        )
        self._error = ErrorFactory(attempt_id=attempt_id)

    def emit(
        self,
        event_type: str,
        *,
        sample_run_id: Optional[str] = None,
        payload: Optional[dict] = None,
        deadline: Optional[float] = None,
        best_effort: bool = False,
    ) -> Optional[str]:
        """
        Send a structured callback envelope to PathoEQA.

        Parameters
        ----------
        event_type : str
            Lifecycle event name (e.g., 'sample_completed').
        sample_run_id : str, optional
            Target sample run ID, if applicable.
        payload : dict, optional
            Event payload data dictionary.
        deadline : float, optional
            Monotonic time threshold for retries.
        best_effort : bool, default=False
            If True, drop delivery failures as warnings instead of raising.

        Returns
        -------
        str or None
            The `event_id` of the delivered envelope, or ``None`` if delivery
            failed and `best_effort` is True.

        Raises
        ------
        VeritasRunnerError
            If delivery fails and `best_effort` is False.
        """
        envelope = CallbackEnvelope(
            schema_version=self.schema_version,
            event_id=str(uuid.uuid4()),
            attempt_id=self.attempt_id,
            workflow_run_id=self.workflow_run_id,
            event_type=event_type,
            occurred_at=datetime.now(timezone.utc).isoformat(),
            sample_run_id=sample_run_id,
            payload=payload or {},
        )
        try:
            CONTROL_PLANE.run(
                lambda: self.client.send_callback(envelope),
                description=f"callback {event_type}",
                deadline=deadline,
            )
        except VeritasRunnerError:
            if not best_effort:
                raise
            logger.warning("Dropped advisory callback %s", event_type)
            return None
        return envelope.event_id

    def deliver_result(
        self,
        sample_run_id: str,
        metrics: bytes,
        *,
        event_id: str,
        input_type: str,
        input_sha256: str,
        truth_manifest_hash: str,
        executor_sha: str,
        deadline: Optional[float] = None,
        best_effort: bool = False,
    ) -> None:
        """
        Build and deliver one sample's metrics TSV to PathoEQA, retrying transient failures.

        Parameters
        ----------
        sample_run_id : str
            Sample run the metrics belong to.
        metrics : bytes
            Exact bytes of the sample's ``metrics.tsv``.
        event_id : str
            `event_id` of the sample's accepted terminal callback.
        input_type : str
            ``"fasta"`` or ``"vcf"``.
        input_sha256 : str
            SHA-256 of the original query input, as listed in the manifest.
        truth_manifest_hash : str
            Hash identifying the truth manifest of this evaluation.
        executor_sha : str
            Commit SHA of the running workflow.
        deadline : float, optional
            Monotonic time threshold for retries.
        best_effort : bool, default=False
            If True, log delivery failures instead of raising.

        Raises
        ------
        VeritasRunnerError
            If the metadata is invalid or delivery fails, and `best_effort` is False.
        """
        fail = self._error.bind(sample_run_id=sample_run_id)
        try:
            try:
                metadata = ResultMetadata.from_bytes(
                    metrics,
                    event_id=event_id,
                    input_type=input_type,
                    input_sha256=input_sha256,
                    truth_manifest_hash=truth_manifest_hash,
                    executor_sha=executor_sha,
                )
            except ValidationError as e:
                raise fail.from_validation_error(
                    e, StatusClass.CALLBACK_INVALID, context="Result metadata"
                ) from e

            CONTROL_PLANE.run(
                lambda: self.client.send_result(sample_run_id, metrics, metadata),
                description=f"result delivery {sample_run_id}",
                deadline=deadline,
            )
        except VeritasRunnerError as e:
            if not best_effort:
                raise
            logger.error("Failed result delivery for sample %s: %s", sample_run_id, e)

            