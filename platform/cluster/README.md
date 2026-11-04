# Cluster and monitoring (platform)

`platform/setup-local.sh` calls the scripts here for you. This page is for platform engineers who
want to run or change the cluster setup directly.

## Initialize

Run from the repository root:

```sh
bash platform/cluster/init-kind.sh
export KUBECONFIG="$PWD/.runtime/performance-test/kubeconfig"
kubectl get nodes
```

The script:

1. Validates configuration and checks that Docker is running.
2. Creates the `performance-test` cluster using `platform/cluster/kind-cluster.yaml`, or
   reuses an existing kind cluster with the exact same name.
3. Writes a private kubeconfig to `.runtime/performance-test/kubeconfig`.
4. Waits for all nodes and the CoreDNS Deployment to become ready.
5. Installs/updates monitoring in the `monitoring` namespace and waits for its
   workloads, including the operator-created Prometheus StatefulSet.
6. Prints the commands to connect to Grafana/Prometheus and delete the cluster.

It does not modify the default kubeconfig. The explicit export above selects this
cluster for subsequent commands in your current shell. `.runtime/` is ignored by
Git because it contains credentials.

Re-running initialization checks the existing cluster and reconciles the Helm
release with our monitoring values. It does not recreate the cluster or apply
changes to its kind configuration. Delete and recreate the dedicated cluster
explicitly if you want kind configuration changes to take effect.

## Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `CLUSTER_NAME` | `performance-test` | Dedicated kind cluster name |
| `CLUSTER_WAIT` | `180s` | Timeout for each creation/readiness wait; positive whole seconds or minutes |
| `MONITORING_WAIT` | `10m` | Timeout for each monitoring installation/readiness wait |
| `KIND_NODE_IMAGE` | kind's bundled default | Optional compatible node image, preferably pinned by digest |

Example:

```sh
CLUSTER_NAME=performance-demo CLUSTER_WAIT=5m bash platform/cluster/init-kind.sh
export KUBECONFIG="$PWD/.runtime/performance-demo/kubeconfig"
```

For reproducible CI setup, pin kind, a compatible node image, and kubectl together.
The initial single-node configuration keeps local resource requirements modest;
it does not isolate load-generator CPU or memory from application workloads.

## Monitoring stack

The standard [kube-prometheus-stack chart](https://github.com/prometheus-community/helm-charts/tree/kube-prometheus-stack-89.2.0/charts/kube-prometheus-stack)
is pinned in `platform/cluster/monitoring/chart-version`. Configuration lives in
`platform/cluster/monitoring/values.yaml`.

| Component | What it supplies |
| --- | --- |
| Prometheus | Time-series storage and queries, 15-second scrape interval |
| Grafana | UI with a provisioned Prometheus data source, no dashboards |
| Kubelet/cAdvisor scraping | Container CPU, memory, throttling and usage metrics across namespaces |
| kube-state-metrics | Kubernetes object state, replicas, requests/limits and restart counts |
| node-exporter | Linux kind-node resource metrics (not the macOS host) |
| Prometheus Operator | Standard upstream component that manages Prometheus and scrape configuration |

This uses the upstream Prometheus Operator bundled by the chart; it does not add
a custom test operator or the k6 Operator. Alertmanager and default dashboard/rule
provisioning are disabled. kind's inaccessible scheduler/controller/etcd/proxy
scrape targets are disabled; application container metrics remain enabled.
Database-specific internals such as query latency need additional exporters later.

Prometheus retains up to 24 hours of data, bounded by a 4 GB retention setting on a
5 GiB volume. Grafana uses a 1 GiB volume. Both use kind's `standard` storage class.
Data survives Pod restarts but is lost when the kind cluster is deleted. Export
test artifacts before deleting the cluster. Neither service is publicly exposed.

To update monitoring alone after editing its values:

```sh
bash platform/cluster/init-monitoring.sh
```

Use the same `CLUSTER_NAME` as initialization if you selected a custom name.
The script reads that cluster's isolated kubeconfig, not your current context.
Keep chart upgrades deliberate: a changed chart version may also require the
upstream CRD upgrade procedure; Helm upgrades do not automatically update CRDs.

## Open Grafana and Prometheus

First export the cluster kubeconfig as shown above. Run each port-forward in a
separate terminal (they bind to localhost):

```sh
kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80
```

Open [Grafana](http://localhost:3000), username `admin`. Retrieve the generated
password locally; it is never hard-coded or printed by initialization:

```sh
kubectl -n monitoring get secret monitoring-grafana \
  -o jsonpath='{.data.admin-password}' | base64 --decode
echo
```

```sh
kubectl -n monitoring port-forward svc/monitoring-prometheus 9090:9090
```

Open [Prometheus](http://localhost:9090). After a few scrape intervals, check
Targets and query these metrics, or use Grafana Explore with the Prometheus data
source:

```promql
count(container_memory_working_set_bytes{container!="",container!="POD"})
count(kube_pod_info)
count(node_cpu_seconds_total)
```

Each should return a positive count. For CPU rate queries, allow at least one
minute of data. Workload readiness alone does not prove successful metric scraping;
these checks verify that data is arriving. Future application/dependency Pods
are covered automatically by kubelet scraping and kube-state-metrics.

## Troubleshooting and cleanup

If initialization fails, the script returns a nonzero exit code and identifies the
failed step. It does not automatically delete an existing cluster. If a cluster
exists, inspect it using the printed kubeconfig command or export kind logs:

```sh
kind export logs --name performance-test ./artifacts/kind
```

When finished, delete the dedicated cluster:

```sh
kind delete cluster --name performance-test
```

Metrics Server (the separate service needed for `kubectl top` and CPU-based HPA),
ingress, application dependencies, and test runners are not installed. Prometheus
collects Kubernetes metrics without Metrics Server.

## Script checks

```sh
bash -n platform/cluster/init-kind.sh platform/cluster/init-monitoring.sh
python3 -m venv .runtime/chart-checks
.runtime/chart-checks/bin/pip install -r platform/tests/requirements.txt
.runtime/chart-checks/bin/python -m unittest discover -s platform/tests -v
```

Initialization checks use mock commands; chart checks run Helm locally.
The kind integration check is skipped unless explicitly enabled. See the
[ApplicationDefinition documentation](../../ApplicationDefinitions/README.md) for deployment and live
verification commands.

