import os
import pytest
import pysam
import pandas as pd
from veritas.validate import calc_metrics, save_metrics_tsv


def _write_annotated_vcf(path, records):
    """
    Write a minimal annotated VCF with TAG, TYPE, BED INFO fields.

    Parameters
    ----------
    path : str
        Output path (uncompressed).
    records : list of dict
        Each dict must have keys: pos, ref, alt, tag, var_type, bed.
        ``bed`` is a list of strings, e.g. ['PRIMER'] or ['.'].
    """
    with open(path, "w") as f:
        f.write("##fileformat=VCFv4.2\n")
        f.write("##contig=<ID=NC_045512.2,length=300>\n")
        f.write('##FILTER=<ID=PASS,Description="All filters passed">\n')
        f.write('##INFO=<ID=TAG,Number=1,Type=String,Description="Variant class">\n')
        f.write('##INFO=<ID=TYPE,Number=1,Type=String,Description="Variant type">\n')
        f.write('##INFO=<ID=BED,Number=.,Type=String,Description="BED regions">\n')
        f.write('##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n')
        f.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tMySample\n")
        for r in records:
            bed_str = ",".join(r["bed"])
            info = f"TAG={r['tag']};TYPE={r['var_type']};BED={bed_str}"
            f.write(
                f"NC_045512.2\t{r['pos']}\t.\t{r['ref']}\t{r['alt']}\t100\tPASS\t{info}\tGT\t1\n"
            )

    vcf_gz = path + ".gz"
    pysam.tabix_compress(path, vcf_gz, force=True)
    pysam.tabix_index(vcf_gz, preset="vcf", force=True)
    return vcf_gz


class TestCalcMetrics:
    """Tests for calc_metrics()."""

    def test_perfect_snv_match(self, temp_dir):
        vcf = _write_annotated_vcf(
            os.path.join(temp_dir, "perfect.vcf"),
            [
                {
                    "pos": 70,
                    "ref": "A",
                    "alt": "G",
                    "tag": "TP",
                    "var_type": "SNV",
                    "bed": ["."],
                }
            ],
        )
        result = calc_metrics(vcf)
        snv = result["all"]["SNV"]
        assert snv["TP"] == 1
        assert snv["FP"] == 0
        assert snv["FN"] == 0
        assert snv["Precision"] == 1.0
        assert snv["Recall"] == 1.0
        assert snv["F1-Score"] == 1.0

    def test_false_positive_counted(self, temp_dir):
        vcf = _write_annotated_vcf(
            os.path.join(temp_dir, "fp.vcf"),
            [
                {
                    "pos": 50,
                    "ref": "C",
                    "alt": "T",
                    "tag": "FP",
                    "var_type": "SNV",
                    "bed": ["."],
                },
                {
                    "pos": 70,
                    "ref": "A",
                    "alt": "G",
                    "tag": "TP",
                    "var_type": "SNV",
                    "bed": ["."],
                },
            ],
        )
        result = calc_metrics(vcf)
        snv = result["all"]["SNV"]
        assert snv["TP"] == 1
        assert snv["FP"] == 1
        assert pytest.approx(snv["Precision"], abs=1e-6) == 0.5

    def test_false_negative_counted(self, temp_dir):
        vcf = _write_annotated_vcf(
            os.path.join(temp_dir, "fn.vcf"),
            [
                {
                    "pos": 70,
                    "ref": "A",
                    "alt": "G",
                    "tag": "TP",
                    "var_type": "SNV",
                    "bed": ["."],
                },
                {
                    "pos": 80,
                    "ref": "T",
                    "alt": "C",
                    "tag": "FN",
                    "var_type": "SNV",
                    "bed": ["."],
                },
            ],
        )
        result = calc_metrics(vcf)
        snv = result["all"]["SNV"]
        assert snv["FN"] == 1
        assert pytest.approx(snv["Recall"], abs=1e-6) == 0.5

    def test_indel_counted_separately(self, temp_dir):
        vcf = _write_annotated_vcf(
            os.path.join(temp_dir, "indel.vcf"),
            [
                {
                    "pos": 70,
                    "ref": "A",
                    "alt": "G",
                    "tag": "TP",
                    "var_type": "SNV",
                    "bed": ["."],
                },
                {
                    "pos": 80,
                    "ref": "A",
                    "alt": "ATG",
                    "tag": "TP",
                    "var_type": "INDEL",
                    "bed": ["."],
                },
            ],
        )
        result = calc_metrics(vcf)
        assert result["all"]["SNV"]["TP"] == 1
        assert result["all"]["INDEL"]["TP"] == 1

    def test_primer_bed_counts_when_enabled(self, temp_dir):
        vcf = _write_annotated_vcf(
            os.path.join(temp_dir, "primer.vcf"),
            [
                {
                    "pos": 50,
                    "ref": "C",
                    "alt": "T",
                    "tag": "FP",
                    "var_type": "SNV",
                    "bed": ["PRIMER"],
                },
                {
                    "pos": 70,
                    "ref": "A",
                    "alt": "G",
                    "tag": "TP",
                    "var_type": "SNV",
                    "bed": ["."],
                },
            ],
        )
        result = calc_metrics(vcf, has_primer_bed=True)
        assert result["counts_primer"] is not None
        assert result["counts_primer"]["SNV"]["FP"] == 1
        assert result["counts_primer"]["SNV"]["TP"] == 0

    def test_primer_bed_none_when_disabled(self, temp_dir):
        vcf = _write_annotated_vcf(
            os.path.join(temp_dir, "no_primer.vcf"),
            [
                {
                    "pos": 70,
                    "ref": "A",
                    "alt": "G",
                    "tag": "TP",
                    "var_type": "SNV",
                    "bed": ["PRIMER"],
                }
            ],
        )
        result = calc_metrics(vcf, has_primer_bed=False)
        assert result["counts_primer"] is None

    def test_mask_bed_counts_when_enabled(self, temp_dir):
        vcf = _write_annotated_vcf(
            os.path.join(temp_dir, "mask.vcf"),
            [
                {
                    "pos": 60,
                    "ref": "A",
                    "alt": "G",
                    "tag": "FP",
                    "var_type": "SNV",
                    "bed": ["MASK"],
                },
            ],
        )
        result = calc_metrics(vcf, has_mask_bed=True)
        assert result["counts_mask"]["SNV"]["FP"] == 1

    def test_low_cov_truth_counts_when_enabled(self, temp_dir):
        vcf = _write_annotated_vcf(
            os.path.join(temp_dir, "lowcov.vcf"),
            [
                {
                    "pos": 90,
                    "ref": "A",
                    "alt": "G",
                    "tag": "FN",
                    "var_type": "SNV",
                    "bed": ["LOW_COV_TRUTH"],
                },
            ],
        )
        result = calc_metrics(vcf, has_low_cov_truth_bed=True)
        assert result["counts_low_cov_truth"]["SNV"]["FN"] == 1

    def test_result_has_expected_keys(self, temp_dir):
        vcf = _write_annotated_vcf(
            os.path.join(temp_dir, "keys.vcf"),
            [
                {
                    "pos": 70,
                    "ref": "A",
                    "alt": "G",
                    "tag": "TP",
                    "var_type": "SNV",
                    "bed": ["."],
                }
            ],
        )
        result = calc_metrics(vcf)
        assert "all" in result
        assert "counts_primer" in result
        assert "counts_mask" in result
        assert "counts_low_cov_truth" in result
        assert "counts_low_cov_query" in result

    def test_all_zeros_empty_vcf(self, temp_dir):
        vcf = _write_annotated_vcf(
            os.path.join(temp_dir, "empty.vcf"),
            [],
        )
        result = calc_metrics(vcf)
        assert result["all"]["SNV"]["TP"] == 0
        assert result["all"]["INDEL"]["TP"] == 0


class TestSaveMetricsTsv:
    """Tests for save_metrics_tsv()."""

    def _make_metrics(self, temp_dir, **kwargs):
        vcf = _write_annotated_vcf(
            os.path.join(temp_dir, "m.vcf"),
            [
                {
                    "pos": 70,
                    "ref": "A",
                    "alt": "G",
                    "tag": "TP",
                    "var_type": "SNV",
                    "bed": ["."],
                }
            ],
        )
        return calc_metrics(vcf, **kwargs)

    def test_tsv_file_created(self, temp_dir):
        metrics = self._make_metrics(temp_dir)
        out = os.path.join(temp_dir, "metrics.tsv")
        save_metrics_tsv(metrics, out)
        assert os.path.exists(out)

    def test_tsv_has_all_region_row(self, temp_dir):
        metrics = self._make_metrics(temp_dir)
        out = os.path.join(temp_dir, "metrics.tsv")
        save_metrics_tsv(metrics, out)
        df = pd.read_csv(out, sep="\t")
        assert "ALL" in df["Region"].values

    def test_tsv_columns_present(self, temp_dir):
        metrics = self._make_metrics(temp_dir)
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
        vcf = _write_annotated_vcf(
            os.path.join(temp_dir, "pm.vcf"),
            [
                {
                    "pos": 50,
                    "ref": "C",
                    "alt": "T",
                    "tag": "FP",
                    "var_type": "SNV",
                    "bed": ["PRIMER"],
                },
                {
                    "pos": 70,
                    "ref": "A",
                    "alt": "G",
                    "tag": "TP",
                    "var_type": "SNV",
                    "bed": ["."],
                },
            ],
        )
        metrics = calc_metrics(vcf, has_primer_bed=True)
        out = os.path.join(temp_dir, "metrics_primer.tsv")
        save_metrics_tsv(metrics, out)
        df = pd.read_csv(out, sep="\t")
        assert "PRIMER BED" in df["Region"].values

    def test_tsv_no_primer_row_when_disabled(self, temp_dir):
        metrics = self._make_metrics(temp_dir)
        out = os.path.join(temp_dir, "no_primer.tsv")
        save_metrics_tsv(metrics, out)
        df = pd.read_csv(out, sep="\t")
        assert "PRIMER BED" not in df["Region"].values

    def test_snv_and_indel_rows_present(self, temp_dir):
        metrics = self._make_metrics(temp_dir)
        out = os.path.join(temp_dir, "si.tsv")
        save_metrics_tsv(metrics, out)
        df = pd.read_csv(out, sep="\t")
        all_rows = df[df["Region"] == "ALL"]
        categories = set(all_rows["Category"].values)
        assert "SNV" in categories
        assert "INDEL" in categories

    def test_perfect_precision_in_tsv(self, temp_dir):
        metrics = self._make_metrics(temp_dir)
        out = os.path.join(temp_dir, "perf.tsv")
        save_metrics_tsv(metrics, out)
        df = pd.read_csv(out, sep="\t")
        snv_all = df[(df["Category"] == "SNV") & (df["Region"] == "ALL")]
        assert snv_all.iloc[0]["Precision"] == pytest.approx(1.0)
