"""Veritas' metrics.tsv must satisfy PathoEQA's TSV contract (parser pathoeqa-tsv-v1-decimal28)."""
from veritas.validate import calc_metrics, save_metrics_tsv

from tests.contract.metrics_contract import evaluate

VCF = """##fileformat=VCFv4.2
##contig=<ID=chr1,length=1000>
##INFO=<ID=TAG,Number=1,Type=String,Description="x">
##INFO=<ID=TYPE,Number=1,Type=String,Description="x">
##INFO=<ID=BED,Number=.,Type=String,Description="x">
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO
chr1\t10\t.\tA\tG\t.\tPASS\tTAG=TP;TYPE=SNV;BED=PRIMER
chr1\t20\t.\tA\tG\t.\tPASS\tTAG=FP;TYPE=SNV
chr1\t30\t.\tAT\tA\t.\tPASS\tTAG=TP;TYPE=INDEL
chr1\t40\t.\tC\tT\t.\tPASS\tTAG=FN;TYPE=SNV;BED=PRIMER
"""


def test_metrics_tsv_with_primer_bed_is_accepted_by_pathoeqa(tmp_path):
    vcf = tmp_path / "truth_query.vcf"
    vcf.write_text(VCF)
    tsv = tmp_path / "metrics.tsv"

    save_metrics_tsv(calc_metrics(str(vcf), has_primer_bed=True), str(tsv))

    parsed, diagnostic = evaluate(tsv.read_bytes())
    assert diagnostic is None, diagnostic
    assert {(m.category, m.region) for m in parsed.metrics} == {
        ("SNV", "ALL"), ("INDEL", "ALL"), ("SNV", "PRIMER BED"), ("INDEL", "PRIMER BED")}
