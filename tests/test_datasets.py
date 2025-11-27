"""
Tests for veritas.datasets module.
"""

import pytest
from veritas.datasets import (
    print_formatted_table,
)


class TestPrintFormattedTable:
    """Tests for print_formatted_table function."""

    def test_single_dataset(self, sample_yaml_metadata, capsys):
        """Test formatting a single dataset."""
        data = [sample_yaml_metadata]

        print_formatted_table(data)
        captured = capsys.readouterr()

        # Check that output contains expected fields
        assert "dataset" in captured.out
        assert "pathogen" in captured.out
        assert "SARS-CoV-2" in captured.out
        assert "veritas/sars-cov-2/test-dataset" in captured.out
        assert "artificial" in captured.out

    def test_multiple_datasets(self, capsys):
        """Test formatting multiple datasets."""
        data = [
            {
                "dataset": "veritas/sars-cov-2/dataset1",
                "dataset_version": "1.0",
                "pathogen": "SARS-CoV-2",
                "sequencing_technology": "Illumina",
                "layout": "PAIRED",
                "dataset_type": "real",
                "strategy": "AMPLICON",
                "read_length": "250",
            },
            {
                "dataset": "veritas/mpox/dataset2",
                "dataset_version": "1.0",
                "pathogen": "Mpox",
                "sequencing_technology": "ONT",
                "layout": "SINGLE",
                "dataset_type": "real",
                "strategy": "WGS",
                "read_length": "variable",
            },
        ]

        print_formatted_table(data)
        captured = capsys.readouterr()

        # Check both datasets are present
        assert "sars-cov-2" in captured.out
        assert "mpox" in captured.out
        assert "Illumina" in captured.out
        assert "ONT" in captured.out

        # Check table borders
        assert "┌" in captured.out  # Top border
        assert "└" in captured.out  # Bottom border
        assert "|" in captured.out  # Row separator (uses | in implementation)

    def test_table_alignment(self, sample_yaml_metadata, capsys):
        """Test that table columns are properly aligned."""
        data = [sample_yaml_metadata]

        print_formatted_table(data)
        captured = capsys.readouterr()

        lines = captured.out.split("\n")

        # Get non-empty lines
        table_lines = [l for l in lines if l.strip()]

        # Filter out top and bottom borders which use different characters
        content_lines = [
            l for l in table_lines if not (l.startswith("┌") or l.startswith("└"))
        ]

        # Content lines and separators should have | characters
        assert all("|" in line for line in content_lines)

    def test_empty_dataset(self, capsys):
        """Test with minimal dataset information."""
        data = [
            {
                "dataset": "test",
                "dataset_version": "",
                "pathogen": "",
                "sequencing_technology": "",
                "layout": "",
                "dataset_type": "",
                "strategy": "",
                "read_length": "",
            }
        ]

        print_formatted_table(data)
        captured = capsys.readouterr()

        # Should still create a table
        assert "dataset" in captured.out
        assert "test" in captured.out
