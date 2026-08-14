## Artifact Handling Workflow

Artifact handling in `veritas-runner` follows a fail-fast architecture designed to prevent unnecessary network traffic and ensure strict byte-level integrity.

---

### Pipeline Ingestion Flow

Prior to downloading large data artifacts (like truth VCFs or RTG SDF references), the runner validates manifest compatibility in accordance with **SPEC-05** rules.

```text
┌────────────────────────────────┐
│  1. Download / Parse Manifest  │
└──────────────┬─────────────────┘
               │
               ▼
┌────────────────────────────────┐
│  2. Validate Versions (SPEC-05)│  <-- Fail fast before downloading big files!
└──────────────┬─────────────────┘  (Manifest, Policy, & Truth Package versions)
               │
               ▼
┌────────────────────────────────┐
│  3. ArtefactClient.download()  │  <-- Stream & verify SHA-256 / size
└────────────────────────────────┘
```

1. **Manifest Parsing & Validation**: Reads the manifest and verifies manifest, policy, and truth-package schema versions. If incompatible, execution halts immediately.
2. **Artifact Materialization**: Hands off individual file references to `ArtefactClient` for cached lookup or network streaming.

---

### `ArtefactClient` Download Lifecycle

Each artifact streaming operation managed by `ArtefactClient.download()` separates network streaming from local CPU/Disk validation to minimize open socket lifetimes and keep memory footprints predictable:

```text
download()
│
├── 1. self._is_cached() ──────────────► [LOCAL CPU / DISK]
│                                        Reads existing local file & hashes SHA-256.
│                                        No network request or HTTP session used.
│
├── 2. self._stream_to_disk() ─────────► [NETWORK STREAM]
│   │                                    Uses self.session.get(...)
│   └── with session.get(...)            Socket OPEN: Chunks read, written to
│       └─ stream loop                   <path>.part, and hashed in RAM.
│   │
│   └── (context exits) ────────────────► Socket CLOSED.
│
├── 3. self._verify_written() ─────────► [LOCAL CPU]
│                                        Compares in-memory SHA-256 digest & size
│                                        against expected manifest metadata.
│
└── 4. os.replace(tmp_path, dest_path) ─► [LOCAL DISK]
                                         Atomic rename from <path>.part to final dest.
```

#### Key Reliability & Security Features:
* **HTTP Connection Reuse**: Uses a persistent `requests.Session` during streaming to leverage TCP Keep-Alive across multiple artifact downloads.
* **Atomic Writes**: Downloads stream directly to `<dest_path>.part` files and are atomically replaced (`os.replace`) only after digest verification succeeds.
* **Zip-Slip Protection**: Safe unpacking of archives (`.zip`, `.tar.gz`) preventing directory traversal vulnerabilities.
* **Bounded Deadline Checks**: Streams evaluate operational deadline limits between chunks to guarantee clean `DEADLINE_EXCEEDED` reporting.