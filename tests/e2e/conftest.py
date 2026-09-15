"""
tests/e2e/conftest.py

These tests dispatch the real veritas-executor.yml on real GitHub. PathoEQA
doesn't exist yet, so no fixture here depends on a resolvable attempt - see
test_workflow_dispatch.py for what's actually being proven in the meantime.

Needs:

    GITHUB_TOKEN      PAT with actions:write/actions:read on the repo
    VERITAS_E2E_REPO  "owner/repo"
    VERITAS_E2E_REF   branch to run against (the workflow file itself must
                      already be merged to the default branch - see the
                      workflow_dispatch registration rule - but this ref is
                      what actually gets checked out)

Not set -> the whole module is skipped, not failed. This suite is not part
of the fast local loop; run it deliberately, e.g. a separate CI job or by
hand before a release, since a single case can take several minutes.
"""
from __future__ import annotations

import os

import pytest

from .github_actions import GitHubActionsClient

REQUIRED = ("GITHUB_TOKEN", "VERITAS_E2E_REPO", "VERITAS_E2E_REF")


@pytest.fixture(autouse=True, scope="session")
def _require_e2e_env():
    missing = [v for v in REQUIRED if not os.environ.get(v)]
    if missing:
        pytest.skip(f"e2e suite requires {', '.join(missing)} to be set")


@pytest.fixture(scope="session")
def repo() -> str:
    return os.environ["VERITAS_E2E_REPO"]


@pytest.fixture(scope="session")
def ref() -> str:
    return os.environ["VERITAS_E2E_REF"]


@pytest.fixture(scope="session")
def gh(repo) -> GitHubActionsClient:
    return GitHubActionsClient(repo=repo, token=os.environ["GITHUB_TOKEN"])