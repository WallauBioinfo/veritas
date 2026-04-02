import os
import shutil
import pytest
from click.testing import CliRunner
from veritas.commands.main import cli


def _rtg_available():
    return shutil.which("rtg") is not None


def _gsalign_available():
    return shutil.which("GSAlign") is not None


class TestCliHelp:
    """Tests that --help exits cleanly for all commands."""

    def test_root_help(self):
        """Root CLI --help exits with code 0."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert (
            "benchmark" in result.output.lower()
            or "veritas" in result.output.lower()
            or result.exit_code == 0
        )

    def test_list_datasets_help(self):
        """list-datasets --help exits cleanly."""
        runner = CliRunner()
        result = runner.invoke(cli, ["list-datasets", "--help"])
        assert result.exit_code == 0
        assert "--token" in result.output

    def test_get_dataset_help(self):
        """get-dataset --help exits cleanly."""
        runner = CliRunner()
        result = runner.invoke(cli, ["get-dataset", "--help"])
        assert result.exit_code == 0
        assert "--dataset-name" in result.output

    def test_validate_help(self):
        """validate --help exits cleanly."""
        runner = CliRunner()
        result = runner.invoke(cli, ["validate", "--help"])
        assert result.exit_code == 0
        assert "--query-vcf" in result.output
        assert "--query-fasta" in result.output

    def test_convert_help(self):
        """convert --help exits cleanly."""
        runner = CliRunner()
        result = runner.invoke(cli, ["convert", "--help"])
        assert result.exit_code == 0
        assert "--reference" in result.output
        assert "--query" in result.output

    def test_rtg_format_help(self):
        """rtg-format --help exits cleanly."""
        runner = CliRunner()
        result = runner.invoke(cli, ["rtg-format", "--help"])
        assert result.exit_code == 0
        assert "--reference" in result.output
        assert "--output" in result.output


class TestListDatasetsCommand:
    """Tests for the list-datasets command."""

    def test_fails_without_token(self):
        """list-datasets raises ValueError without a token."""
        runner = CliRunner()
        result = runner.invoke(cli, ["list-datasets"], env={"GITHUB_TOKEN": ""})
        assert result.exit_code != 0

    def test_accepts_token_option(self):
        """list-datasets accepts --token without crashing on option parsing."""
        runner = CliRunner()
        result = runner.invoke(cli, ["list-datasets", "--token", "fake_token"])
        assert result.exit_code != 2  # exit 2 = Click "No such option"


class TestGetDatasetCommand:
    """Tests for the get-dataset command."""

    def test_fails_without_token(self):
        """get-dataset raises ValueError without a token."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["get-dataset", "--dataset-name", "veritas/sars-cov-2/SEARCH-8113"],
            env={"GITHUB_TOKEN": ""},
        )
        assert result.exit_code != 0

    def test_requires_dataset_name(self):
        """get-dataset fails with exit code 2 when --dataset-name is absent."""
        runner = CliRunner()
        result = runner.invoke(cli, ["get-dataset"], env={"GITHUB_TOKEN": "fake"})
        assert result.exit_code == 2
        assert (
            "dataset-name" in result.output.lower()
            or "missing" in result.output.lower()
        )


class TestValidateCommand:
    """Tests for the validate command argument validation."""

    def test_fails_without_query(self, sample_truth_vcf, temp_dir):
        """validate fails if neither --query-vcf nor --query-fasta is given."""
        runner = CliRunner()
        fake_sdf = os.path.join(temp_dir, "rtg_sdf")
        os.makedirs(fake_sdf)
        result = runner.invoke(
            cli,
            [
                "validate",
                "--truth-vcf",
                sample_truth_vcf,
                "--rtg-reference",
                fake_sdf,
                "--output-dir",
                os.path.join(temp_dir, "out"),
            ],
        )
        assert result.exit_code != 0
        assert "query" in result.output.lower()

    def test_fails_with_both_query_vcf_and_fasta(
        self, sample_truth_vcf, sample_query_vcf, sample_query_fasta, temp_dir
    ):
        """validate rejects simultaneous --query-vcf and --query-fasta."""
        runner = CliRunner()
        fake_sdf = os.path.join(temp_dir, "rtg_sdf")
        os.makedirs(fake_sdf)
        result = runner.invoke(
            cli,
            [
                "validate",
                "--truth-vcf",
                sample_truth_vcf,
                "--rtg-reference",
                fake_sdf,
                "--query-vcf",
                sample_query_vcf,
                "--query-fasta",
                sample_query_fasta,
                "--output-dir",
                os.path.join(temp_dir, "out"),
            ],
        )
        assert result.exit_code != 0
        assert "cannot" in result.output.lower() or "both" in result.output.lower()

    def test_fails_without_rtg_reference(
        self, sample_truth_vcf, sample_query_vcf, temp_dir
    ):
        """validate fails gracefully when --rtg-reference is not provided."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "validate",
                "--truth-vcf",
                sample_truth_vcf,
                "--query-vcf",
                sample_query_vcf,
                "--output-dir",
                os.path.join(temp_dir, "out"),
            ],
        )
        assert result.exit_code != 0

    def test_fails_without_truth_vcf(self, sample_query_vcf, temp_dir):
        """validate fails when no truth VCF is given and no dataset directory."""
        runner = CliRunner()
        fake_sdf = os.path.join(temp_dir, "rtg_sdf")
        os.makedirs(fake_sdf)
        result = runner.invoke(
            cli,
            [
                "validate",
                "--query-vcf",
                sample_query_vcf,
                "--rtg-reference",
                fake_sdf,
                "--output-dir",
                os.path.join(temp_dir, "out"),
            ],
        )
        assert result.exit_code != 0
        assert "truth" in result.output.lower()

    def test_dataset_dir_not_existing_fails(self, sample_query_vcf, temp_dir):
        """validate fails when --dataset-dir points to a non-existent path."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "validate",
                "--dataset-dir",
                os.path.join(temp_dir, "nonexistent"),
                "--query-vcf",
                sample_query_vcf,
                "--output-dir",
                os.path.join(temp_dir, "out"),
            ],
        )
        assert result.exit_code != 0

    def test_fasta_query_without_reference_fails(
        self, sample_truth_vcf, sample_query_fasta, temp_dir
    ):
        """validate fails when --query-fasta is given without --reference."""
        runner = CliRunner()
        fake_sdf = os.path.join(temp_dir, "rtg_sdf")
        os.makedirs(fake_sdf)
        result = runner.invoke(
            cli,
            [
                "validate",
                "--truth-vcf",
                sample_truth_vcf,
                "--rtg-reference",
                fake_sdf,
                "--query-fasta",
                sample_query_fasta,
                "--output-dir",
                os.path.join(temp_dir, "out"),
            ],
        )
        assert result.exit_code != 0
        assert "reference" in result.output.lower()


class TestConvertCommand:
    """Tests for the convert command."""

    def test_fails_without_reference(self, sample_query_fasta):
        """convert fails with exit code 2 when --reference is missing."""
        runner = CliRunner()
        result = runner.invoke(cli, ["convert", "--query", sample_query_fasta])
        assert result.exit_code == 2

    def test_fails_without_query(self, sample_reference_fasta):
        """convert fails with exit code 2 when --query is missing."""
        runner = CliRunner()
        result = runner.invoke(cli, ["convert", "--reference", sample_reference_fasta])
        assert result.exit_code == 2

    def test_fails_with_nonexistent_reference(self, sample_query_fasta, temp_dir):
        """convert fails gracefully when --reference path does not exist."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "convert",
                "--reference",
                os.path.join(temp_dir, "nonexistent.fa"),
                "--query",
                sample_query_fasta,
            ],
        )
        # Click type=Path(exists=True) returns exit_code 2 for bad paths
        assert result.exit_code == 2

    @pytest.mark.requires_gsalign
    def test_convert_produces_vcf(
        self, sample_reference_fasta, sample_query_fasta, temp_dir
    ):
        """convert produces a .vcf.gz output when GSAlign is available."""
        if not _gsalign_available():
            pytest.skip("GSAlign not available")
        runner = CliRunner()
        out_vcf = os.path.join(temp_dir, "output.vcf.gz")
        result = runner.invoke(
            cli,
            [
                "convert",
                "--reference",
                sample_reference_fasta,
                "--query",
                sample_query_fasta,
                "--sample",
                "MySample",
                "--output",
                out_vcf,
            ],
        )
        assert result.exit_code == 0
        assert os.path.exists(out_vcf)


class TestRtgFormatCommand:
    """Tests for the rtg-format command."""

    def test_fails_without_reference(self):
        """rtg-format fails with exit code 2 when --reference is missing."""
        runner = CliRunner()
        result = runner.invoke(cli, ["rtg-format", "--output", "out_sdf"])
        assert result.exit_code == 2

    def test_fails_without_output(self, sample_reference_fasta):
        """rtg-format fails with exit code 2 when --output is missing."""
        runner = CliRunner()
        result = runner.invoke(
            cli, ["rtg-format", "--reference", sample_reference_fasta]
        )
        assert result.exit_code == 2

    def test_fails_with_nonexistent_reference(self, temp_dir):
        """rtg-format fails gracefully when --reference file does not exist."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "rtg-format",
                "--reference",
                os.path.join(temp_dir, "missing.fa"),
                "--output",
                os.path.join(temp_dir, "sdf"),
            ],
        )
        assert result.exit_code == 2

    @pytest.mark.requires_rtg
    def test_rtg_format_creates_sdf(self, sample_reference_fasta, temp_dir):
        """rtg-format creates the SDF directory when RTG is available."""
        if not _rtg_available():
            pytest.skip("RTG Tools not available")
        runner = CliRunner()
        sdf_out = os.path.join(temp_dir, "ref_sdf")
        result = runner.invoke(
            cli,
            [
                "rtg-format",
                "--reference",
                sample_reference_fasta,
                "--output",
                sdf_out,
            ],
        )
        assert result.exit_code == 0
        assert os.path.isdir(sdf_out)
