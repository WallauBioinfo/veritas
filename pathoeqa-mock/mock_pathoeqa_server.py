"""
pathoeqa-mock/mock_pathoeqa_server.py

Demo stand-in for PathoEQA. Standard library only (no Flask needed).

  GET  /attempts/<id>/manifest   -> manifest in the CURRENT runner schema (datamodels.py),
                                    NOT yet SPEC-16's four-file shape.
  GET  /files/<name>             -> serves files from ./data
  POST /callbacks                -> prints each envelope live, returns 200

Two attempt ids:
  GOOD_ID  -> every sha256 correct -> attempt_completed
  BAD_ID   -> truth_vcf sha256 deliberately wrong -> checksum failure,
              failure_class shows up in the sample_failed callback, and the
              earlier attempt_started / sample_started checkpoints survive.

The OIDC bearer is printed (first 8 chars) but never validated.
"""
from __future__ import annotations

import hashlib
import json
import os
import ssl
import sys
import tarfile
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
DATA = HERE / "data"
HOST = os.environ.get("MOCK_HOST", "localhost")
PORT = int(os.environ.get("MOCK_PORT", "8443"))
BASE = os.environ.get("MOCK_PUBLIC_URL", f"https://{HOST}:{PORT}")  # set to tunnel URL if using cloudflared

GOOD_ID = "11111111-1111-4111-8111-111111111111"
BAD_ID = "22222222-2222-4222-8222-222222222222"

# role -> file served from ./data
ROLE_FILES = {
    "truth_vcf": "SEARCH-8113.vcf.gz",
    "truth_tbi": "SEARCH-8113.vcf.gz.tbi",
    "rtg_sdf": "rtg_sdf.tar.gz",
    "reference_fasta": "reference.fa",
    "query_vcf": "query_SEARCH-8113.vcf.gz",
    "primer_bed": "primers.bed",
}


def _ensure_sdf_tarball() -> None:
    tgz = DATA / "rtg_sdf.tar.gz"
    if not tgz.exists():
        with tarfile.open(tgz, "w:gz") as tar:
            tar.add(DATA / "rtg_sdf", arcname="rtg_sdf")


def _file_entry(role: str, corrupt: bool = False) -> dict:
    path = DATA / ROLE_FILES[role]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if corrupt:
        digest = "0" * 64  # well-formed hex, wrong value
    return {
        "role": role,
        "url": f"{BASE}/files/{path.name}",
        "sha256": digest,
        "size": path.stat().st_size,  # sizes known -> 50 GiB fallback floor not applied
    }


def build_manifest(attempt_id: str) -> dict:
    corrupt_truth = attempt_id == BAD_ID
    truth_files = [
        _file_entry("truth_vcf", corrupt=corrupt_truth),
        _file_entry("truth_tbi"),
        _file_entry("rtg_sdf"),
        _file_entry("reference_fasta"),
    ]
    return {
        "schema_version": "1.0",
        "attempt_id": attempt_id,
        "execution_id": str(uuid.uuid5(uuid.NAMESPACE_URL, attempt_id)),
        "operational_deadline_seconds": 75 * 60,
        "hard_timeout_seconds": 90 * 60,
        "executor": {
            "veritas_commit": "0" * 40,
            "environment_digest": "sha256:" + "0" * 64,
            "parser_version": "demo",
        },
        "samples": [
            {
                "sample_run_id": "SEARCH-8113",
                "sample_order": 1,
                "query_type": "vcf",
                "truth_bundle": {
                    "id": "sars-cov-2-SEARCH-8113",
                    "version": "v1.0.0",
                    "files": truth_files,
                },
                "query_input": _file_entry("query_vcf"),
                "region_annotations": {"primer_bed": _file_entry("primer_bed")},
            }
        ],
    }


CALLBACKS: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter default log
        sys.stderr.write(f"[http] {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}\n")

    def _json(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parts = self.path.split("?")[0].strip("/").split("/")
        if len(parts) == 3 and parts[0] == "attempts" and parts[2] == "manifest":
            attempt_id = parts[1]
            bearer = self.headers.get("Authorization", "")[7:15]
            print(f"\n>>> MANIFEST requested for {attempt_id} (bearer {bearer}...)", flush=True)
            if attempt_id not in (GOOD_ID, BAD_ID):
                return self._json(404, {"detail": "attempt not found"})
            return self._json(200, build_manifest(attempt_id))

        if len(parts) == 2 and parts[0] == "files":
            path = DATA / parts[1]
            if not path.is_file() or path.parent != DATA:
                return self._json(404, {"detail": "file not found"})
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if parts == ["callbacks"]:  # handy for the demo: list what arrived
            return self._json(200, {"callbacks": CALLBACKS})
        self._json(404, {"detail": "no route"})

    def do_POST(self):
        if self.path.rstrip("/") != "/callbacks":
            return self._json(404, {"detail": "no route"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        CALLBACKS.append(body)
        print(
            f"<<< CALLBACK #{len(CALLBACKS)} {body.get('event_type')}"
            f"  sample={body.get('sample_run_id')}"
            f"  idem={self.headers.get('Idempotency-Key')}\n"
            f"    payload={json.dumps(body.get('payload', {}))}",
            flush=True,
        )
        self._json(200, {"ok": True})


def main() -> None:
    _ensure_sdf_tarball()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    if not os.environ.get("MOCK_NO_TLS"):  # set MOCK_NO_TLS=1 when cloudflared terminates TLS
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(HERE / "certs" / "localhost.crt", HERE / "certs" / "localhost.key")
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    print(f"Mock PathoEQA on {BASE}\n  good attempt: {GOOD_ID}\n  bad  attempt: {BAD_ID}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
