import subprocess


def run_rtg_format(reference, output):
    """
    Run RTG format to convert reference FASTA to SDF.

    Args:
        reference: Path to reference FASTA file
        output: Path to output SDF directory
    """
    cmd = ["rtg", "format", "-o", output, reference]
    subprocess.run(cmd, check=True)


def run_gsalign(reference, query_fasta, output_prefix):
    """
    Run GSAlign to align query_fasta against reference and generate VCF.

    Args:
        reference: Path to reference FASTA file
        query_fasta: Path to query FASTA file
        output_prefix: Prefix for output files (will generate .vcf and .fa)

    Returns:
        Tuple of (vcf_file, alignment_file) paths
    """
    cmd = [
        "GSAlign",
        "-r",
        reference,
        "-q",
        query_fasta,
        "-o",
        output_prefix,
        "-fmt",
        "1",
        "-sen",
    ]

    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    vcf_file = f"{output_prefix}.vcf"
    alignment_file = f"{output_prefix}.maf"

    return vcf_file, alignment_file
