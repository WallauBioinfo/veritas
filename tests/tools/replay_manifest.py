"""Replay a real PathoEQA manifest against the local MockPathoEQA.

Runs the real runner (and whatever `veritas`, `rtg` and `bcftools` are on PATH) on
the inputs listed in a manifest, with the mock standing in for PathoEQA: manifest,
file downloads, callbacks and per-sample result delivery. The metrics.tsv Veritas
produces is judged by PathoEQA's own TSV parser, so you see what PathoEQA would say.

A manifest holds only URLs and SHA-256 hashes, so you must supply the input files.
Any file in --files-dir (any name, any subfolder) is matched to the manifest by hash.

Usage (from the repo root):
    python -m tests.tools.replay_manifest manifest.json --files-dir ./staging_files
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.contract.metrics_contract import evaluate
from tests.veritas_integration_local.mock_pathoeqa import MockPathoEQA, make_self_signed_cert
from veritas.veritas_runner.exceptions import ErrorFactory, VeritasRunnerError
from veritas.veritas_runner.runner import ExecutionAttempt


def _file_entries(manifest: dict):
    for sample in manifest["samples"]:
        yield sample["query_input"]
        yield from sample["truth_bundle"]["files"]
        yield from (f for f in (sample.get("region_annotations") or {}).values() if f)


def _index_files(files_dir: Path) -> dict[str, Path]:
    index: dict[str, Path] = {}
    for path in sorted(files_dir.rglob("*")):
        if path.is_file():
            index.setdefault(hashlib.sha256(path.read_bytes()).hexdigest(), path)
    return index


def _report(server: MockPathoEQA, manifest: dict, workdir: Path) -> bool:
    print("\nCallbacks received by the mock PathoEQA:")
    for call in server.callbacks:
        env = call["envelope"]
        print(f"  {env['event_type']:<32} sample={env.get('sample_run_id') or '-':<38} event_id={env['event_id']}")

    all_delivered = True
    for sample in manifest["samples"]:
        sid = sample["sample_run_id"]
        print(f"\n=== sample {sid} (order {sample['sample_order']}) ===")
        tsv_path = workdir / sid / "output" / "metrics.tsv"
        if not tsv_path.is_file():
            print("  no metrics.tsv was generated")
            all_delivered = False
            continue
        raw = tsv_path.read_bytes()
        print(f"  metrics.tsv ({len(raw)} bytes) - {tsv_path}")
        for line in raw.decode("utf-8", errors="replace").splitlines():
            print("    " + line)
        _, diagnostic = evaluate(raw)
        print(f"  PathoEQA TSV parser: {'ACCEPTED' if diagnostic is None else 'REJECTED - ' + str(diagnostic)}")

        records = [r for r in server.results if r["sample_run_id"] == sid]
        if not records:
            print("  delivery: NOT ATTEMPTED (see the log above; a dropped terminal callback skips it)")
            all_delivered = False
        else:
            last = records[-1]
            print(f"  delivery: HTTP {last['status']} after {len(records)} request(s)"
                  + (f" - {last['rejection']}" if last["rejection"] else ""))
            all_delivered &= last["status"] == 200
    return all_delivered


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("manifest", help="manifest JSON, as served by PathoEQA")
    parser.add_argument("--files-dir", required=True, help="folder holding the real input files")
    parser.add_argument("--workdir", default="./replay_out", help="where the runner writes (default: ./replay_out)")
    parser.add_argument("--skip-disk-check", action="store_true", help="ignore the runner's 50 GiB free-space estimate")
    args = parser.parse_args(argv)

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    index = _index_files(Path(args.files_dir))
    entries = list(_file_entries(manifest))
    missing = [e for e in entries if e["sha256"] not in index]
    if missing:
        print("No file in --files-dir matches these manifest entries (by SHA-256):")
        for entry in missing:
            print(f"  {entry['role']:<16} size={entry.get('size')}  sha256={entry['sha256']}")
        return 2

    workdir = Path(args.workdir).resolve()
    attempt_id = manifest["attempt_id"]
    env = {"GITHUB_SHA": manifest["executor"]["veritas_commit"]}
    patches = [mock.patch.dict(os.environ, env)]
    if args.skip_disk_check:
        patches.append(mock.patch.object(
            shutil, "disk_usage", lambda _p: SimpleNamespace(total=2**40, used=0, free=2**40)))

    with tempfile.TemporaryDirectory() as tmp, MockPathoEQA(*make_self_signed_cert(Path(tmp))) as server:
        patches.append(mock.patch.dict(os.environ, {"REQUESTS_CA_BUNDLE": str(Path(tmp) / "cert.pem")}))
        for entry in entries:
            name = entry["url"].split("?")[0].rsplit("/", 1)[-1]
            server.add_artefact(name, index[entry["sha256"]].read_bytes())
            entry["url"] = server.artefact_url(name)
        server.manifest = manifest

        for p in patches:
            p.start()
        try:
            attempt = ExecutionAttempt(attempt_id=attempt_id, workdir=str(workdir), api_url=server.base_url,
                                       oidc_token="local-replay", workflow_run_id=1)
            result = attempt.run_attempt(ErrorFactory(attempt_id=attempt_id))
            print(f"\nAttempt finished: {result.terminal_state}")
        except VeritasRunnerError as exc:
            print(f"\nRunner aborted: {exc}")
        finally:
            for p in reversed(patches):
                p.stop()
        return 0 if _report(server, manifest, workdir) else 1


if __name__ == "__main__":
    raise SystemExit(main())
