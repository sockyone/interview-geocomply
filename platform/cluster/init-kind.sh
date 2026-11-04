#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CLUSTER_NAME="${CLUSTER_NAME:-performance-test}"
CLUSTER_WAIT="${CLUSTER_WAIT:-180s}"
MONITORING_WAIT="${MONITORING_WAIT:-10m}"
STATE_DIR="$REPO_DIR/.runtime/$CLUSTER_NAME"

if [[ $# -eq 1 && ( "$1" == "--help" || "$1" == "-h" ) ]]; then
  cat <<'EOF'
Usage: bash platform/cluster/init-kind.sh

Requires Docker (running), kind, kubectl, and Helm. Does not install tools.
Optional environment variables:
  CLUSTER_NAME  Dedicated kind cluster name (default: performance-test)
  CLUSTER_WAIT  Readiness timeout (default: 180s)
  MONITORING_WAIT  Monitoring installation/readiness timeout (default: 10m)
  KIND_NODE_IMAGE  Explicit kind-compatible node image, ideally pinned by digest

Creates a single-node cluster or reuses an existing cluster with that name.
Installs/updates Prometheus, Grafana, and Kubernetes metric collectors.
Reusing a cluster does not apply changes to its kind configuration.
Writes an isolated kubeconfig under .runtime/<name>/kubeconfig.
EOF
  exit 0
fi

if [[ $# -ne 0 ]]; then
  echo 'Unexpected argument. Use --help for usage.' >&2
  exit 1
fi

if [[ ! "$CLUSTER_NAME" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]]; then
  echo 'CLUSTER_NAME must contain lowercase letters/digits separated by hyphens.' >&2
  exit 1
fi

if [[ ! "$CLUSTER_WAIT" =~ ^[1-9][0-9]*(s|m)$ ]]; then
  echo 'CLUSTER_WAIT must be a positive whole number followed by s or m (e.g. 180s or 5m).' >&2
  exit 1
fi

if [[ ! "$MONITORING_WAIT" =~ ^[1-9][0-9]*(s|m)$ ]]; then
  echo 'MONITORING_WAIT must be a positive whole number followed by s or m.' >&2
  exit 1
fi

for tool in docker kind kubectl helm; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "Missing required tool: $tool" >&2
    exit 1
  fi
done

if ! docker info >/dev/null 2>&1; then
  echo 'Docker is not available. Start Docker and retry.' >&2
  exit 1
fi

# Keep kind and kubectl away from the user's default kubeconfig/context.
umask 077
mkdir -p "$STATE_DIR"
export KUBECONFIG="$STATE_DIR/kubeconfig"
export KIND_EXPERIMENTAL_PROVIDER=docker

stage='checking existing clusters'
on_error() {
  local exit_code=$?
  echo "Cluster initialization failed while $stage (exit $exit_code)." >&2
  echo 'No existing cluster will be automatically deleted.' >&2
  printf 'Inspect with: kubectl --kubeconfig %q --context %q get pods -A\n' \
    "$KUBECONFIG" "kind-$CLUSTER_NAME" >&2
  exit "$exit_code"
}
trap on_error ERR

clusters="$(kind get clusters)"
exists=false
while IFS= read -r name; do
  if [[ "$name" == "$CLUSTER_NAME" ]]; then
    exists=true
    break
  fi
done <<< "$clusters"

if [[ "$exists" == true ]]; then
  stage='exporting the existing cluster kubeconfig'
  echo "Reusing kind cluster $CLUSTER_NAME (existing configuration retained)."
  kind export kubeconfig --name "$CLUSTER_NAME" --kubeconfig "$KUBECONFIG"
else
  stage='creating the kind cluster'
  args=(
    create cluster
    --name "$CLUSTER_NAME"
    --config "$REPO_DIR/platform/cluster/kind-cluster.yaml"
    --kubeconfig "$KUBECONFIG"
    --wait "$CLUSTER_WAIT"
  )
  if [[ -n "${KIND_NODE_IMAGE:-}" ]]; then
    args+=(--image "$KIND_NODE_IMAGE")
  fi
  kind "${args[@]}"
fi

chmod 600 "$KUBECONFIG"
stage='waiting for nodes to become ready'
kubectl --context "kind-$CLUSTER_NAME" wait \
  --for=condition=Ready nodes --all --timeout="$CLUSTER_WAIT"
stage='waiting for cluster DNS to become ready'
kubectl --context "kind-$CLUSTER_NAME" --namespace kube-system \
  rollout status deployment/coredns --timeout="$CLUSTER_WAIT"

stage='initializing monitoring'
CLUSTER_NAME="$CLUSTER_NAME" MONITORING_WAIT="$MONITORING_WAIT" \
  bash "$REPO_DIR/platform/cluster/init-monitoring.sh"

echo "Cluster and monitoring ready: $CLUSTER_NAME"
printf 'Use it with: export KUBECONFIG=%q\n' "$KUBECONFIG"
printf 'Delete it when finished: kind delete cluster --name %q\n' "$CLUSTER_NAME"
