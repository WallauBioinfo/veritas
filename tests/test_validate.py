"""
Tests for veritas.validate module.
"""

import pytest
import os
from veritas.validate import (
    compute_metrics,
    get_bed_intervals,
    process_gsalign_vcf,
)


class TestComputeMetrics:
    """Tests for compute_metrics function."""

    def test_perfect_match(self):
        """Test metrics with perfect precision and recall."""
        tp, fp, fn = 10, 0, 0
        precision, recall, f1 = compute_metrics(tp, fp, fn)

        assert precision == 1.0
        assert recall == 1.0
        assert f1 == 1.0

    def test_with_false_positives(self):
        """Test metrics with false positives."""
        tp, fp, fn = 10, 5, 0
        precision, recall, f1 = compute_metrics(tp, fp, fn)

        assert precision == pytest.approx(10 / 15)  # 0.667
        assert recall == 1.0
        assert f1 == pytest.approx(0.8)

    def test_with_false_negatives(self):
        """Test metrics with false negatives."""
        tp, fp, fn = 10, 0, 2
        precision, recall, f1 = compute_metrics(tp, fp, fn)

        assert precision == 1.0
        assert recall == pytest.approx(10 / 12)  # 0.833
        assert f1 == pytest.approx(0.909, abs=0.01)

    def test_all_zeros(self):
        """Test metrics when all counts are zero."""
        tp, fp, fn = 0, 0, 0
        precision, recall, f1 = compute_metrics(tp, fp, fn)

        assert precision == 0.0
        assert recall == 0.0
        assert f1 == 0.0

    def test_zero_tp(self):
        """Test metrics when TP is zero."""
        tp, fp, fn = 0, 5, 3
        precision, recall, f1 = compute_metrics(tp, fp, fn)

        assert precision == 0.0
        assert recall == 0.0
        assert f1 == 0.0


class TestGetBedIntervals:
    """Tests for get_bed_intervals function."""

    def test_simple_bed_file(self, sample_primer_bed):
        """Test reading a simple BED file."""
        intervals = get_bed_intervals(sample_primer_bed)

        assert len(intervals) == 1
        assert intervals[0] == (45, 55)

    def test_multiple_intervals(self, temp_dir):
        """Test reading BED file with multiple intervals."""
        bed_path = os.path.join(temp_dir, "multi.bed")
        with open(bed_path, "w") as f:
            f.write("chr1\t100\t200\tregion1\n")
            f.write("chr1\t300\t400\tregion2\n")
            f.write("chr2\t500\t600\tregion3\n")

        intervals = get_bed_intervals(bed_path)

        assert len(intervals) == 3
        assert (100, 200) in intervals
        assert (300, 400) in intervals
        assert (500, 600) in intervals

    def test_bed_with_extra_columns(self, temp_dir):
        """Test BED file with more than 3 columns."""
        bed_path = os.path.join(temp_dir, "extra.bed")
        with open(bed_path, "w") as f:
            f.write("chr1\t100\t200\tname\t100\t+\n")

        intervals = get_bed_intervals(bed_path)

        assert len(intervals) == 1
        assert intervals[0] == (100, 200)


class TestProcessGSAlignVCF:
    """Tests for process_gsalign_vcf function."""

    def test_process_gsalign_vcf(self, temp_dir):
        """Test processing GSAlign VCF output."""
        # Create a mock GSAlign VCF
        gsalign_vcf = os.path.join(temp_dir, "gsalign.vcf")
        with open(gsalign_vcf, "w") as f:
            f.write("##fileformat=VCFv4.2\n")
            f.write("##contig=<ID=NC_045512.2,length=100>\n")
            f.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
            f.write("NC_045512.2\t50\t.\tA\tG\t100\t*\tEND=50\n")

        output_vcf = os.path.join(temp_dir, "processed.vcf")
        sample_name = "TestSample"

        result = process_gsalign_vcf(gsalign_vcf, sample_name, output_vcf)

        # Check that output was created
        assert os.path.exists(result)
        assert result.endswith(".gz")

        # Verify the content
        import pysam

        with pysam.VariantFile(result) as vcf:
            samples = list(vcf.header.samples)
            assert len(samples) == 1
            assert samples[0] == sample_name

            # Check that FILTER is PASS
            records = list(vcf)
            assert len(records) == 1
            assert records[0].filter.keys() == ["PASS"]

            # Check that FORMAT and genotype are present
            assert "GT" in records[0].format
            assert records[0].samples[sample_name]["GT"] == (1,)

    def test_process_multiple_variants(self, temp_dir):
        """Test processing VCF with multiple variants."""
        gsalign_vcf = os.path.join(temp_dir, "gsalign_multi.vcf")
        with open(gsalign_vcf, "w") as f:
            f.write("##fileformat=VCFv4.2\n")
            f.write("##contig=<ID=NC_045512.2,length=100>\n")
            f.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
            f.write("NC_045512.2\t10\t.\tA\tG\t100\t*\t.\n")
            f.write("NC_045512.2\t20\t.\tT\tC\t100\t*\t.\n")
            f.write("NC_045512.2\t30\t.\tATG\tA\t100\t*\t.\n")  # Deletion

        output_vcf = os.path.join(temp_dir, "processed_multi.vcf")
        sample_name = "MultiSample"

        result = process_gsalign_vcf(gsalign_vcf, sample_name, output_vcf)

        import pysam

        with pysam.VariantFile(result) as vcf:
            records = list(vcf)
            assert len(records) == 3

            # All should have PASS filter
            for record in records:
                assert "PASS" in record.filter.keys()
                assert record.samples[sample_name]["GT"] == (1,)


class TestInBedRegion:
    """Tests for checking if position is in BED region."""

    def test_position_in_region(self, sample_bed_intervals):
        """Test position within an interval."""
        from veritas.validate import in_bed_region

        # Mock record with position 15 (in first interval 10-20)
        class MockRecord:
            pos = 15

        assert in_bed_region(MockRecord(), sample_bed_intervals) is True

    def test_position_not_in_region(self, sample_bed_intervals):
        """Test position outside all intervals."""
        from veritas.validate import in_bed_region

        class MockRecord:
            pos = 25  # Between intervals

        assert in_bed_region(MockRecord(), sample_bed_intervals) is False

    def test_position_at_boundary(self, sample_bed_intervals):
        """Test position at interval boundaries."""
        from veritas.validate import in_bed_region

        class MockRecordStart:
            pos = 10  # Start of first interval

        class MockRecordEnd:
            pos = 20  # End of first interval

        assert in_bed_region(MockRecordStart(), sample_bed_intervals) is True
        assert in_bed_region(MockRecordEnd(), sample_bed_intervals) is True

    def test_empty_bed(self):
        """Test with no BED intervals."""
        from veritas.validate import in_bed_region

        class MockRecord:
            pos = 50

        assert in_bed_region(MockRecord(), None) is False
        assert in_bed_region(MockRecord(), []) is False
