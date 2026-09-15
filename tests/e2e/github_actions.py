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
from datetime import datetime, timezone

import requests

WORKFLOW_FILE = "veritas-executor.yml"
API = "https://api.github.com"


@dataclass
class RunOutcome:
    run_id: int
    html_url: str
    conclusion: str
    result_json: str | None
    error_annotations: list[str]


class GitHubActionsClient:
    def __init__(self, repo: str, token: str):
        self.repo = repo
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def dispatch(self, ref: str, attempt_id: str, dry_run: bool) -> datetime:
        dispatched_at = datetime.now(timezone.utc)
        resp = requests.post(
            f"{API}/repos/{self.repo}/actions/workflows/{WORKFLOW_FILE}/dispatches",
            json={"ref": ref, "inputs": {"attempt_id": attempt_id, "dry_run": dry_run}},
            headers=self.headers,
            timeout=15,
        )
        resp.raise_for_status()
        return dispatched_at

    def find_run(self, ref: str, dispatched_at: datetime, timeout_s: int = 30) -> dict:
        """
        workflow_dispatch returns no run id, so poll the runs list and match
        the newest run created at/after the dispatch call. Inherent short
        race: GitHub needs a moment to register the run.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            resp = requests.get(
                f"{API}/repos/{self.repo}/actions/workflows/{WORKFLOW_FILE}/runs",
                params={"branch": ref, "event": "workflow_dispatch", "per_page": 5},
                headers=self.headers,
                timeout=15,
            )
            resp.raise_for_status()
            for run in resp.json().get("workflow_runs", []):
                created = datetime.strptime(run["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
                if created >= dispatched_at:
                    return run
            time.sleep(2)
        raise TimeoutError(f"No matching run appeared on '{ref}' within {timeout_s}s of dispatch.")

    def wait_for_completion(self, run_id: int, timeout_s: int = 900) -> dict:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            resp = requests.get(f"{API}/repos/{self.repo}/actions/runs/{run_id}", headers=self.headers, timeout=15)
            resp.raise_for_status()
            run = resp.json()
            if run["status"] == "completed":
                return run
            time.sleep(5)
        raise TimeoutError(f"Run {run_id} did not complete within {timeout_s}s.")

    def fetch_result_json(self, run_id: int, attempt_id: str) -> str | None:
        resp = requests.get(f"{API}/repos/{self.repo}/actions/runs/{run_id}/artifacts", headers=self.headers, timeout=15)
        resp.raise_for_status()
        artefact_name = f"veritas-attempt-{attempt_id}"
        match = next((a for a in resp.json().get("artifacts", []) if a["name"] == artefact_name), None)
        if match is None:
            return None
        zip_resp = requests.get(match["archive_download_url"], headers=self.headers, timeout=30)
        zip_resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as zf:
            for name in zf.namelist():
                if name.endswith("result.json"):
                    return zf.read(name).decode("utf-8")
        return None

    def fetch_error_annotations(self, run_id: int) -> list[str]:
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

    def run(self, ref: str, attempt_id: str, dry_run: bool, dispatch_timeout: int = 30, run_timeout: int = 900) -> RunOutcome:
        dispatched_at = self.dispatch(ref, attempt_id, dry_run)
        run = self.find_run(ref, dispatched_at, timeout_s=dispatch_timeout)
        run = self.wait_for_completion(run["id"], timeout_s=run_timeout)
        result_json = self.fetch_result_json(run["id"], attempt_id)
        annotations = [] if result_json is not None else self.fetch_error_annotations(run["id"])
        return RunOutcome(
            run_id=run["id"],
            html_url=run["html_url"],
            conclusion=run["conclusion"],
            result_json=result_json,
            error_annotations=annotations,
        )
