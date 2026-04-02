import os
import pytest
from veritas.datasets import print_formatted_table, find_dataset_file, find_dataset_dir


class TestPrintFormattedTable:
    """Tests for print_formatted_table()."""

    def test_single_row_no_exception(self, sample_yaml_metadata, capsys):
        """Single-row table prints without raising errors."""
        print_formatted_table([sample_yaml_metadata])
        captured = capsys.readouterr()
        assert len(captured.out) > 0

    def test_multiple_rows(self, sample_yaml_metadata, capsys):
        """Multiple rows are all rendered in output."""
        second = dict(sample_yaml_metadata)
        second["dataset"] = "veritas/sars-cov-2/other-dataset"
        second["dataset_type"] = "real"
        print_formatted_table([sample_yaml_metadata, second])
        captured = capsys.readouterr()
        assert "test-dataset" in captured.out
        assert "other-dataset" in captured.out

    def test_column_headers_present(self, sample_yaml_metadata, capsys):
        """All expected column headers appear in the output."""
        print_formatted_table([sample_yaml_metadata])
        captured = capsys.readouterr()
        for field in [
            "dataset",
            "pathogen",
            "sequencing_technology",
            "layout",
            "dataset_type",
            "strategy",
        ]:
            assert field in captured.out

    def test_metadata_values_present(self, sample_yaml_metadata, capsys):
        """Cell values from the fixture appear in the rendered table."""
        print_formatted_table([sample_yaml_metadata])
        captured = capsys.readouterr()
        assert "SARS-CoV-2" in captured.out
        assert "Illumina MiSeq" in captured.out
        assert "artificial" in captured.out

    def test_table_bordering_chars(self, sample_yaml_metadata, capsys):
        """Table has visual border characters."""
        print_formatted_table([sample_yaml_metadata])
        captured = capsys.readouterr()
        assert "┌" in captured.out
        assert "└" in captured.out
        assert "|" in captured.out

    def test_missing_optional_field(self, capsys):
        """Rows missing optional fields don't raise errors (uses empty string)."""
        minimal = {
            "dataset": "test/minimal",
            "dataset_version": "v1.0",
            "pathogen": "TestVirus",
            "sequencing_technology": "Illumina",
            "layout": "PAIRED",
            "dataset_type": "artificial",
            "strategy": "AMPLICON",
            # read_length intentionally omitted
        }
        print_formatted_table([minimal])
        captured = capsys.readouterr()
        assert "TestVirus" in captured.out

    def test_long_values_dont_truncate(self, capsys):
        """Long cell values widen the column and are not truncated."""
        long_name = "a-very-long-dataset-name-that-exceeds-normal-width"
        data = {
            "dataset": long_name,
            "dataset_version": "v1.0",
            "pathogen": "TestVirus",
            "sequencing_technology": "Illumina",
            "layout": "PAIRED",
            "dataset_type": "real",
            "strategy": "WGS",
            "read_length": "150",
        }
        print_formatted_table([data])
        captured = capsys.readouterr()
        assert long_name in captured.out


class TestFindDatasetFile:
    """Tests for find_dataset_file()."""

    def test_finds_existing_file(self, temp_dir):
        """Returns path when a matching file exists."""
        bed_path = os.path.join(temp_dir, "primers.bed")
        open(bed_path, "w").close()
        result = find_dataset_file(temp_dir, "primers.bed", "primers BED")
        assert result == bed_path

    def test_returns_none_when_no_match(self, temp_dir):
        """Returns None when no file matches the pattern."""
        result = find_dataset_file(temp_dir, "nonexistent.bed", "missing file")
        assert result is None

    def test_returns_none_when_dataset_dir_is_none(self):
        """Returns None immediately if dataset_dir is None."""
        result = find_dataset_file(None, "*.vcf.gz", "truth VCF")
        assert result is None

    def test_glob_pattern_matching(self, temp_dir):
        """Glob patterns like *.vcf.gz are resolved correctly."""
        vcf_path = os.path.join(temp_dir, "truth.vcf.gz")
        open(vcf_path, "w").close()
        result = find_dataset_file(temp_dir, "*.vcf.gz", "truth VCF")
        assert result == vcf_path


class TestFindDatasetDir:
    """Tests for find_dataset_dir()."""

    def test_finds_existing_directory(self, temp_dir):
        """Returns path when the named subdirectory exists."""
        sdf_dir = os.path.join(temp_dir, "rtg_sdf")
        os.makedirs(sdf_dir)
        result = find_dataset_dir(temp_dir, "rtg_sdf", "RTG SDF")
        assert result == sdf_dir

    def test_returns_none_when_directory_missing(self, temp_dir):
        """Returns None when the directory does not exist."""
        result = find_dataset_dir(temp_dir, "rtg_sdf", "RTG SDF")
        assert result is None

    def test_returns_none_when_dataset_dir_is_none(self):
        """Returns None immediately if dataset_dir is None."""
        result = find_dataset_dir(None, "rtg_sdf", "RTG SDF")
        assert result is None

    def test_file_not_confused_with_directory(self, temp_dir):
        """A file with the same name as the target dir does not match."""
        # Create a file called rtg_sdf (not a directory)
        not_a_dir = os.path.join(temp_dir, "rtg_sdf")
        open(not_a_dir, "w").close()
        result = find_dataset_dir(temp_dir, "rtg_sdf", "RTG SDF")
        # os.path.isdir will be False for a regular file
        assert result is None
