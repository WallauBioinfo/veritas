"""
tests/e2e/test_workflow_dispatch.py

Level 2: does the actual GitHub Actions trigger work end to end - dispatch,
OIDC, PATHOEQA_API_URL validation, artefact upload? Level 1
(tests/veritas_integration/) already covers run_attempt()'s own logic; this
suite exists to catch the things only GitHub itself can break: registration
of the workflow_dispatch trigger, permissions, the OIDC handshake, and the
concurrency group.

PathoEQA does not exist yet. That does NOT block these tests: every step up
to and including a real manifest fetch attempt runs for real regardless of
what's on the other end of PATHOEQA_API_URL - including the one thing Level
1 structurally cannot exercise, the real GitHub OIDC mint (Level 1 always
supplies --oidc-token directly and never touches ACTIONS_ID_TOKEN_REQUEST_URL).
Until PathoEQA exists, a well-formed request is *expected* to fail at the
manifest fetch with ATTEMPT_NOT_FOUND (if host returns HTTP 404) or 
UPSTREAM_UNAVAILABLE (if host is unreachable), and `if: always()` on the 
upload step means that failure still produces a real, downloadable result.json.

Prerequisite this suite cannot set for you: PATHOEQA_API_URL is a repo
*variable* (${{ vars.PATHOEQA_API_URL }}), not a workflow_dispatch input, so
it can't be overridden per run here. It must already be set to some
https://... placeholder in Settings > Secrets and variables > Actions >
Variables, or every case below fails with CONFIG_ERROR instead - a real but
uninteresting result we're not trying to prove right now.

Once PathoEQA exists, swap test_dry_run_fails_at_manifest_fetch
for happy-path (attempt_completed) and explicit failure cases.

Run deliberately, not on every push - see conftest.py for required env vars.
"""
from __future__ import annotations

import json
import uuid

import pytest

pytestmark = pytest.mark.e2e


def test_malformed_attempt_id_fails_before_run_attempt(gh, ref):
    """Doesn't touch PathoEQA at all - fails in the 'Validate attempt_id'
    bash step, before Python even starts."""
    outcome = gh.run(ref=ref, attempt_id="not-a-uuid", dry_run=True)

    assert outcome.conclusion == "failure"
    assert outcome.result_json is None, "should fail before 'Run attempt', so no artefact should exist"
    assert any("INPUT_MALFORMED" in line for line in outcome.error_annotations), outcome.error_annotations


def test_dry_run_fails_at_manifest_fetch(gh, ref):
    """
    Proves the whole real chain up to and including the manifest fetch:
    dispatch -> run registers -> OIDC token minted by GitHub -> tool
    install -> validate_prerequisites passes (https scheme is satisfied,
    reachability is not checked there) -> fetch_manifest() receives a 404 or
    connection failure against PathoEQA -> __main__.py classifies it as
    attempt_not_found or upstream_unavailable and still emits a correct
    result.json via `if: always()`.

    A well-formed, syntactically valid attempt_id is used here on purpose;
    since nothing can currently find a real manifest, any real UUID exercises
    the same path right now.
    """
    attempt_id = str(uuid.uuid4())
    outcome = gh.run(ref=ref, attempt_id=attempt_id, dry_run=True)

    assert outcome.conclusion == "failure", (
        f"run {outcome.html_url} should fail while PathoEQA doesn't exist; "
        f"annotations: {outcome.error_annotations}"
    )
    assert outcome.result_json is not None, (
        "expected a veritas-attempt-* artefact even on failure (if: always()); "
        f"annotations: {outcome.error_annotations}"
    )

    payload = json.loads(outcome.result_json)
    assert payload["attempt_id"] == attempt_id
    assert payload["terminal_state"] == "attempt_failed"
    assert payload["failure_class"] in ("attempt_not_found", "upstream_unavailable"), (
        f"expected attempt_not_found or upstream_unavailable, got {payload['failure_class']}. "
        "A different failure_class means something upstream of the manifest fetch changed "
        "(e.g. PATHOEQA_API_URL misconfigured -> config_error, or attempt_id validation regressed)"
    )
    assert payload["dry_run"] is True