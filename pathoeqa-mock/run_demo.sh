#!/usr/bin/env bash
# Runs both demo attempts against the mock (server must already be running).
# Usage: ./run_demo.sh good|bad
set -uo pipefail
cd "$(dirname "$0")"
export REQUESTS_CA_BUNDLE="$PWD/certs/localhost.crt"
export PYTHONPATH="${VERITAS_REPO:-..}/veritas${PYTHONPATH:+:$PYTHONPATH}"

case "${1:-good}" in
  good) ID=11111111-1111-4111-8111-111111111111 ;;
  bad)  ID=22222222-2222-4222-8222-222222222222 ;;
  *) echo "usage: $0 good|bad"; exit 2 ;;
esac

python -m veritas_runner run-attempt \
  --attempt-id "$ID" \
  --api-url "${MOCK_PUBLIC_URL:-https://localhost:8443}" \
  --oidc-token dummy \
  --workdir "/tmp/veritas-demo/$ID" \
  --output-dir "/tmp/veritas-demo/$ID/out" > "/tmp/veritas-demo-$1.json"
echo "exit code: $?   result: /tmp/veritas-demo-$1.json"
cat "/tmp/veritas-demo-$1.json"
