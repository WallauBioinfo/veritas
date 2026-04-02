import pytest
import tempfile
import os
import pysam
from pathlib import Path


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def sample_reference_fasta(temp_dir):
    """Create a simple reference FASTA file."""
    ref_path = os.path.join(temp_dir, "reference.fa")
    with open(ref_path, "w") as f:
        f.write(">NC_045512.2\n")
        f.write(
            "ATTAAAGGTTTATACCTTCCCAGGTAACAAACCAACCAACTTTCGATCTCTTGTAGATCTGTTCTCTAAA\n"
        )
    return ref_path


@pytest.fixture
def sample_query_fasta(temp_dir):
    """Create a query FASTA with a SNP at position 70 (A->G)."""
    query_path = os.path.join(temp_dir, "query.fa")
    with open(query_path, "w") as f:
        f.write(">MySample\n")
        # Changed A to G at position 70 (last position)
        f.write(
            "ATTAAAGGTTTATACCTTCCCAGGTAACAAACCAACCAACTTTCGATCTCTTGTAGATCTGTTCTCTAAG\n"
        )
    return query_path


@pytest.fixture
def sample_truth_vcf(temp_dir):
    """Create a truth VCF file."""
    vcf_path = os.path.join(temp_dir, "truth.vcf")
    with open(vcf_path, "w") as f:
        f.write("##fileformat=VCFv4.2\n")
        f.write("##contig=<ID=NC_045512.2,length=71>\n")
        f.write('##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n')
        f.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tMySample\n")
        f.write("NC_045512.2\t70\t.\tA\tG\t100\tPASS\t.\tGT\t1\n")

    vcf_gz = vcf_path + ".gz"
    pysam.tabix_compress(vcf_path, vcf_gz, force=True)
    pysam.tabix_index(vcf_gz, preset="vcf", force=True)

    return vcf_gz


@pytest.fixture
def sample_query_vcf(temp_dir):
    """Create a query VCF file with TP, FP, and FN variants."""
    vcf_path = os.path.join(temp_dir, "query.vcf")
    with open(vcf_path, "w") as f:
        f.write("##fileformat=VCFv4.2\n")
        f.write("##contig=<ID=NC_045512.2,length=71>\n")
        f.write('##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n')
        f.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tMySample\n")
        f.write("NC_045512.2\t50\t.\tC\tT\t100\tPASS\t.\tGT\t1\n")
        f.write("NC_045512.2\t70\t.\tA\tG\t100\tPASS\t.\tGT\t1\n")

    vcf_gz = vcf_path + ".gz"
    pysam.tabix_compress(vcf_path, vcf_gz, force=True)
    pysam.tabix_index(vcf_gz, preset="vcf", force=True)

    return vcf_gz


@pytest.fixture
def sample_primer_bed(temp_dir):
    """Create a BED file with primer regions."""
    bed_path = os.path.join(temp_dir, "primers.bed")
    with open(bed_path, "w") as f:
        f.write("NC_045512.2\t45\t55\tprimer1\n")
    return bed_path


@pytest.fixture
def sample_mask_bed(temp_dir):
    """Create a BED file with mask regions."""
    bed_path = os.path.join(temp_dir, "mask.bed")
    with open(bed_path, "w") as f:
        f.write("NC_045512.2\t60\t65\tmask_region\n")
    return bed_path


@pytest.fixture
def sample_bed_intervals():
    """Sample BED intervals as list of tuples."""
    return [(10, 20), (30, 40), (50, 60)]


@pytest.fixture
def sample_yaml_metadata():
    """Sample metadata dictionary."""
    return {
        "dataset": "veritas/sars-cov-2/test-dataset",
        "dataset_version": "1.0",
        "pathogen": "SARS-CoV-2",
        "sequencing_technology": "Illumina MiSeq",
        "layout": "PAIRED",
        "dataset_type": "artificial",
        "strategy": "AMPLICON",
        "read_length": "250",
        "sample": "test_sample",
        "sra": None,
        "zenodo_id": "12345678",
    }
