#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CLUSTER_NAME="${CLUSTER_NAME:-performance-test}"
MONITORING_WAIT="${MONITORING_WAIT:-10m}"
CHART_VERSION="$(cat "$REPO_DIR/platform/cluster/monitoring/chart-version")"

if [[ $# -eq 1 && ( "$1" == --help || "$1" == -h ) ]]; then
  cat <<'EOF'
Usage: bash platform/cluster/init-monitoring.sh

Install/update monitoring in a cluster previously created by init-kind.sh.
Requires Helm and kubectl (1.31+). Reads the isolated cluster kubeconfig.
CLUSTER_NAME defaults to performance-test; MONITORING_WAIT defaults to 10m.
Prometheus, Grafana, kube-state-metrics and node-exporter are installed in
namespace monitoring. No dashboards are provisioned.
EOF
  exit 0
fi

if [[ $# -ne 0 ]]; then
  echo 'Unexpected argument. Use --help for usage.' >&2
  exit 1
fi
if [[ ! "$CLUSTER_NAME" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ||
      ! "$MONITORING_WAIT" =~ ^[1-9][0-9]*(s|m)$ ]]; then
  echo 'Invalid CLUSTER_NAME or MONITORING_WAIT. See --help.' >&2
  exit 1
fi
for tool in helm kubectl; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "Missing required tool: $tool" >&2
    exit 1
  fi
done

export KUBECONFIG="$REPO_DIR/.runtime/$CLUSTER_NAME/kubeconfig"
if [[ ! -s "$KUBECONFIG" ]]; then
  echo 'Cluster kubeconfig is missing. Run platform/cluster/init-kind.sh first.' >&2
  exit 1
fi
context="kind-$CLUSTER_NAME"
kube=(kubectl --context "$context" --namespace monitoring)

on_error() {
  local exit_code=$?
  echo "Monitoring initialization failed (exit $exit_code). Resources retained for inspection." >&2
  printf 'Inspect with: kubectl --kubeconfig %q --context %q -n monitoring get pods,pvc\n' \
    "$KUBECONFIG" "$context" >&2
  exit "$exit_code"
}
trap on_error ERR

# Explicit context avoids accidentally installing into another current cluster.
kubectl --context "$context" get nodes >/dev/null
echo "Installing monitoring chart $CHART_VERSION in $context..."
helm upgrade --install monitoring kube-prometheus-stack \
  --repo https://prometheus-community.github.io/helm-charts \
  --version "$CHART_VERSION" \
  --kube-context "$context" \
  --namespace monitoring --create-namespace \
  --values "$REPO_DIR/platform/cluster/monitoring/values.yaml" \
  --wait --wait-for-jobs --timeout "$MONITORING_WAIT"

# Helm waits for ordinary workloads, but the Prometheus StatefulSet is created
# asynchronously by Prometheus Operator. Wait for its creation and rollout too.
"${kube[@]}" wait --for=create statefulset/prometheus-monitoring-prometheus \
  --timeout="$MONITORING_WAIT"
"${kube[@]}" rollout status statefulset/prometheus-monitoring-prometheus \
  --timeout="$MONITORING_WAIT"
"${kube[@]}" rollout status deployment/monitoring-grafana \
  --timeout="$MONITORING_WAIT"

cat <<'EOF'
Monitoring workloads are ready. Allow a few scrape intervals for metrics to appear.
After exporting the cluster KUBECONFIG, run each port-forward in a separate terminal:

  kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80
  kubectl -n monitoring port-forward svc/monitoring-prometheus 9090:9090

Grafana: http://localhost:3000 (user: admin)
Retrieve its generated password locally (not printed by this script):

  kubectl -n monitoring get secret monitoring-grafana -o jsonpath='{.data.admin-password}' | base64 --decode; echo

Prometheus: http://localhost:9090
Grafana's Prometheus data source is provisioned; dashboards will be added later.
EOF
