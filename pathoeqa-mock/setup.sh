#!/usr/bin/env bash
# Downloads SEARCH-8113 and creates a self-signed cert for localhost. Run once.
set -euo pipefail
cd "$(dirname "$0")"
RAW=https://raw.githubusercontent.com/WallauBioinfo/veritas_data/main/datasets/veritas/sars-cov-2/SEARCH-8113
mkdir -p data/rtg_sdf certs

for f in SEARCH-8113.vcf.gz SEARCH-8113.vcf.gz.tbi reference.fa primers.bed low_cov.bed; do
  curl -fsSL "$RAW/$f" -o "data/$f"
done
for f in done format.log mainIndex nameIndex0 namedata0 namepointer0 progress \
         seqdata0 seqpointer0 sequenceIndex0 summary.txt; do
  curl -fsSL "$RAW/rtg_sdf/$f" -o "data/rtg_sdf/$f"
done

openssl req -x509 -newkey rsa:2048 -nodes -days 7 \
  -keyout certs/localhost.key -out certs/localhost.crt \
  -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"

echo "Ready. Start the server: python mock_pathoeqa_server.py"
