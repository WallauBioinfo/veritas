import os
import pytest
import pysam
from veritas.validate import (
    compute_metrics,
    get_bed_intervals,
    in_bed_region,
    process_gsalign_vcf,
    fix_gsalign_vcf,
)


class TestComputeMetrics:
    """Tests for compute_metrics()."""

    def test_perfect_match(self):
        """All variants are true positives: precision=recall=F1=1.0"""
        precision, recall, f1 = compute_metrics(tp=10, fp=0, fn=0)
        assert precision == 1.0
        assert recall == 1.0
        assert f1 == 1.0

    def test_no_true_positives(self):
        """No TPs: precision and recall are both 0, F1 is 0."""
        precision, recall, f1 = compute_metrics(tp=0, fp=5, fn=5)
        assert precision == 0.0
        assert recall == 0.0
        assert f1 == 0.0

    def test_only_false_positives(self):
        """Only FPs: precision=0, recall=1 (0 FN), F1=0."""
        precision, recall, f1 = compute_metrics(tp=0, fp=5, fn=0)
        assert precision == 0.0
        # recall = TP / (TP + FN) = 0 / 0 → safe_div returns 0
        assert recall == 0.0
        assert f1 == 0.0

    def test_only_false_negatives(self):
        """Only FNs: recall=0, precision=1 (0 FP), but F1=0."""
        precision, recall, f1 = compute_metrics(tp=0, fp=0, fn=5)
        # precision = 0 / (0 + 0) → safe_div returns 0
        assert precision == 0.0
        assert recall == 0.0
        assert f1 == 0.0

    def test_mixed_example(self):
        """Typical mixed case: TP=8, FP=2, FN=2."""
        precision, recall, f1 = compute_metrics(tp=8, fp=2, fn=2)
        assert pytest.approx(precision, abs=1e-6) == 8 / 10  # 0.8
        assert pytest.approx(recall, abs=1e-6) == 8 / 10  # 0.8
        expected_f1 = 2 * 0.8 * 0.8 / (0.8 + 0.8)
        assert pytest.approx(f1, abs=1e-6) == expected_f1

    def test_all_zeros(self):
        """All zeros: no division errors, all metrics are 0."""
        precision, recall, f1 = compute_metrics(tp=0, fp=0, fn=0)
        assert precision == 0.0
        assert recall == 0.0
        assert f1 == 0.0

    def test_high_fp(self):
        """Many false positives drags precision down."""
        precision, recall, f1 = compute_metrics(tp=1, fp=99, fn=0)
        assert pytest.approx(precision, abs=1e-4) == 1 / 100
        assert recall == 1.0

    def test_return_types_are_floats(self):
        """All returned values should be floats."""
        result = compute_metrics(tp=5, fp=3, fn=2)
        for val in result:
            assert isinstance(val, float)


class TestGetBedIntervals:
    """Tests for get_bed_intervals()."""

    def test_single_interval(self, sample_primer_bed):
        """Single BED region parsed correctly as (start, end) tuple."""
        intervals = get_bed_intervals(sample_primer_bed)
        assert len(intervals) == 1
        assert intervals[0] == (45, 55)

    def test_multiple_intervals(self, temp_dir):
        """Multiple BED regions all parsed."""
        bed_path = os.path.join(temp_dir, "multi.bed")
        with open(bed_path, "w") as f:
            f.write("NC_045512.2\t10\t20\tregion1\n")
            f.write("NC_045512.2\t30\t40\tregion2\n")
            f.write("NC_045512.2\t50\t60\tregion3\n")
        intervals = get_bed_intervals(bed_path)
        assert len(intervals) == 3
        assert (10, 20) in intervals
        assert (30, 40) in intervals
        assert (50, 60) in intervals

    def test_returns_list_of_tuples(self, sample_primer_bed):
        """Return type should be a list of tuples."""
        intervals = get_bed_intervals(sample_primer_bed)
        assert isinstance(intervals, list)
        assert all(isinstance(iv, tuple) for iv in intervals)

    def test_empty_bed_file(self, temp_dir):
        """Empty BED file returns an empty list."""
        bed_path = os.path.join(temp_dir, "empty.bed")
        open(bed_path, "w").close()
        intervals = get_bed_intervals(bed_path)
        assert intervals == []

    def test_start_end_are_integers(self, sample_primer_bed):
        """Parsed start/end values must be integers."""
        intervals = get_bed_intervals(sample_primer_bed)
        start, end = intervals[0]
        assert isinstance(start, int)
        assert isinstance(end, int)


class TestInBedRegion:
    """Tests for in_bed_region()."""

    def _make_record(self, temp_dir, pos):
        """Helper: create a minimal VCF record at a given position."""
        vcf_path = os.path.join(temp_dir, f"tmp_{pos}.vcf")
        with open(vcf_path, "w") as f:
            f.write("##fileformat=VCFv4.2\n")
            f.write("##contig=<ID=NC_045512.2,length=100>\n")
            f.write('##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n')
            f.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1\n")
            f.write(f"NC_045512.2\t{pos}\t.\tA\tG\t100\tPASS\t.\tGT\t1\n")
        vcf_gz = vcf_path + ".gz"
        pysam.tabix_compress(vcf_path, vcf_gz, force=True)
        pysam.tabix_index(vcf_gz, preset="vcf", force=True)
        with pysam.VariantFile(vcf_gz) as vcf:
            for record in vcf:
                return record

    def test_position_inside_interval(self, temp_dir, sample_bed_intervals):
        """A position within a BED interval returns True."""
        record = self._make_record(temp_dir, pos=15)  # falls in (10, 20)
        assert in_bed_region(record, sample_bed_intervals) is True

    def test_position_outside_all_intervals(self, temp_dir, sample_bed_intervals):
        """A position outside all BED intervals returns False."""
        record = self._make_record(temp_dir, pos=70)  # outside (10,20),(30,40),(50,60)
        assert in_bed_region(record, sample_bed_intervals) is False

    def test_position_at_interval_boundary(self, temp_dir, sample_bed_intervals):
        """A position at the exact boundary of an interval returns True."""
        record = self._make_record(temp_dir, pos=10)  # boundary of (10, 20)
        assert in_bed_region(record, sample_bed_intervals) is True

    def test_empty_bed_list(self, temp_dir):
        """An empty interval list always returns False."""
        record = self._make_record(temp_dir, pos=15)
        assert in_bed_region(record, []) is False

    def test_none_bed_list(self, temp_dir):
        """A None bed argument returns False without error."""
        record = self._make_record(temp_dir, pos=15)
        assert in_bed_region(record, None) is False


class TestProcessGsalignVcf:
    """Tests for process_gsalign_vcf()."""

    def _write_gsalign_vcf(self, path):
        """Write a minimal GSAlign-style VCF (no FORMAT/sample columns)."""
        with open(path, "w") as f:
            f.write("##fileformat=VCFv4.2\n")
            f.write("##contig=<ID=NC_045512.2,length=100>\n")
            f.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
            f.write("NC_045512.2\t70\t.\tA\tG\t100\t.\t.\n")

    def test_output_file_created(self, temp_dir):
        """Output .vcf.gz is created after processing."""
        raw_vcf = os.path.join(temp_dir, "gsalign.vcf")
        out_vcf = os.path.join(temp_dir, "processed.vcf")
        self._write_gsalign_vcf(raw_vcf)
        result = process_gsalign_vcf(raw_vcf, "MySample", out_vcf)
        assert os.path.exists(result)
        assert result.endswith(".gz")

    def test_sample_name_in_header(self, temp_dir):
        """Sample name is injected into the VCF header."""
        raw_vcf = os.path.join(temp_dir, "gsalign.vcf")
        out_vcf = os.path.join(temp_dir, "processed.vcf")
        self._write_gsalign_vcf(raw_vcf)
        result = process_gsalign_vcf(raw_vcf, "MySample", out_vcf)
        with pysam.VariantFile(result) as vcf:
            samples = list(vcf.header.samples)
        assert "MySample" in samples

    def test_variants_have_pass_filter(self, temp_dir):
        """All variants in the output VCF have PASS filter."""
        import gzip

        raw_vcf = os.path.join(temp_dir, "gsalign.vcf")
        out_vcf = os.path.join(temp_dir, "processed.vcf")
        self._write_gsalign_vcf(raw_vcf)
        result = process_gsalign_vcf(raw_vcf, "MySample", out_vcf)
        with gzip.open(result, "rt") as fh:
            data_lines = [l for l in fh if not l.startswith("#") and l.strip()]
        for line in data_lines:
            fields = line.strip().split("\t")
            assert fields[6] == "PASS"

    def test_index_file_created(self, temp_dir):
        """A .tbi index is created alongside the output VCF."""
        raw_vcf = os.path.join(temp_dir, "gsalign.vcf")
        out_vcf = os.path.join(temp_dir, "processed.vcf")
        self._write_gsalign_vcf(raw_vcf)
        result = process_gsalign_vcf(raw_vcf, "MySample", out_vcf)
        assert os.path.exists(result + ".tbi")

    def test_gt_format_added(self, temp_dir):
        """GT FORMAT field and header are present in the output VCF."""
        import gzip

        raw_vcf = os.path.join(temp_dir, "gsalign.vcf")
        out_vcf = os.path.join(temp_dir, "processed.vcf")
        self._write_gsalign_vcf(raw_vcf)
        result = process_gsalign_vcf(raw_vcf, "MySample", out_vcf)
        with gzip.open(result, "rt") as fh:
            header_lines = [l for l in fh if l.startswith("##FORMAT")]
        assert any("ID=GT" in l for l in header_lines)


class TestFixGsalignVcf:
    """Tests for fix_gsalign_vcf()."""

    def test_filter_definition_added(self, temp_dir):
        """The * FILTER definition is injected into the header."""
        raw_vcf = os.path.join(temp_dir, "raw.vcf")
        fixed_vcf = os.path.join(temp_dir, "fixed.vcf")
        with open(raw_vcf, "w") as f:
            f.write("##fileformat=VCFv4.2\n")
            f.write("##contig=<ID=REF,length=100>\n")
            f.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
            f.write("REF\t10\t.\tA\tG\t.\t*\tEND=20\n")
        fix_gsalign_vcf(raw_vcf, fixed_vcf)
        with open(fixed_vcf) as f:
            content = f.read()
        assert "FILTER=<ID=*" in content

    def test_end_field_removed_from_info(self, temp_dir):
        """END= entries are stripped from the INFO column."""
        raw_vcf = os.path.join(temp_dir, "raw.vcf")
        fixed_vcf = os.path.join(temp_dir, "fixed.vcf")
        with open(raw_vcf, "w") as f:
            f.write("##fileformat=VCFv4.2\n")
            f.write("##contig=<ID=REF,length=100>\n")
            f.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
            f.write("REF\t10\t.\tA\tG\t.\t*\tEND=20;DP=30\n")
        fix_gsalign_vcf(raw_vcf, fixed_vcf)
        with open(fixed_vcf) as f:
            lines = [l for l in f if not l.startswith("#")]
        assert all("END=" not in l for l in lines)

    def test_non_end_info_fields_preserved(self, temp_dir):
        """Non-END INFO fields are kept intact after fixing."""
        raw_vcf = os.path.join(temp_dir, "raw.vcf")
        fixed_vcf = os.path.join(temp_dir, "fixed.vcf")
        with open(raw_vcf, "w") as f:
            f.write("##fileformat=VCFv4.2\n")
            f.write("##contig=<ID=REF,length=100>\n")
            f.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n")
            f.write("REF\t10\t.\tA\tG\t.\t*\tEND=20;DP=30\n")
        fix_gsalign_vcf(raw_vcf, fixed_vcf)
        with open(fixed_vcf) as f:
            data_lines = [l for l in f if not l.startswith("#")]
        assert any("DP=30" in l for l in data_lines)
