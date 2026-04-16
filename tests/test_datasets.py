import base64
import json
import os
import pytest
import yaml
from unittest.mock import MagicMock, patch
from veritas.datasets import (
    print_formatted_table,
    find_dataset_file,
    find_dataset_dir,
    get_datasets_paths,
    get_metadata_information,
)


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


class TestPrintFormattedTableEmptyList:
    """Edge-case: empty input list."""

    def test_empty_list_does_not_raise(self, capsys):
        """Empty dataset list returns without error and prints no table."""
        print_formatted_table([])
        captured = capsys.readouterr()
        assert "┌" not in captured.out


class TestGetDatasetsPaths:
    """Tests for get_datasets_paths() — all HTTP calls are mocked."""

    def _mock_tree(self, paths):
        items = [{"type": "blob", "path": p} for p in paths]
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"tree": items}
        mock_resp.raise_for_status = MagicMock()
        return mock_resp

    def test_returns_only_metadata_yaml_paths(self):
        """Only paths that end with metadata.yaml are returned."""
        mock_resp = self._mock_tree(
            [
                "datasets/pathogen/sample-A/metadata.yaml",
                "datasets/pathogen/sample-A/reference.fa",
                "README.md",
            ]
        )
        with patch("veritas.datasets.requests.get", return_value=mock_resp):
            result = get_datasets_paths("owner/repo/git/trees/main?recursive=1", {})
        assert result == ["datasets/pathogen/sample-A/metadata.yaml"]

    def test_empty_tree_returns_empty_list(self):
        """A repository with no files returns an empty list."""
        mock_resp = self._mock_tree([])
        with patch("veritas.datasets.requests.get", return_value=mock_resp):
            result = get_datasets_paths("owner/repo/git/trees/main?recursive=1", {})
        assert result == []

    def test_multiple_metadata_files_all_returned(self):
        """All metadata.yaml files across multiple datasets are collected."""
        mock_resp = self._mock_tree(
            [
                "datasets/virus/sample-A/metadata.yaml",
                "datasets/virus/sample-B/metadata.yaml",
                "datasets/virus/sample-A/truth.vcf.gz",
            ]
        )
        with patch("veritas.datasets.requests.get", return_value=mock_resp):
            result = get_datasets_paths("owner/repo/git/trees/main?recursive=1", {})
        assert len(result) == 2

    def test_http_error_propagates(self):
        """raise_for_status() exceptions bubble up to the caller."""
        import requests as req_lib

        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = req_lib.exceptions.HTTPError("403")
        with patch("veritas.datasets.requests.get", return_value=mock_resp):
            with pytest.raises(req_lib.exceptions.HTTPError):
                get_datasets_paths("owner/repo/git/trees/main", {})

    def test_request_uses_timeout(self):
        """requests.get is called with a timeout parameter."""
        mock_resp = self._mock_tree([])
        with patch("veritas.datasets.requests.get", return_value=mock_resp) as mock_get:
            get_datasets_paths("owner/repo/git/trees/main?recursive=1", {})
        _, kwargs = mock_get.call_args
        assert "timeout" in kwargs


class TestGetMetadataInformation:
    """Tests for get_metadata_information() — all HTTP calls are mocked."""

    def _make_response(self, yaml_dict):
        encoded = base64.b64encode(yaml.dump(yaml_dict).encode()).decode()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"content": encoded}
        mock_resp.raise_for_status = MagicMock()
        return mock_resp

    def test_returns_parsed_metadata(self, sample_yaml_metadata):
        """Decoded YAML content is returned with a 'dataset' key injected."""
        mock_resp = self._make_response(sample_yaml_metadata)
        with patch("veritas.datasets.requests.get", return_value=mock_resp):
            result = get_metadata_information(
                "owner/repo/contents",
                ["datasets/pathogen/sample-A/metadata.yaml"],
                {},
            )
        assert len(result) == 1
        assert result[0]["pathogen"] == sample_yaml_metadata["pathogen"]
        assert "dataset" in result[0]

    def test_empty_paths_returns_empty_list(self):
        """No paths → no HTTP calls, empty list returned."""
        with patch("veritas.datasets.requests.get") as mock_get:
            result = get_metadata_information("owner/repo/contents", [], {})
        mock_get.assert_not_called()
        assert result == []

    def test_multiple_paths_all_fetched(self, sample_yaml_metadata):
        """One HTTP call is made per path."""
        mock_resp = self._make_response(sample_yaml_metadata)
        paths = [
            "datasets/pathogen/sample-A/metadata.yaml",
            "datasets/pathogen/sample-B/metadata.yaml",
        ]
        with patch("veritas.datasets.requests.get", return_value=mock_resp) as mock_get:
            result = get_metadata_information("owner/repo/contents", paths, {})
        assert mock_get.call_count == 2
        assert len(result) == 2
