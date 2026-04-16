import pytest
from unittest.mock import patch, MagicMock
from veritas.tools import run_rtg_format, run_gsalign, _require_tool


class TestRequireTool:
    """Tests for the _require_tool pre-flight helper."""

    def test_raises_when_tool_missing(self):
        """RuntimeError is raised when the tool is not on PATH."""
        with patch("veritas.tools.shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="not found on PATH"):
                _require_tool("nonexistent_tool")

    def test_does_not_raise_when_tool_present(self):
        """No exception raised when shutil.which finds the tool."""
        with patch("veritas.tools.shutil.which", return_value="/usr/bin/fake_tool"):
            _require_tool("fake_tool")  # should not raise


class TestRunRtgFormat:
    """Tests for run_rtg_format()."""

    def test_calls_rtg_format_with_correct_args(self):
        """subprocess.run is invoked with the expected rtg format command."""
        with patch("veritas.tools.shutil.which", return_value="/usr/bin/rtg"):
            with patch("veritas.tools.subprocess.run") as mock_run:
                run_rtg_format("reference.fa", "output_sdf")
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "rtg"
        assert "format" in cmd
        assert "reference.fa" in cmd
        assert "output_sdf" in cmd

    def test_raises_when_rtg_missing(self):
        """RuntimeError raised if rtg is not on PATH."""
        with patch("veritas.tools.shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="rtg"):
                run_rtg_format("reference.fa", "output_sdf")

    def test_subprocess_called_with_check_true(self):
        """subprocess.run is always called with check=True."""
        with patch("veritas.tools.shutil.which", return_value="/usr/bin/rtg"):
            with patch("veritas.tools.subprocess.run") as mock_run:
                run_rtg_format("reference.fa", "output_sdf")
        _, kwargs = mock_run.call_args
        assert kwargs.get("check") is True


class TestRunGsalign:
    """Tests for run_gsalign()."""

    def _make_completed_process(self, stderr=b""):
        mock_result = MagicMock()
        mock_result.stderr = stderr
        return mock_result

    def test_returns_expected_output_paths(self):
        """Return value is (prefix.vcf, prefix.maf)."""
        with patch("veritas.tools.shutil.which", return_value="/usr/bin/GSAlign"):
            with patch(
                "veritas.tools.subprocess.run",
                return_value=self._make_completed_process(),
            ):
                vcf, maf = run_gsalign("ref.fa", "query.fa", "/tmp/out")
        assert vcf == "/tmp/out.vcf"
        assert maf == "/tmp/out.maf"

    def test_raises_when_gsalign_missing(self):
        """RuntimeError raised if GSAlign is not on PATH."""
        with patch("veritas.tools.shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="GSAlign"):
                run_gsalign("ref.fa", "query.fa", "/tmp/out")

    def test_gsalign_flags_present(self):
        """-fmt 1 and -sen flags are always passed to GSAlign."""
        with patch("veritas.tools.shutil.which", return_value="/usr/bin/GSAlign"):
            with patch(
                "veritas.tools.subprocess.run",
                return_value=self._make_completed_process(),
            ) as mock_run:
                run_gsalign("ref.fa", "query.fa", "/tmp/out")
        cmd = mock_run.call_args[0][0]
        assert "-fmt" in cmd
        assert "1" in cmd
        assert "-sen" in cmd

    def test_stderr_logged_at_debug(self):
        """Non-empty stderr from GSAlign is forwarded to logger.debug."""
        with patch("veritas.tools.shutil.which", return_value="/usr/bin/GSAlign"):
            with patch(
                "veritas.tools.subprocess.run",
                return_value=self._make_completed_process(stderr=b"warning: slow path"),
            ):
                with patch("veritas.tools.logger") as mock_logger:
                    run_gsalign("ref.fa", "query.fa", "/tmp/out")
        assert mock_logger.debug.called
        logged = " ".join(str(c) for c in mock_logger.debug.call_args_list)
        assert "GSAlign" in logged

    def test_empty_stderr_produces_no_log(self):
        """Empty stderr from GSAlign produces no debug log calls."""
        with patch("veritas.tools.shutil.which", return_value="/usr/bin/GSAlign"):
            with patch(
                "veritas.tools.subprocess.run",
                return_value=self._make_completed_process(stderr=b""),
            ):
                with patch("veritas.tools.logger") as mock_logger:
                    run_gsalign("ref.fa", "query.fa", "/tmp/out")
        mock_logger.debug.assert_not_called()
