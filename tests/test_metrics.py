import os
import pytest
import pandas as pd
from unittest.mock import MagicMock, patch
from veritas.validate import calc_metrics, save_metrics_tsv


def _mock_record(pos, ref, alt, tag, var_type, bed):
    """Create a mock pysam VariantRecord with the fields calc_metrics reads."""
    rec = MagicMock()
    rec.pos = pos
    rec.ref = ref
    rec.alts = (alt,)
    info = {"TAG": tag, "TYPE": var_type, "BED": tuple(bed)}
    rec.info.get = lambda key, default=None: info.get(key, default)
    return rec


def _run_calc_metrics(records, **kwargs):
    """
    Call calc_metrics() with a mocked pysam.VariantFile that yields
    the given mock records, avoiding platform-specific htslib issues.
    """
    mock_vcf_ctx = MagicMock()
    mock_vcf_ctx.__enter__ = MagicMock(return_value=iter(records))
    mock_vcf_ctx.__exit__ = MagicMock(return_value=False)

    with patch("veritas.validate.pysam.VariantFile", return_value=mock_vcf_ctx):
        return calc_metrics("dummy.vcf.gz", **kwargs)


class TestCalcMetrics:
    """Tests for calc_metrics()."""

    def test_perfect_snv_match(self):
        result = _run_calc_metrics(
            [_mock_record(70, "A", "G", "TP", "SNV", ["."])]
        )
        snv = result["all"]["SNV"]
        assert snv["TP"] == 1
        assert snv["FP"] == 0
        assert snv["FN"] == 0
        assert snv["Precision"] == 1.0
        assert snv["Recall"] == 1.0
        assert snv["F1-Score"] == 1.0

    def test_false_positive_counted(self):
        result = _run_calc_metrics(
            [
                _mock_record(50, "C", "T", "FP", "SNV", ["."]),
                _mock_record(70, "A", "G", "TP", "SNV", ["."]),
            ]
        )
        snv = result["all"]["SNV"]
        assert snv["TP"] == 1
        assert snv["FP"] == 1
        assert pytest.approx(snv["Precision"], abs=1e-6) == 0.5

    def test_false_negative_counted(self):
        result = _run_calc_metrics(
            [
                _mock_record(70, "A", "G", "TP", "SNV", ["."]),
                _mock_record(80, "T", "C", "FN", "SNV", ["."]),
            ]
        )
        snv = result["all"]["SNV"]
        assert snv["FN"] == 1
        assert pytest.approx(snv["Recall"], abs=1e-6) == 0.5

    def test_indel_counted_separately(self):
        result = _run_calc_metrics(
            [
                _mock_record(70, "A", "G", "TP", "SNV", ["."]),
                _mock_record(80, "A", "ATG", "TP", "INDEL", ["."]),
            ]
        )
        assert result["all"]["SNV"]["TP"] == 1
        assert result["all"]["INDEL"]["TP"] == 1

    def test_primer_bed_counts_when_enabled(self):
        result = _run_calc_metrics(
            [
                _mock_record(50, "C", "T", "FP", "SNV", ["PRIMER"]),
                _mock_record(70, "A", "G", "TP", "SNV", ["."]),
            ],
            has_primer_bed=True,
        )
        assert result["counts_primer"] is not None
        assert result["counts_primer"]["SNV"]["FP"] == 1
        assert result["counts_primer"]["SNV"]["TP"] == 0

    def test_primer_bed_none_when_disabled(self):
        result = _run_calc_metrics(
            [_mock_record(70, "A", "G", "TP", "SNV", ["PRIMER"])],
            has_primer_bed=False,
        )
        assert result["counts_primer"] is None

    def test_mask_bed_counts_when_enabled(self):
        result = _run_calc_metrics(
            [_mock_record(60, "A", "G", "FP", "SNV", ["MASK"])],
            has_mask_bed=True,
        )
        assert result["counts_mask"]["SNV"]["FP"] == 1

    def test_low_cov_truth_counts_when_enabled(self):
        result = _run_calc_metrics(
            [_mock_record(90, "A", "G", "FN", "SNV", ["LOW_COV_TRUTH"])],
            has_low_cov_truth_bed=True,
        )
        assert result["counts_low_cov_truth"]["SNV"]["FN"] == 1

    def test_result_has_expected_keys(self):
        result = _run_calc_metrics(
            [_mock_record(70, "A", "G", "TP", "SNV", ["."])]
        )
        assert "all" in result
        assert "counts_primer" in result
        assert "counts_mask" in result
        assert "counts_low_cov_truth" in result
        assert "counts_low_cov_query" in result

    def test_all_zeros_empty_vcf(self):
        result = _run_calc_metrics([])
        assert result["all"]["SNV"]["TP"] == 0
        assert result["all"]["INDEL"]["TP"] == 0


class TestSaveMetricsTsv:
    """Tests for save_metrics_tsv()."""

    def _make_metrics(self, **kwargs):
        return _run_calc_metrics(
            [_mock_record(70, "A", "G", "TP", "SNV", ["."])],
            **kwargs,
        )

    def test_tsv_file_created(self, temp_dir):
        metrics = self._make_metrics()
        out = os.path.join(temp_dir, "metrics.tsv")
        save_metrics_tsv(metrics, out)
        assert os.path.exists(out)

    def test_tsv_has_all_region_row(self, temp_dir):
        metrics = self._make_metrics()
        out = os.path.join(temp_dir, "metrics.tsv")
        save_metrics_tsv(metrics, out)
        df = pd.read_csv(out, sep="\t")
        assert "ALL" in df["Region"].values

    def test_tsv_columns_present(self, temp_dir):
        metrics = self._make_metrics()
        out = os.path.join(temp_dir, "metrics.tsv")
        save_metrics_tsv(metrics, out)
        df = pd.read_csv(out, sep="\t")
        for col in [
            "Category",
            "Region",
            "TP",
            "FP",
            "FN",
            "Precision",
            "Recall",
            "F1-Score",
        ]:
            assert col in df.columns

    def test_tsv_primer_row_when_enabled(self, temp_dir):
        metrics = _run_calc_metrics(
            [
                _mock_record(50, "C", "T", "FP", "SNV", ["PRIMER"]),
                _mock_record(70, "A", "G", "TP", "SNV", ["."]),
            ],
            has_primer_bed=True,
        )
        out = os.path.join(temp_dir, "metrics_primer.tsv")
        save_metrics_tsv(metrics, out)
        df = pd.read_csv(out, sep="\t")
        assert "PRIMER BED" in df["Region"].values

    def test_tsv_no_primer_row_when_disabled(self, temp_dir):
        metrics = self._make_metrics()
        out = os.path.join(temp_dir, "no_primer.tsv")
        save_metrics_tsv(metrics, out)
        df = pd.read_csv(out, sep="\t")
        assert "PRIMER BED" not in df["Region"].values

    def test_snv_and_indel_rows_present(self, temp_dir):
        metrics = self._make_metrics()
        out = os.path.join(temp_dir, "si.tsv")
        save_metrics_tsv(metrics, out)
        df = pd.read_csv(out, sep="\t")
        all_rows = df[df["Region"] == "ALL"]
        categories = set(all_rows["Category"].values)
        assert "SNV" in categories
        assert "INDEL" in categories

    def test_perfect_precision_in_tsv(self, temp_dir):
        metrics = self._make_metrics()
        out = os.path.join(temp_dir, "perf.tsv")
        save_metrics_tsv(metrics, out)
        df = pd.read_csv(out, sep="\t")
        snv_all = df[(df["Category"] == "SNV") & (df["Region"] == "ALL")]
        assert snv_all.iloc[0]["Precision"] == pytest.approx(1.0)
