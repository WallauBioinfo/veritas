"""
Integration tests for Veritas CLI commands.
"""

import pytest
import os
from click.testing import CliRunner
from veritas.commands.main import (
    cli,
    list_datasets,
    get_dataset,
    validate,
    convert,
    rtg_format,
)


class TestCLI:
    """Tests for CLI interface."""

    def test_cli_help(self):
        """Test that CLI help works."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0
        assert "Tool to get benchmark datasets and compare VCF files" in result.output

    def test_list_datasets_help(self):
        """Test list-datasets help."""
        runner = CliRunner()
        result = runner.invoke(list_datasets, ["--help"])

        assert result.exit_code == 0
        assert "List datasets available" in result.output

    def test_validate_help(self):
        """Test validate help."""
        runner = CliRunner()
        result = runner.invoke(validate, ["--help"])

        assert result.exit_code == 0
        assert "Validate query variants" in result.output

    def test_convert_help(self):
        """Test convert help."""
        runner = CliRunner()
        result = runner.invoke(convert, ["--help"])

        assert result.exit_code == 0
        assert "Convert FASTA to VCF" in result.output

    def test_rtg_format_help(self):
        """Test rtg-format help."""
        runner = CliRunner()
        result = runner.invoke(rtg_format, ["--help"])

        assert result.exit_code == 0
        assert "Format reference FASTA" in result.output


class TestValidateCommand:
    """Tests for validate command."""

    def test_validate_missing_required_args(self):
        """Test validate fails without required arguments."""
        runner = CliRunner()
        result = runner.invoke(validate, [])

        # Should fail due to missing arguments
        assert result.exit_code != 0

    def test_validate_mutually_exclusive_args(self):
        """Test validate fails with both query-vcf and query-fasta."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            # Create dummy files
            open("truth.vcf.gz", "a").close()
            open("query.vcf.gz", "a").close()
            open("query.fa", "a").close()
            os.mkdir("rtg_sdf")

            result = runner.invoke(
                validate,
                [
                    "--truth-vcf",
                    "truth.vcf.gz",
                    "--query-vcf",
                    "query.vcf.gz",
                    "--query-fasta",
                    "query.fa",
                    "--rtg-reference",
                    "rtg_sdf",
                ],
            )

            # Should fail due to mutually exclusive options
            assert result.exit_code != 0
            assert "Cannot specify both" in result.output

    def test_validate_query_fasta_requires_reference(self):
        """Test validate fails when query-fasta is used without reference."""
        runner = CliRunner()
        with runner.isolated_filesystem():
            open("truth.vcf.gz", "a").close()
            open("query.fa", "a").close()
            os.mkdir("rtg_sdf")

            result = runner.invoke(
                validate,
                [
                    "--truth-vcf",
                    "truth.vcf.gz",
                    "--query-fasta",
                    "query.fa",
                    "--rtg-reference",
                    "rtg_sdf",
                ],
            )

            # Should fail due to missing reference
            assert result.exit_code != 0
            assert "reference is required" in result.output


class TestConvertCommand:
    """Tests for convert command."""

    def test_convert_missing_args(self):
        """Test convert fails without required arguments."""
        runner = CliRunner()
        result = runner.invoke(convert, [])

        # Should fail due to missing arguments
        assert result.exit_code != 0

    def test_convert_reference_and_query_required(self):
        """Test convert requires both reference and query."""
        runner = CliRunner()
        result = runner.invoke(convert, ["--reference", "ref.fa"])

        # Should fail due to missing query
        assert result.exit_code != 0


class TestRTGFormatCommand:
    """Tests for rtg-format command."""

    def test_rtg_format_missing_args(self):
        """Test rtg-format fails without required arguments."""
        runner = CliRunner()
        result = runner.invoke(rtg_format, [])

        # Should fail due to missing arguments
        assert result.exit_code != 0

    def test_rtg_format_requires_both_args(self):
        """Test rtg-format requires both reference and output."""
        runner = CliRunner()
        result = runner.invoke(rtg_format, ["--reference", "ref.fa"])

        # Should fail due to missing output
        assert result.exit_code != 0
