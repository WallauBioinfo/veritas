import uuid
import logging
import os
from typing import Optional
from datetime import datetime, timezone

from veritas_runner.pathoeqa import PathoEQAClient
from veritas_runner.datamodels import CallbackEnvelope
from veritas_runner.exceptions import VeritasRunnerError
from veritas_runner.retry import with_auto_retry

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

    """

    CALLBACK_SCHEMA_VERSION = str(os.environ.get("VERITAS_CALLBACK_SCHEMA_VERSION", "1.0"))
    
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
            with_auto_retry(
                lambda: self.client.send_callback(envelope),
                description=f"callback {event_type}",
                deadline=deadline,
            )
        except VeritasRunnerError:
            if not best_effort:
               raise
            logger.warning("Dropped advisory callback %s", event_type)
