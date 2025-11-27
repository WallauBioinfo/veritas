import click
import os
import pysam
import shutil
from veritas.datasets import (
    get_datasets_paths,
    get_medata_information,
    print_formatted_table,
    download_dataset,
    find_dataset_file,
    find_dataset_dir,
)
from veritas.validate import (
    process_gsalign_vcf,
    calc_metrics,
    compare,
    get_bed_intervals,
    save_metrics_tsv,
)
from veritas.tools import run_rtg_format, run_gsalign
from veritas.logger import logger, setup_logger

# Information about the datasets repository
OWNER = "WallauBioinfo"
REPO = "veritas_data"
BRANCH = "main"


@click.group()
def cli():
    "Tool to get benchmark datasets and compare VCF files"
    pass


@cli.command()
@click.option("-t", "--token", help="Github token (temporary)", envvar="GITHUB_TOKEN")
def list_datasets(
    token,
):
    """
    List datasets available in the veritas_data repository.
    """
    # temporary section
    if not token:
        raise ValueError(
            "Please set your GitHub token in the GITHUB_TOKEN environment variable."
        )
    HEADERS = {"Authorization": f"token {token}"}

    datasets_metadata_paths = get_datasets_paths(
        f"{OWNER}/{REPO}/git/trees/{BRANCH}?recursive=1", HEADERS
    )
    datasets_information = get_medata_information(
        f"{OWNER}/{REPO}/contents", datasets_metadata_paths, HEADERS
    )
    print_formatted_table(datasets_information)


@cli.command()
@click.option("-t", "--token", help="Github token (temporary)", envvar="GITHUB_TOKEN")
@click.option(
    "-d",
    "--dataset-name",
    help="Dataset name to download (e.g., veritas/sars-cov-2/SEARCH-8113)",
    required=True,
)
@click.option(
    "-o",
    "--output-dir",
    help="Directory to save the downloaded dataset",
    default="veritas_datasets",
)
def get_dataset(token, dataset_name, output_dir):
    """
    Download dataset available in the veritas_data repository.
    """
    # temporary section
    if not token:
        raise ValueError(
            "Please set your GitHub token in the GITHUB_TOKEN environment variable."
        )

    download_dataset(
        repo_path=f"{OWNER}/{REPO}",
        dataset_name=dataset_name,
        output_dir=output_dir,
        token=token,
    )


@cli.command()
@click.option(
    "-d",
    "--dataset-dir",
    help="Dataset directory containing truth VCF and reference files. Auto-discovers: *.vcf.gz (truth), reference.fa, primers.bed, mask.bed, low_cov.bed",
)
@click.option(
    "-t",
    "--truth-vcf",
    help="Truth VCF file path (gzipped). Optional if --dataset-dir is provided.",
)
@click.option(
    "-q",
    "--query-vcf",
    help="Query VCF file path (gzipped). Mutually exclusive with --query-fasta.",
)
@click.option(
    "-f",
    "--query-fasta",
    help="Query FASTA file path. Mutually exclusive with --query-vcf. Requires --reference.",
)
@click.option(
    "-r",
    "--reference",
    help="Reference FASTA file for GSAlign. Optional if --dataset-dir is provided (auto-discovers reference.fa).",
)
@click.option(
    "-o",
    "--output-dir",
    help="Directory to save the output files",
    default="veritas_output",
)
@click.option(
    "--rtg-reference",
    help="RTG reference SDF directory. Optional if --dataset-dir is provided (auto-discovers rtg_sdf).",
)
@click.option(
    "-p",
    "--primerd-bed",
    help="BED file with primer regions to annotate variants. Optional if --dataset-dir is provided.",
)
@click.option(
    "-m",
    "--mask-bed",
    help="BED file with regions to mask during comparison. Optional if --dataset-dir is provided.",
)
@click.option(
    "-lt",
    "--low-cov-truth-bed",
    help="BED file with low coverage regions in truth VCF. Optional if --dataset-dir is provided.",
)
@click.option(
    "-lq",
    "--low-cov-query-bed",
    help="BED file with low coverage regions in query VCF.",
)
def validate(
    dataset_dir,
    truth_vcf,
    query_vcf,
    query_fasta,
    reference,
    output_dir,
    rtg_reference,
    primerd_bed,
    mask_bed,
    low_cov_truth_bed,
    low_cov_query_bed,
):
    """
    Validate query variants against truth VCF.

    Can accept either a query VCF (--query-vcf) or a query FASTA (--query-fasta).
    When using --query-fasta, you must also provide --reference.
    The tool will run GSAlign to generate a VCF from the FASTA alignment.
    You can use --dataset-dir to automatically find dataset files, or specify them individually.
    """
    if dataset_dir:
        if not os.path.isdir(dataset_dir):
            raise click.UsageError(f"Dataset directory does not exist: {dataset_dir}")

        if not truth_vcf:
            truth_vcf = find_dataset_file(dataset_dir, "*.vcf.gz", "truth VCF")

        if not reference:
            reference = find_dataset_file(
                dataset_dir, "reference.fa", "reference FASTA"
            )

        if not rtg_reference:
            rtg_reference = find_dataset_dir(
                dataset_dir, "rtg_sdf", "RTG reference SDF"
            )

        if not primerd_bed:
            primerd_bed = find_dataset_file(dataset_dir, "primers.bed", "primers BED")

        if not mask_bed:
            mask_bed = find_dataset_file(dataset_dir, "mask.bed", "mask BED")

        if not low_cov_truth_bed:
            low_cov_truth_bed = find_dataset_file(
                dataset_dir, "low_cov.bed", "low coverage BED"
            )

    if not truth_vcf:
        raise click.UsageError(
            "Truth VCF is required. Provide either --truth-vcf or --dataset-dir with a VCF file."
        )

    if not os.path.exists(truth_vcf):
        raise click.UsageError(f"Truth VCF file does not exist: {truth_vcf}")

    if not rtg_reference:
        raise click.UsageError(
            "RTG reference is required. Provide either --rtg-reference or --dataset-dir with rtg_sdf directory."
        )

    if not os.path.isdir(rtg_reference):
        raise click.UsageError(
            f"RTG reference directory does not exist: {rtg_reference}"
        )

    if query_vcf and query_fasta:
        raise click.UsageError(
            "Cannot specify both --query-vcf and --query-fasta. Please use only one."
        )

    if not query_vcf and not query_fasta:
        raise click.UsageError("Must specify either --query-vcf or --query-fasta.")

    if query_fasta and not reference:
        raise click.UsageError(
            "--reference is required when using --query-fasta. Provide either --reference or --dataset-dir with reference.fa."
        )

    os.makedirs(output_dir, exist_ok=True)

    if query_fasta:
        logger.info(f"Running GSAlign to align {query_fasta} against {reference}...")

        gsalign_dir = os.path.join(output_dir, "gsalign")
        os.makedirs(gsalign_dir, exist_ok=True)

        gsalign_output_prefix = os.path.join(gsalign_dir, "sample")
        gsalign_vcf, gsalign_alignment = run_gsalign(
            reference, query_fasta, gsalign_output_prefix
        )

        final_alignment_path = os.path.join(gsalign_dir, "alignment.fa")
        os.rename(gsalign_alignment, final_alignment_path)

        logger.info("Processing GSAlign VCF output...")

        with pysam.VariantFile(truth_vcf) as truth:
            sample_name = truth.header.samples[0] if truth.header.samples else "SAMPLE"

        processed_vcf_temp = os.path.join(output_dir, "query.vcf")
        query_vcf = process_gsalign_vcf(gsalign_vcf, sample_name, processed_vcf_temp)

        logger.info(f"Query VCF saved to: {query_vcf}")

    primer_bed_intervals = get_bed_intervals(primerd_bed) if primerd_bed else None
    mask_bed_intervals = get_bed_intervals(mask_bed) if mask_bed else None
    low_cov_truth_bed_intervals = (
        get_bed_intervals(low_cov_truth_bed) if low_cov_truth_bed else None
    )
    low_cov_query_bed_intervals = (
        get_bed_intervals(low_cov_query_bed) if low_cov_query_bed else None
    )

    logger.info("Running VCF comparison...")
    compare(
        truth_vcf=truth_vcf,
        query_vcf=query_vcf,
        output_dir=output_dir,
        rtg_reference=rtg_reference,
        primerd_bed=primer_bed_intervals,
        mask_bed=mask_bed_intervals,
        low_cov_truth_bed=low_cov_truth_bed_intervals,
        low_cov_query_bed=low_cov_query_bed_intervals,
    )

    logger.info("Calculating metrics...")
    metrics = calc_metrics(
        f"{output_dir}/truth_query.vcf.gz",
        has_primer_bed=bool(primerd_bed),
        has_mask_bed=bool(mask_bed),
        has_low_cov_truth_bed=bool(low_cov_truth_bed),
        has_low_cov_query_bed=bool(low_cov_query_bed),
    )
    save_metrics_tsv(metrics, f"{output_dir}/metrics.tsv")

    logger.info(f"\nValidation complete! Results saved to: {output_dir}")
    logger.info(f"  - Truth/Query comparison VCF: {output_dir}/truth_query.vcf.gz")
    logger.info(f"  - Metrics: {output_dir}/metrics.tsv")
    if query_fasta:
        logger.info(f"  - GSAlign outputs: {output_dir}/gsalign/")


@cli.command()
@click.option(
    "-r",
    "--reference",
    type=click.Path(exists=True),
    required=True,
    help="Reference FASTA file",
)
@click.option(
    "-q",
    "--query",
    type=click.Path(exists=True),
    required=True,
    help="Query FASTA file",
)
@click.option(
    "-s", "--sample", help="Sample name. If not provided, uses query FASTA header."
)
@click.option(
    "-o",
    "--output",
    help="Output VCF file. If not provided, uses query filename prefix.",
)
def convert(reference, query, sample, output):
    """
    Convert FASTA to VCF using GSAlign.
    """
    import os

    if not sample:
        try:
            with open(query, "r") as f:
                first_line = f.readline().strip()
                if first_line.startswith(">"):
                    # Use the first word after > as sample name
                    sample = first_line[1:].split()[0]
                else:
                    sample = "SAMPLE"
                    logger.warning(
                        f"Could not determine sample name from {query}, using 'SAMPLE'"
                    )
        except Exception as e:
            logger.error(f"Error reading query file: {e}")
            raise click.Abort()

    if output:
        if output.endswith(".gz"):
            output_vcf_uncompressed = output[:-3]
        else:
            output_vcf_uncompressed = output
    else:
        query_basename = os.path.basename(query)
        prefix = os.path.splitext(query_basename)[0]
        output_vcf_uncompressed = f"{prefix}.vcf"

    output_dir = os.path.dirname(os.path.abspath(output_vcf_uncompressed))
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    logger.info(f"Running GSAlign to align {query} against {reference}...")

    gsalign_prefix = os.path.join(output_dir, f"gsalign_temp_{os.getpid()}")

    try:
        gsalign_vcf, gsalign_maf = run_gsalign(reference, query, gsalign_prefix)

        logger.info("Processing GSAlign VCF output...")
        final_vcf = process_gsalign_vcf(gsalign_vcf, sample, output_vcf_uncompressed)

        logger.info(f"Converted VCF saved to: {final_vcf}")

    finally:
        if "gsalign_vcf" in locals() and os.path.exists(gsalign_vcf):
            os.remove(gsalign_vcf)
        if "gsalign_maf" in locals() and os.path.exists(gsalign_maf):
            os.remove(gsalign_maf)


@cli.command(name="rtg-format")
@click.option(
    "-r",
    "--reference",
    type=click.Path(exists=True),
    required=True,
    help="Reference FASTA file",
)
@click.option("-o", "--output", required=True, help="Output directory name for RTG SDF")
def rtg_format(reference, output):
    """
    Format reference FASTA for RTG Tools.
    """
    try:
        run_rtg_format(reference, output)
    except Exception as e:
        logger.error(f"Error running RTG format: {e}")
        raise click.Abort()

if __name__ == "__main__":
    cli()
