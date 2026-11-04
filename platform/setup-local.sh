#!/usr/bin/env bash
# One-time local setup. Safe to re-run: every step reuses what already exists.
#
#   1. checks prerequisites (Docker running, kind, kubectl, helm, Python 3.11+)
#   2. installs the perf-runner CLI into .runtime/perf-runner
#   3. creates the kind cluster `performance-test` with Prometheus and Grafana
#   4. starts a local OCI registry on localhost:5001 and lets the cluster pull from it
#   5. publishes the platform artifacts (runner image, chart, PostgreSQL)
#   6. publishes the Checkout sample service, its ApplicationDefinition and TestSuite
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
VENV=.runtime/perf-runner
REGISTRY=perf-artifacts-registry
SAMPLE_VERSION="${SAMPLE_VERSION:-0.3.0}"
export CLUSTER_NAME=performance-test  # must match cluster.* in platform/config/local.yaml

step() { printf '\n==> %s\n' "$*"; }

step 'Checking prerequisites'
for tool in docker kind kubectl helm python3; do
  command -v "$tool" >/dev/null 2>&1 || { echo "Missing required tool: $tool (see README prerequisites)" >&2; exit 1; }
done
docker info >/dev/null 2>&1 || { echo 'Docker is not running. Start Docker (or Colima) and retry.' >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo 'Python 3.11 or newer is required.' >&2; exit 1; }

step "Installing the perf-runner CLI into $VENV"
[[ -x "$VENV/bin/python" ]] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --disable-pip-version-check -e './platform[charts]'

step "Creating the kind cluster '$CLUSTER_NAME' with monitoring (first run takes a few minutes)"
PATH="$PWD/$VENV/bin:$PATH" bash platform/cluster/init-kind.sh

step 'Starting the local artifact registry on localhost:5001'
if ! docker container inspect "$REGISTRY" >/dev/null 2>&1; then
  docker run -d --name "$REGISTRY" --label perf-runner/component=artifact-registry \
    --restart unless-stopped -p 127.0.0.1:5001:5000 \
    -v perf-artifacts-registry-data:/var/lib/registry registry:2.8.3 >/dev/null
elif [[ "$(docker container inspect -f '{{.State.Running}}' "$REGISTRY")" != true ]]; then
  docker start "$REGISTRY" >/dev/null
fi
for _ in $(seq 1 30); do
  curl -fsS http://127.0.0.1:5001/v2/ >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS http://127.0.0.1:5001/v2/ >/dev/null || { echo 'Registry did not become ready.' >&2; exit 1; }
"$VENV/bin/python" -m perf_runner.kind_registry --node "$CLUSTER_NAME-control-plane" >/dev/null

step 'Publishing platform artifacts (runner image, chart, PostgreSQL)'
"$VENV/bin/python" -m perf_runner.publication platform >/dev/null

step "Publishing the Checkout sample (service image, ApplicationDefinition, TestSuite) as $SAMPLE_VERSION"
"$VENV/bin/python" -m perf_runner.publication checkout --version "$SAMPLE_VERSION" >/dev/null

cat <<DONE

Setup complete. Run the sample load test:

  ./loadtest run LoadTestDefinitions/checkout.yaml

Remove everything when finished:  bash platform/teardown-local.sh
DONE
