"""
tests/conftest.py

Shared fixtures for the local integration suite (contract: manifest schema 2.0).

The suite exercises the real code path end to end:
    python-level run_attempt()
        -> real requests.Session over TLS
        -> mock PathoEQA (manifest, callbacks, per-sample result delivery)
        -> mock artefact storage (real streaming + real SHA-256)
        -> fake `veritas`, `rtg` and `bcftools` executables on PATH

Only external systems we do not own are faked: PathoEQA and the scientific /
genomics binaries. Everything in veritas_runner runs for real. The result
delivery is validated with PathoEQA's own metrics parser (tests/contract).
"""

from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

from tests.veritas_integration_local.mock_pathoeqa import MockPathoEQA, make_self_signed_cert

EXECUTOR_SHA = "a" * 40          # GITHUB_SHA == manifest executor.veritas_commit
TRUTH_BUNDLE_ID = "b" * 64       # hash of the truth manifest, as in the real manifest

# PathoEQA's metrics TSV: header + SNV/INDEL x ALL/PRIMER BED, ratios always present.
GOOD_TSV = (
    "Category\tRegion\tTP\tFP\tFN\tPrecision\tRecall\tF1-Score\n"
    "SNV\tALL\t1\t1\t1\t0.5\t0.5\t0.5\n"
    "INDEL\tALL\t1\t0\t0\t1.0\t1.0\t1.0\n"
    "SNV\tPRIMER BED\t1\t0\t1\t1.0\t0.5\t0.6666666666666666\n"
    "INDEL\tPRIMER BED\t0\t0\t0\t0.0\t0.0\t0.0\n"
)
# What Veritas wrote before the fix: PRIMER BED rows carry counts but no ratios.
LEGACY_TSV = (
    "Category\tRegion\tTP\tFP\tFN\tPrecision\tRecall\tF1-Score\n"
    "SNV\tALL\t1\t1\t1\t0.5\t0.5\t0.5\n"
    "INDEL\tALL\t1\t0\t0\t1.0\t1.0\t1.0\n"
    "SNV\tPRIMER BED\t1\t0\t1\t\t\t\n"
    "INDEL\tPRIMER BED\t0\t0\t0\t\t\t\n"
)

# ------------------------------------------------------------------ artefacts

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_VCF = (
    "##fileformat=VCFv4.2\n##contig=<ID=chrM,length=500>\n"
    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1\n"
    "chrM\t300\t.\tA\tG\t.\tPASS\t.\tGT\t1/1\n"
    "chrM\t100\t.\tC\tT\t.\tPASS\t.\tGT\t1/1\n"
)

# The four originals PathoEQA ships. TBI and SDF are NOT in the manifest any more.
ARTEFACT_BYTES = {
    "reference.fa": b">chrM\n" + b"ACGTACGTAC\n" * 50,
    "truth.vcf.gz": gzip.compress(_VCF.encode(), mtime=0),
    "query.vcf.gz": gzip.compress(_VCF.encode(), mtime=0),
    "primers.bed": b"chrM\t0\t100\n",
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


def _script(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def fake_veritas(tmp_path, monkeypatch):
    """
    Put scriptable `veritas`, `rtg` and `bcftools` on PATH.

    `veritas` is driven by VERITAS_FAKE_MODE:
        ok          - writes a contract-valid metrics.tsv, exits 0   (default)
        legacy_tsv  - writes the old TSV (empty PRIMER BED ratios)   -> PathoEQA 422
        no_metrics  - exits 0 but writes nothing                     -> METRICS_MISSING
        crash       - exits 3                                        -> VERITAS_CRASHED
        slow        - sleeps ~0.8s, then writes a valid metrics.tsv  (budget tests)
        hang        - sleeps far past any timeout                    -> processing_timeout

    `rtg format -o DIR FASTA` creates DIR/mainIndex (and refuses an existing DIR, like rtg).
    `bcftools sort|index` copy a gzip input and touch the .tbi; non-gzip input fails.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "good.tsv").write_text(GOOD_TSV)
    (bindir / "legacy.tsv").write_text(LEGACY_TSV)

    _script(bindir / "veritas", """#!/usr/bin/env bash
set -u
here="$(dirname "$0")"
if [ "${1:-}" = "--version" ]; then echo "veritas 0.0.0-test"; exit 0; fi
mode="${VERITAS_FAKE_MODE:-ok}"
out=""
while [ $# -gt 0 ]; do
  case "$1" in --output-dir) out="$2"; shift 2;; *) shift;; esac
done
case "$mode" in
  crash) echo "boom" >&2; exit 3;;
  hang) sleep 600; exit 0;;
  slow) sleep 0.8; mkdir -p "$out"; cp "$here/good.tsv" "$out/metrics.tsv"; exit 0;;
  no_metrics) exit 0;;
  legacy_tsv) mkdir -p "$out"; cp "$here/legacy.tsv" "$out/metrics.tsv"; exit 0;;
  *) mkdir -p "$out"; cp "$here/good.tsv" "$out/metrics.tsv"; exit 0;;
esac
""")
    _script(bindir / "rtg", """#!/usr/bin/env bash
[ "${1:-}" = "format" ] || exit 2
out="$3"
[ -e "$out" ] && { echo "Error: directory exists" >&2; exit 1; }
mkdir -p "$out" && echo mock > "$out/mainIndex"
""")
    _script(bindir / "bcftools", """#!/usr/bin/env bash
cmd="${1:-}"; shift
case "$cmd" in
  sort)
    out=""; in=""
    while [ $# -gt 0 ]; do
      case "$1" in -o) out="$2"; shift 2;; -O*) shift;; *) in="$1"; shift;; esac
    done
    head -c2 "$in" | od -An -tx1 | grep -q '1f 8b' || { echo "[E::hts_open] not a gzip VCF" >&2; exit 1; }
    cp "$in" "$out";;
  index) for last; do :; done; : > "$last.tbi";;
  *) exit 2;;
esac
""")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("VERITAS_FAKE_MODE", "ok")
    return bindir / "veritas"


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    """Keep the retry semantics, drop the wall-clock cost; pin the workflow SHA."""
    monkeypatch.setenv("VERITAS_RETRY_BASE_DELAY", "0.01")
    monkeypatch.setenv("GITHUB_SHA", EXECUTOR_SHA)
    # The runner refuses to start below a 50 GiB free-space estimate; don't let the
    # developer's disk decide whether the suite passes.
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: SimpleNamespace(total=500 * 2**30, used=0, free=500 * 2**30)
    )


# ------------------------------------------------------------- manifest builder


@pytest.fixture
def manifest_factory(server):
    """
    Build a schema-2.0 manifest whose URLs point at the mock artefact storage and
    whose checksums are the real SHA-256 of the bytes that will be served.
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
                "id": TRUTH_BUNDLE_ID,
                "version": "1",
                "files": [
                    _file("truth_vcf", "truth.vcf.gz"),
                    _file("reference_fasta", "reference.fa"),
                ],
            },
            "query_input": _file("query_vcf", "query.vcf.gz"),
            "region_annotations": {"primer_bed": _file("primer_bed", "primers.bed")},
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
            "schema_version": "2.0",
            "attempt_id": attempt_id,
            "execution_id": "exec-0001",
            "operational_deadline_seconds": operational_deadline_seconds,
            "hard_timeout_seconds": 5400,
            "executor": {
                "veritas_commit": EXECUTOR_SHA,
                "environment_digest": "sha256:" + "b" * 64,
                "parser_version": "pathoeqa-tsv-v1-decimal28",
            },
            "samples": [_sample(f"sr-{i + 1:03d}", i + 1) for i in range(samples)],
        }
        manifest.update(overrides)
        return manifest

    build.file = _file  # type: ignore[attr-defined]
    build.sample = _sample  # type: ignore[attr-defined]
    return build
