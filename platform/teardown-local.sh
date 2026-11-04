#!/usr/bin/env bash
# Remove the local cluster and registry created by setup-local.sh.
# Run evidence under .runtime/runs/ is kept. Pass --purge to also delete the registry data.
set -euo pipefail
kind delete cluster --name performance-test
docker rm -f perf-artifacts-registry >/dev/null 2>&1 || true
if [[ "${1:-}" == "--purge" ]]; then
  docker volume rm perf-artifacts-registry-data >/dev/null 2>&1 || true
fi
echo 'Local cluster and registry removed.'
