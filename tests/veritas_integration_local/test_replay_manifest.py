"""The manifest replay tool: feed a manifest + its real input files, inspect TSV, callbacks, delivery."""
import json
import uuid

from tests.veritas_integration_local.conftest import ARTEFACT_BYTES
from tests.tools import replay_manifest


def _inputs(tmp_path, manifest_factory):
    files = tmp_path / "inputs"
    files.mkdir()
    for name, data in ARTEFACT_BYTES.items():
        (files / name).write_bytes(data)          # arbitrary names: matched by hash
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(manifest_factory(str(uuid.uuid4()), samples=2)))
    return manifest, files


def _replay(manifest, files, tmp_path):
    return replay_manifest.main([str(manifest), "--files-dir", str(files), "--workdir", str(tmp_path / "out")])


def test_replay_runs_the_manifest_and_reports_callbacks_tsv_and_delivery(
    server, manifest_factory, fake_veritas, tmp_path, capsys
):
    manifest, files = _inputs(tmp_path, manifest_factory)

    code = _replay(manifest, files, tmp_path)

    out = capsys.readouterr().out
    assert code == 0
    assert "Attempt finished: attempt_completed" in out
    assert "sample_completed" in out and "attempt_completed" in out
    assert out.count("PathoEQA TSV parser: ACCEPTED") == 2
    assert out.count("delivery: HTTP 200") == 2


def test_replay_shows_why_pathoeqa_rejects_a_tsv(server, manifest_factory, fake_veritas, tmp_path, capsys, monkeypatch):
    manifest, files = _inputs(tmp_path, manifest_factory)
    monkeypatch.setenv("VERITAS_FAKE_MODE", "legacy_tsv")

    code = _replay(manifest, files, tmp_path)

    out = capsys.readouterr().out
    assert code == 1
    assert "REJECTED - metric_required" in out and "delivery: HTTP 422" in out


def test_replay_lists_missing_input_files(server, manifest_factory, fake_veritas, tmp_path, capsys):
    manifest, files = _inputs(tmp_path, manifest_factory)
    (files / "primers.bed").unlink()

    code = _replay(manifest, files, tmp_path)

    out = capsys.readouterr().out
    assert code == 2 and "primer_bed" in out
