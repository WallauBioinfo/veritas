"""
tests/mock_pathoeqa.py

A real (tiny) HTTPS server that impersonates the PathoEQA control plane plus the
signed artefact storage it points at. Nothing is monkeypatched: the runner opens
real sockets, does real TLS, streams real bytes and hashes them. That is the
whole point - these are integration tests, not unit tests with mocks.

Why HTTPS and not HTTP: `ManifestFile.check_https` rejects any non-HTTPS URL, so
a plain-HTTP mock could never appear in a valid manifest. The server therefore
serves a throw-away self-signed certificate for `localhost`, and the test
fixture exports REQUESTS_CA_BUNDLE so `requests` trusts exactly that cert.

Endpoints
    GET  /attempts/{attempt_id}/manifest   -> manifest JSON
    POST /callbacks                        -> 202, envelope recorded
    GET  /artefacts/{name}                 -> raw artefact bytes

Fault injection (all optional, all per-instance):
    server.manifest_status  : list[int]  status codes to return, one per call,
                              falling back to 200 once exhausted
    server.artefact_status  : dict[name, list[int]]  same, per artefact
    server.callback_status  : list[int]
    server.corrupt          : set[name]  serve one flipped byte (checksum test)
    server.truncate         : set[name]  serve half the bytes (size test)
    server.slow_artefacts   : dict[name, float]  seconds to stall mid-stream
"""

from __future__ import annotations

import json
import subprocess
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Set


# --------------------------------------------------------------------- TLS


def make_self_signed_cert(directory: Path) -> tuple[Path, Path]:
    """Generate a short-lived localhost cert with the openssl CLI."""
    directory.mkdir(parents=True, exist_ok=True)
    cert = directory / "cert.pem"
    key = directory / "key.pem"
    if cert.exists() and key.exists():
        return cert, key
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "2",
            "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


# ------------------------------------------------------------------ handler


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # keep pytest output readable
    def log_message(self, fmt, *args):  # noqa: A003
        pass

    # -- helpers ---------------------------------------------------------
    @property
    def srv(self) -> "MockPathoEQA":
        return self.server.owner  # type: ignore[attr-defined]

    def _send(self, status: int, body: bytes = b"", content_type: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    @staticmethod
    def _next_status(queue: Optional[List[int]]) -> int:
        if queue:
            return queue.pop(0)
        return 200

    # -- routes ----------------------------------------------------------
    def do_GET(self):  # noqa: N802
        srv = self.srv
        path = self.path.split("?")[0]

        if path.endswith("/manifest") and path.startswith("/attempts/"):
            srv.manifest_requests += 1
            status = self._next_status(srv.manifest_status)
            if status != 200:
                return self._send(status, json.dumps({"detail": "injected"}).encode())
            if srv.manifest is None:
                return self._send(404, b'{"detail":"no manifest configured"}')
            return self._send(200, json.dumps(srv.manifest).encode())

        if path.startswith("/artefacts/"):
            name = path[len("/artefacts/"):]
            srv.artefact_requests[name] = srv.artefact_requests.get(name, 0) + 1
            status = self._next_status(srv.artefact_status.get(name))
            if status != 200:
                return self._send(status, b'{"detail":"injected"}')
            data = srv.artefacts.get(name)
            if data is None:
                return self._send(404, b'{"detail":"unknown artefact"}')
            if name in srv.corrupt:
                data = bytes([data[0] ^ 0xFF]) + data[1:]
            if name in srv.truncate:
                data = data[: len(data) // 2]

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            stall = srv.slow_artefacts.get(name)
            if stall:
                half = len(data) // 2
                self.wfile.write(data[:half])
                self.wfile.flush()
                time.sleep(stall)
                self.wfile.write(data[half:])
            else:
                self.wfile.write(data)
            return

        return self._send(404, b'{"detail":"not found"}')

    def do_POST(self):  # noqa: N802
        srv = self.srv
        if self.path.split("?")[0] != "/callbacks":
            return self._send(404, b'{"detail":"not found"}')

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            envelope = json.loads(raw)
        except ValueError:
            return self._send(400, b'{"detail":"bad json"}')

        srv.callbacks.append(
            {
                "envelope": envelope,
                "idempotency_key": self.headers.get("Idempotency-Key"),
                "authorization": bool(self.headers.get("Authorization")),
            }
        )
        status = self._next_status(srv.callback_status)
        return self._send(status if status != 200 else 202, b'{"ok":true}')


# ------------------------------------------------------------------- server


class MockPathoEQA:
    """Threaded HTTPS stand-in for PathoEQA + artefact storage."""

    def __init__(self, certfile: Path, keyfile: Path):
        self.manifest: Optional[dict] = None
        self.artefacts: Dict[str, bytes] = {}

        # observations
        self.callbacks: List[dict] = []
        self.manifest_requests = 0
        self.artefact_requests: Dict[str, int] = {}

        # fault injection
        self.manifest_status: List[int] = []
        self.artefact_status: Dict[str, List[int]] = {}
        self.callback_status: List[int] = []
        self.corrupt: Set[str] = set()
        self.truncate: Set[str] = set()
        self.slow_artefacts: Dict[str, float] = {}

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.owner = self  # type: ignore[attr-defined]
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))
        self._httpd.socket = context.wrap_socket(self._httpd.socket, server_side=True)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    # -- lifecycle -------------------------------------------------------
    def start(self) -> "MockPathoEQA":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "MockPathoEQA":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- addressing ------------------------------------------------------
    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def base_url(self) -> str:
        return f"https://localhost:{self.port}"

    def artefact_url(self, name: str) -> str:
        return f"{self.base_url}/artefacts/{name}"

    # -- convenience -----------------------------------------------------
    def add_artefact(self, name: str, data: bytes) -> None:
        self.artefacts[name] = data

    def events(self) -> List[str]:
        """Ordered list of event_type values PathoEQA received."""
        return [c["envelope"]["event_type"] for c in self.callbacks]

    def terminal_event(self) -> Optional[dict]:
        for c in reversed(self.callbacks):
            if c["envelope"]["event_type"].startswith("attempt_") and c["envelope"][
                "event_type"
            ] != "attempt_started":
                return c["envelope"]
        return None
