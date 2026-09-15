"""
tests/e2e/github_actions.py

Thin client for dispatching veritas-executor.yml on real GitHub and reading
back its result. No mocking - this talks to the actual GitHub REST API.

Auth: a PAT with `actions:write`/`actions:read` on the target repo, via
GITHUB_TOKEN. Never OIDC - that token is minted inside the running job and
is never available to code calling the dispatch API from outside Actions.
"""
from __future__ import annotations

import io
import re
import time
import zipfile
from dataclasses import dataclass

import requests

WORKFLOW_FILE = "veritas-executor.yml"
API = "https://api.github.com"


@dataclass
class RunOutcome:
    run_id: int
    html_url: str
    conclusion: str
    result_json: str | None
    oidc_probe_json: str | None
    error_annotations: list[str]


class GitHubActionsClient:
    def __init__(self, repo: str, token: str):
        self.repo = repo
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _get_existing_run_ids(self) -> set[int]:
        """Snapshot all currently existing workflow run IDs prior to dispatching."""
        resp = requests.get(
            f"{API}/repos/{self.repo}/actions/workflows/{WORKFLOW_FILE}/runs",
            params={"per_page": 20},
            headers=self.headers,
            timeout=15,
        )
        if resp.status_code == 200:
            return {run["id"] for run in resp.json().get("workflow_runs", [])}
        return set()

    def dispatch(self, ref: str, attempt_id: str, dry_run: bool) -> None:
        """Trigger the workflow_dispatch event."""
        resp = requests.post(
            f"{API}/repos/{self.repo}/actions/workflows/{WORKFLOW_FILE}/dispatches",
            json={"ref": ref, "inputs": {"attempt_id": attempt_id, "dry_run": dry_run}},
            headers=self.headers,
            timeout=15,
        )
        resp.raise_for_status()

    def find_run(self, ref: str, existing_ids: set[int], timeout_s: int = 60) -> dict:
        """
        Poll the workflow runs list and return the first run ID that was not
        present before dispatching. Eliminates race conditions with past runs.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            resp = requests.get(
                f"{API}/repos/{self.repo}/actions/workflows/{WORKFLOW_FILE}/runs",
                params={"branch": ref, "event": "workflow_dispatch", "per_page": 10},
                headers=self.headers,
                timeout=15,
            )
            resp.raise_for_status()
            for run in resp.json().get("workflow_runs", []):
                if run["id"] not in existing_ids:
                    return run
            time.sleep(2)
        raise TimeoutError(f"No new run appeared on '{ref}' within {timeout_s}s of dispatch.")

    def wait_for_completion(self, run_id: int, timeout_s: int = 900) -> dict:
        """Poll a specific run until its status becomes 'completed'."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            resp = requests.get(f"{API}/repos/{self.repo}/actions/runs/{run_id}", headers=self.headers, timeout=15)
            resp.raise_for_status()
            run = resp.json()
            if run["status"] == "completed":
                return run
            time.sleep(5)
        raise TimeoutError(f"Run {run_id} did not complete within {timeout_s}s.")

    def fetch_artefact_files(self, run_id: int, attempt_id: str) -> dict[str, str]:
        """Return {basename: text} for every JSON file in the attempt artefact."""
        resp = requests.get(
            f"{API}/repos/{self.repo}/actions/runs/{run_id}/artifacts", headers=self.headers, timeout=15
        )
        resp.raise_for_status()
        artefact_name = f"veritas-attempt-{attempt_id}"
        match = next((a for a in resp.json().get("artifacts", []) if a["name"] == artefact_name), None)
        if match is None:
            return {}
        zip_resp = requests.get(match["archive_download_url"], headers=self.headers, timeout=30)
        zip_resp.raise_for_status()
        files: dict[str, str] = {}
        with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as zf:
            for name in zf.namelist():
                if name.endswith(".json"):
                    files[name.rsplit("/", 1)[-1]] = zf.read(name).decode("utf-8")
        return files

    def fetch_error_annotations(self, run_id: int) -> list[str]:
        """Fetch error annotations from the workflow run logs."""
        jobs_resp = requests.get(f"{API}/repos/{self.repo}/actions/runs/{run_id}/jobs", headers=self.headers, timeout=15)
        jobs_resp.raise_for_status()
        errors: list[str] = []
        for job in jobs_resp.json().get("jobs", []):
            logs_resp = requests.get(
                f"{API}/repos/{self.repo}/actions/jobs/{job['id']}/logs", headers=self.headers, timeout=15
            )
            if logs_resp.status_code == 200:
                errors.extend(re.findall(r"::error[^\n]*", logs_resp.text))
        return errors

    def run(
        self,
        ref: str,
        attempt_id: str,
        dry_run: bool,
        dispatch_timeout: int = 60,
        run_timeout: int = 900,
    ) -> RunOutcome:
        """Snapshot run IDs, dispatch, wait for the new run, and return results."""
        existing_ids = self._get_existing_run_ids()
        self.dispatch(ref, attempt_id, dry_run)
        run = self.find_run(ref, existing_ids, timeout_s=dispatch_timeout)
        run = self.wait_for_completion(run["id"], timeout_s=run_timeout)
        files = self.fetch_artefact_files(run["id"], attempt_id)
        annotations = self.fetch_error_annotations(run["id"])

        return RunOutcome(
            run_id=run["id"],
            html_url=run["html_url"],
            conclusion=run["conclusion"],
            result_json=files.get("result.json") or None,
            oidc_probe_json=files.get("oidc_probe.json") or None,
            error_annotations=annotations,
        )