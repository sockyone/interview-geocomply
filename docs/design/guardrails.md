# Platform guardrails

Every load test runs in its own namespace. Before any workload starts, the runner checks
the whole test against a platform policy and then creates objects in the namespace so that
Kubernetes itself enforces that policy. Test authors cannot change the policy; it lives in
`platform/src/perf_runner/guardrails.py` (`Policy`) and is owned by the platform team.

## Implemented

### 1. Every Pod has CPU and memory limits

| Layer | What happens |
| --- | --- |
| Built-in components | The database, seed Job, suite Job and their init containers all declare explicit requests and limits. A unit check fails if one is missing. |
| Application and supporting services | The supported Helm chart always renders `resources` (chart defaults: 100m/128Mi request, 1 CPU/256Mi limit). An ApplicationDefinition can change them within the policy. |
| Anything else | The namespace `LimitRange` gives a container without values a default (100m/128Mi request, 500m/512Mi limit) and rejects any container above the maximum (2 CPU, 2Gi). |

### 2. Each test namespace has a resource cap

A `ResourceQuota` (`run-quota`) limits the whole test:

| Resource | Default |
| --- | --- |
| Requests | 4 CPU, 4Gi memory |
| Limits | 8 CPU, 8Gi memory |
| Pods | 20 |
| Services | 10; LoadBalancer and NodePort: 0 |
| PersistentVolumeClaims | 0 (the test database uses `emptyDir`) |

### 3. Pre-flight check (fast feedback)

During resolution, before a namespace exists, the runner builds every Pod the test will create
from the same code the lifecycle uses: the rendered Helm chart for the application and each
service, plus the database, seed and suite manifests. It then checks:

- each container against the per-container maximum;
- the sum of all Pods (HPA `maxReplicas` counts, not the starting replica count) against the quota.

Totals assume all Pods run at the same time. This is conservative, because the seed Job finishes
before the workload starts, but it keeps the rule simple to explain. A test that does not fit stops
in under a second with a readable reason, exit code 2, and nothing deployed:

```
resolve GuardrailError: Test exceeds platform resource limits:
  - suite/suite: cpu limit 3 exceeds the per-container maximum 2
```

The accepted plan is recorded in the run directory as `guardrails.json` and in `resolved.json`
(`resourcePlan`), together with the exact objects applied.

### 4. Traffic stays inside the test namespace

Four `NetworkPolicy` objects:

| Policy | Effect |
| --- | --- |
| `default-deny` | Drops all ingress and egress for every Pod in the namespace. |
| `allow-same-namespace` | Pods of this test may talk to each other (generator → app → database/stubs). |
| `allow-dns` | Egress to CoreDNS (`kube-system`, `k8s-app=kube-dns`) on port 53. |
| `allow-monitoring-scrape` | Ingress from the `monitoring` namespace, for future application metrics. |

Results:

- The load generator can only reach the application under test. A wrong URL cannot send load to
  production, another team's test or the internet.
- The application cannot reach real dependencies (for example a real payment provider). It
  has to use the seeded database and the stubs deployed in its namespace.
- Pods cannot reach the Kubernetes API.

Kubelet health probes come from the node, not from a Pod, so they are not affected.

**Requirement:** the cluster network plugin must enforce NetworkPolicy. kindnet (kind ≥ 0.24)
does; on EKS use the VPC CNI with network policy enabled, Calico or Cilium. kindnet programs
the rules for a new Pod asynchronously, within about a second of the Pod starting. Our workloads
first wait for their dependencies, so this does not matter in practice. The opt-in check
waits 10 seconds before probing.

## Verification

- `platform/tests/test_guardrails.py` (default suite): quantity parsing, object shapes, Pod effective resources
  (init containers vs. containers), LimitRange defaults in the plan, rejection messages, HPA max,
  every built-in container declaring limits, and the sample test fitting the policy.
- `platform/tests/test_guardrails_kind.py` (`GUARDRAILS_KIND_TEST=1`, live cluster): same-namespace traffic is
  open; another namespace, the internet and the Kubernetes API are blocked; a container without
  resources gets the defaults; a 4-CPU container and a NodePort Service are rejected.
- Golden Checkout run with all guardrails applied: passed (5 Pods, 4.5 CPU / 1792Mi of limits
  against an 8 CPU / 8Gi allowance).

## Documented only (production roadmap)

These are not implemented in the prototype. They belong to a shared, multi-team cluster:

| Guardrail | Why |
| --- | --- |
| Expiry label + reaper CronJob that deletes run namespaces past `expires-at` | Guaranteed teardown when the runner or CI job is killed. Today a killed run needs `perf_runner.run cleanup`. |
| Caps on total test duration and peak users per team | Cost control beyond per-stage schema limits. |
| Concurrency limits (runs per team / per cluster) | Avoid noisy neighbours and capacity exhaustion. |
| ValidatingAdmissionPolicy requiring the guardrail objects and labels on test namespaces | Enforcement even when someone bypasses the CLI with raw `kubectl`. |
| Pod Security Admission `restricted` on test namespaces | Block privileged Pods. Needs non-root database and app images first. |
| Image allow-list (approved registries, digest only) | Supply-chain control. Resolution already pins images to digests. |
| Per-team RBAC (create/read test namespaces only) | Teams cannot touch each other's runs or the monitoring stack. |
| Dedicated, tainted load-test node pool with a hard size limit, spot instances, scale to zero | Hard cost ceiling and isolation from other workloads. |
| Budget alerts and cost-allocation labels (`team`, `run-id`) | Spend visibility per team and per run. |
