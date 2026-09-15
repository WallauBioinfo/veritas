"""
tests/conftest.py

Shared fixtures for the local integration suite.

The suite exercises the real code path end to end:
    python-level run_attempt()
        -> real requests.Session over TLS
        -> mock PathoEQA (manifest + callbacks)
        -> mock artefact storage (real streaming + real SHA-256)
        -> a fake `veritas` executable on PATH

Only two things are faked, and both are external systems we do not own:
PathoEQA and the Veritas scientific binary. Everything in veritas_runner runs
for real.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile
from pathlib import Path
from typing import Optional

import pytest

from tests.veritas_integration.mock_pathoeqa import MockPathoEQA, make_self_signed_cert

# ------------------------------------------------------------------ artefacts

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_sdf_tarball() -> bytes:
    """A minimal, well-formed RTG SDF: one directory holding a couple of files."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in (
            ("sdf/mainIndex", b"RTG-SDF-mock\n"),
            ("sdf/seqdata0", b"ACGT" * 64),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


ARTEFACT_BYTES = {
    "reference.fa": b">chrM\n" + b"ACGTACGTAC\n" * 50,
    "truth.vcf.gz": b"\x1f\x8b\x08\x00mock-truth-vcf-payload",
    "truth.vcf.gz.tbi": b"mock-tabix-index",
    "query.vcf.gz": b"\x1f\x8b\x08\x00mock-query-vcf-payload",
    "rtg_sdf.tar.gz": make_sdf_tarball(),
}


# ------------------------------------------------------------------- fixtures


@pytest.fixture(scope="session")
def tls_cert(tmp_path_factory) -> tuple[Path, Path]:
    return make_self_signed_cert(tmp_path_factory.mktemp("tls"))


@pytest.fixture
def server(tls_cert, monkeypatch):
    """A running mock PathoEQA that `requests` trusts, preloaded with artefacts."""
    cert, key = tls_cert
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(cert))
    with MockPathoEQA(cert, key) as srv:
        for name, data in ARTEFACT_BYTES.items():
            srv.add_artefact(name, data)
        yield srv


@pytest.fixture
def fake_veritas(tmp_path, monkeypatch):
    """
    Put a scriptable `veritas` on PATH.

    Behaviour is driven by VERITAS_FAKE_MODE:
        ok          - writes metrics.tsv, exits 0            (default)
        no_metrics  - exits 0 but writes nothing             -> METRICS_MISSING
        crash       - exits 3                                -> VERITAS_CRASHED
        slow        - sleeps ~0.8s, then writes metrics.tsv    (budget tests)
        hang        - sleeps far past any timeout            -> processing_timeout
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "veritas"
    script.write_text(
        """#!/usr/bin/env bash
set -u
if [ "${1:-}" = "--version" ]; then echo "veritas 0.0.0-test"; exit 0; fi
mode="${VERITAS_FAKE_MODE:-ok}"
out=""
while [ $# -gt 0 ]; do
  case "$1" in --output-dir) out="$2"; shift 2;; *) shift;; esac
done
case "$mode" in
  crash) echo "boom" >&2; exit 3;;
  hang) sleep 600; exit 0;;
  slow) sleep 0.8; mkdir -p "$out"; printf 'metric\\tvalue\\nrecall\\t0.99\\n' > "$out/metrics.tsv"; exit 0;;
  no_metrics) exit 0;;
  *) mkdir -p "$out"; printf 'metric\\tvalue\\nrecall\\t0.99\\n' > "$out/metrics.tsv"; exit 0;;
esac
"""
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("VERITAS_FAKE_MODE", "ok")
    return script


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    """Keep the retry semantics, drop the wall-clock cost."""
    monkeypatch.setenv("VERITAS_RETRY_BASE_DELAY", "0.01")


# ------------------------------------------------------------- manifest builder


@pytest.fixture
def manifest_factory(server):
    """
    Build a manifest whose URLs point at the mock artefact storage and whose
    checksums are the real SHA-256 of the bytes that will be served.
    """

    def _file(role: str, name: str, *, sha: Optional[str] = None, size: Optional[int] = None):
        data = ARTEFACT_BYTES[name]
        return {
            "role": role,
            "url": server.artefact_url(name),
            "sha256": sha or sha256(data),
            "size": len(data) if size is None else size,
        }

    def _sample(sample_run_id: str, order: int, **overrides):
        sample = {
            "sample_run_id": sample_run_id,
            "sample_order": order,
            "query_type": "vcf",
            "truth_bundle": {
                "id": "tb-001",
                "version": "1.0.0",
                "files": [
                    _file("reference_fasta", "reference.fa"),
                    _file("truth_vcf", "truth.vcf.gz"),
                    _file("truth_tbi", "truth.vcf.gz.tbi"),
                    _file("rtg_sdf", "rtg_sdf.tar.gz"),
                ],
            },
            "query_input": _file("query_vcf", "query.vcf.gz"),
        }
        sample.update(overrides)
        return sample

    def build(
        attempt_id: str,
        *,
        samples: int = 2,
        operational_deadline_seconds: int = 4500,
        **overrides,
    ) -> dict:
        manifest = {
            "schema_version": "1.0",
            "attempt_id": attempt_id,
            "execution_id": "exec-0001",
            "operational_deadline_seconds": operational_deadline_seconds,
            "hard_timeout_seconds": 5400,
            "executor": {
                "veritas_commit": "a" * 40,
                "environment_digest": "sha256:" + "b" * 64,
                "parser_version": "1.0",
            },
            "samples": [_sample(f"sr-{i + 1:03d}", i + 1) for i in range(samples)],
        }
        manifest.update(overrides)
        return manifest

    build.file = _file  # type: ignore[attr-defined]
    build.sample = _sample  # type: ignore[attr-defined]
    return build
