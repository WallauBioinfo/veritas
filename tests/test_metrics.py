"""
Tests for metrics calculation and VCF processing.
"""

import pytest
import os
import pysam
from veritas.validate import calc_metrics, save_metrics_tsv
import pandas as pd


class TestCalcMetrics:
    """Tests for calc_metrics function."""

    def test_calc_metrics_basic(self, temp_dir):
        """Test basic metrics calculation from a VCF file."""
        # Create a test VCF with TAG and TYPE annotations
        vcf_path = os.path.join(temp_dir, "test_metrics.vcf")

        with open(vcf_path, "w") as f:
            f.write("##fileformat=VCFv4.2\n")
            f.write("##contig=<ID=chr1,length=1000>\n")
            f.write('##INFO=<ID=TAG,Number=1,Type=String,Description="Variant tag">\n')
            f.write(
                '##INFO=<ID=TYPE,Number=1,Type=String,Description="Variant type">\n'
            )
            f.write('##INFO=<ID=BED,Number=1,Type=String,Description="BED regions">\n')
            f.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
            # TP SNVs
            f.write("chr1\t100\t.\tA\tG\t100\tPASS\tTAG=TP;TYPE=SNV\n")
            f.write("chr1\t200\t.\tC\tT\t100\tPASS\tTAG=TP;TYPE=SNV\n")
            # FP SNV
            f.write("chr1\t300\t.\tG\tA\t100\tPASS\tTAG=FP;TYPE=SNV\n")
            # FN SNV
            f.write("chr1\t400\t.\tT\tC\t100\tPASS\tTAG=FN;TYPE=SNV\n")
            # TP INDEL
            f.write("chr1\t500\t.\tATG\tA\t100\tPASS\tTAG=TP;TYPE=INDEL\n")
            # FP INDEL
            f.write("chr1\t600\t.\tA\tATT\t100\tPASS\tTAG=FP;TYPE=INDEL\n")

        # Compress and index
        vcf_gz = vcf_path + ".gz"
        pysam.tabix_compress(vcf_path, vcf_gz, force=True)
        pysam.tabix_index(vcf_gz, preset="vcf", force=True)

        # Calculate metrics
        metrics = calc_metrics(vcf_gz)

        # Check SNV metrics
        assert metrics["all"]["SNV"]["TP"] == 2
        assert metrics["all"]["SNV"]["FP"] == 1
        assert metrics["all"]["SNV"]["FN"] == 1

        # Check INDEL metrics
        assert metrics["all"]["INDEL"]["TP"] == 1
        assert metrics["all"]["INDEL"]["FP"] == 1
        assert metrics["all"]["INDEL"]["FN"] == 0

        # Check calculated values
        assert metrics["all"]["SNV"]["Precision"] == pytest.approx(2 / 3)
        assert metrics["all"]["SNV"]["Recall"] == pytest.approx(2 / 3)

    def test_calc_metrics_with_bed_regions(self, temp_dir):
        """Test metrics calculation with BED annotations."""
        vcf_path = os.path.join(temp_dir, "test_bed_metrics.vcf")

        with open(vcf_path, "w") as f:
            f.write("##fileformat=VCFv4.2\n")
            f.write("##contig=<ID=chr1,length=1000>\n")
            f.write('##INFO=<ID=TAG,Number=1,Type=String,Description="Variant tag">\n')
            f.write(
                '##INFO=<ID=TYPE,Number=1,Type=String,Description="Variant type">\n'
            )
            f.write('##INFO=<ID=BED,Number=1,Type=String,Description="BED regions">\n')
            f.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
            # Variant in PRIMER region
            f.write("chr1\t100\t.\tA\tG\t100\tPASS\tTAG=TP;TYPE=SNV;BED=PRIMER\n")
            # Variant in MASK region
            f.write("chr1\t200\t.\tC\tT\t100\tPASS\tTAG=FP;TYPE=SNV;BED=MASK\n")
            # Variant in LOW_COV_TRUTH region
            f.write(
                "chr1\t300\t.\tG\tA\t100\tPASS\tTAG=FN;TYPE=SNV;BED=LOW_COV_TRUTH\n"
            )

        vcf_gz = vcf_path + ".gz"
        pysam.tabix_compress(vcf_path, vcf_gz, force=True)
        pysam.tabix_index(vcf_gz, preset="vcf", force=True)

        # Calculate metrics with BED flags
        metrics = calc_metrics(
            vcf_gz, has_primer_bed=True, has_mask_bed=True, has_low_cov_truth_bed=True
        )

        # Check BED-specific counts
        assert metrics["counts_primer"]["SNV"]["TP"] == 1
        assert metrics["counts_mask"]["SNV"]["FP"] == 1
        assert metrics["counts_low_cov_truth"]["SNV"]["FN"] == 1


class TestSaveMetricsTSV:
    """Tests for save_metrics_tsv function."""

    def test_save_basic_metrics(self, temp_dir):
        """Test saving metrics to TSV file."""
        metrics = {
            "all": {
                "SNV": {
                    "TP": 10,
                    "FP": 2,
                    "FN": 1,
                    "Precision": 0.833,
                    "Recall": 0.909,
                    "F1-Score": 0.870,
                },
                "INDEL": {
                    "TP": 5,
                    "FP": 1,
                    "FN": 0,
                    "Precision": 0.833,
                    "Recall": 1.0,
                    "F1-Score": 0.909,
                },
            }
        }

        output_file = os.path.join(temp_dir, "metrics.tsv")
        save_metrics_tsv(metrics, output_file)

        # Check file was created
        assert os.path.exists(output_file)

        # Read and verify content
        df = pd.read_csv(output_file, sep="\t")

        assert len(df) == 2  # SNV and INDEL for ALL region
        assert "Category" in df.columns
        assert "Region" in df.columns
        assert "TP" in df.columns
        assert "FP" in df.columns
        assert "FN" in df.columns
        assert "Precision" in df.columns
        assert "Recall" in df.columns
        assert "F1-Score" in df.columns

        # Check SNV row
        snv_row = df[df["Category"] == "SNV"].iloc[0]
        assert snv_row["Region"] == "ALL"
        assert snv_row["TP"] == 10
        assert snv_row["FP"] == 2
        assert snv_row["FN"] == 1

    def test_save_metrics_with_bed_regions(self, temp_dir):
        """Test saving metrics with BED region counts."""
        metrics = {
            "all": {
                "SNV": {
                    "TP": 10,
                    "FP": 2,
                    "FN": 1,
                    "Precision": 0.833,
                    "Recall": 0.909,
                    "F1-Score": 0.870,
                }
            },
            "counts_primer": {"SNV": {"TP": 3, "FP": 1, "FN": 0}},
            "counts_mask": {"SNV": {"TP": 2, "FP": 0, "FN": 1}},
        }

        output_file = os.path.join(temp_dir, "metrics_bed.tsv")
        save_metrics_tsv(metrics, output_file)

        df = pd.read_csv(output_file, sep="\t")

        # Should have rows for ALL, PRIMER BED, and MASK BED
        assert len(df) == 3

        regions = df["Region"].tolist()
        assert "ALL" in regions
        assert "PRIMER BED" in regions
        assert "MASK BED" in regions

        # Check PRIMER BED row
        primer_row = df[df["Region"] == "PRIMER BED"].iloc[0]
        assert primer_row["TP"] == 3
        assert primer_row["FP"] == 1
        assert primer_row["FN"] == 0
