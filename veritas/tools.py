import shutil
import subprocess
from veritas.logger import logger


def _require_tool(name: str) -> None:
    """Raise RuntimeError if *name* is not on PATH."""
    if shutil.which(name) is None:
        raise RuntimeError(
            f"Required tool '{name}' not found on PATH. "
            f"Install it via the conda environment: micromamba env create -f env.yaml"
        )


def run_rtg_format(reference, output):
    """
    Run RTG format to convert reference FASTA to SDF.

    Args:
        reference: Path to reference FASTA file
        output: Path to output SDF directory
    """
    _require_tool("rtg")
    cmd = ["rtg", "format", "-o", output, reference]
    subprocess.run(cmd, check=True)


def run_gsalign(reference, query_fasta, output_prefix):
    """
    Run GSAlign to align query_fasta against reference and generate VCF.

    Args:
        reference: Path to reference FASTA file
        query_fasta: Path to query FASTA file
        output_prefix: Prefix for output files (will generate .vcf and .maf)

    Returns:
        Tuple of (vcf_file, alignment_file) paths
    """
    _require_tool("GSAlign")
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

    result = subprocess.run(
        cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if result.stderr:
        for line in result.stderr.decode(errors="replace").splitlines():
            logger.debug(f"GSAlign: {line}")

    vcf_file = f"{output_prefix}.vcf"
    alignment_file = f"{output_prefix}.maf"

    return vcf_file, alignment_file
