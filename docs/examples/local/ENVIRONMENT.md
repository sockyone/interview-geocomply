# Prepare the full local application environment

> **Advanced, local development only.** For normal use, follow the root
> [README](../../../README.md): `platform/setup-local.sh`, then one `perf_runner.run` command.
> This mode runs pieces of the lifecycle from source, without publishing images.

`prepare_environment(...)` composes the database engine/CSV seed, supporting
services, and ApplicationDefinition deployment from source. It prepares a ready environment
without running load; the full lifecycle (`perf_runner.run`) does all of this plus the
workload, evidence and cleanup.

The [environment YAML](environment.yaml) contains:

- `spec.database`: PostgreSQL engine and local Python seed.
- `spec.services.payments`: supported `payment-stub` type, container image,
  `delayMs` (0–60000), and `outcome` (`approved`, `declined`, or `error`).
- `spec.application.definition`: the user AD YAML, relative to this file.
- `spec.application.bindings`: environment names mapped to named outputs.

The AD still contains only its selected chart/version and values. A binding
replaces that environment name's literal value or Secret reference for this run;
the AD file is not changed. Supported outputs are:

| Output | Binding kind |
| --- | --- |
| `database.host`, `database.port`, `database.database`, `database.user` | Literal environment string |
| `database.password`, `database.url` | Namespaced Secret/key reference |
| `services.<name>.host`, `.port`, `.url` | Literal environment string |

Secret bindings must be in the run's namespace. Unknown outputs are rejected
before provisioning. The runner sets `runId` in effective Helm values to label
Deployment, Pod, Service, and HPA ownership. It validates namespace ownership and
refuses to upgrade releases/Deployments belonging to another run. Explicit
supporting-service redeployment is allowed for changing payment scenarios;
seeding a database again is refused.

## Run against kind

From the repository root, using the Python environment from the
[database example](README.md):

```sh
.runtime/bin/kind get clusters
kubectl --kubeconfig .runtime/performance-test/kubeconfig \
  --context kind-performance-test get nodes

docker build -t checkout:0.3.0 sample-service
.runtime/bin/kind load docker-image --name performance-test checkout:0.3.0
helm package platform/charts/http-service --destination .runtime/charts

kubectl --kubeconfig .runtime/performance-test/kubeconfig \
  --context kind-performance-test create namespace perf-environment-demo
kubectl --kubeconfig .runtime/performance-test/kubeconfig \
  --context kind-performance-test label namespace perf-environment-demo \
  perf-runner/run-id=perf-environment-demo

.runtime/perf-runner/bin/python -m perf_runner.environment \
  --definition docs/examples/local/environment.yaml \
  --chart .runtime/charts/http-service-0.1.0.tgz \
  --kubeconfig .runtime/performance-test/kubeconfig \
  --context kind-performance-test \
  --namespace perf-environment-demo --run-id perf-environment-demo \
  --artifacts .runtime/runs/perf-environment-demo
```

The command explicitly selects the local kind cluster, seeds PostgreSQL, deploys
payments, binds the generated database Secret and payment endpoint into Checkout,
and waits for application readiness. The application and service share the
sample image; user applications can supply their own independently built images.
Deployment consumes the packaged chart archive; a registry is not required for
this local-development example. Published runs resolve everything by name through the
platform config instead.

Inspect the prepared environment:

```sh
kubectl --kubeconfig .runtime/performance-test/kubeconfig \
  --context kind-performance-test -n perf-environment-demo get pods,services
kubectl --kubeconfig .runtime/performance-test/kubeconfig \
  --context kind-performance-test -n perf-environment-demo \
  port-forward service/application-http-service 18081:8080 --address 127.0.0.1
```

The forwarded API is at `http://127.0.0.1:18081`. Use the purchase journey in the
[application README](../../../sample-service/README.md). Stop port-forwarding when finished.
The preparation command leaves the environment running for inspection/use. Its
caller owns cleanup, including after failure:

```sh
kubectl --kubeconfig .runtime/performance-test/kubeconfig \
  --context kind-performance-test delete namespace perf-environment-demo \
  --wait=true --timeout=120s
```

Use a fresh namespace/run ID/artifact directory for another preparation attempt.
Private records survive namespace deletion under the artifact directory:
`database/result.json`, `environment/result.json`, and per-release deployment
attempts under `applications/`. Values include generated Secret references and
non-secret settings; do not put plaintext credentials into AD/service values.
Raw application logs may contain user-sensitive output and should not be
published automatically.

## Python composition

```python
from pathlib import Path
from perf_runner.database import KubernetesTarget, RunContext
from perf_runner.environment import load_environment, prepare_environment
from perf_runner.kubernetes import Kubernetes

# Namespace creation and matching ownership label are the caller's responsibility.
target = KubernetesTarget(Path('.runtime/performance-test/kubeconfig'), 'kind-performance-test')
run = RunContext('perf-environment-demo', 'perf-environment-demo', Path('.runtime/runs/perf-environment-demo'))
try:
    environment = prepare_environment(
        *load_environment(Path('docs/examples/local/environment.yaml')),
        chart=Path('.runtime/charts/http-service-0.1.0.tgz'), target=target, run=run,
    )
    # Next lifecycle step: run the suite against environment.application.url.
finally:
    Kubernetes(target, run).delete_owned_namespace()
```

A full runner must retain the primary failure if collection/cleanup also fails;
this sketch only demonstrates ordering. No load engine is introduced here.

## Verification

```sh
.runtime/perf-runner/bin/python -m unittest discover -s platform/tests -v
ENVIRONMENT_KIND_TEST=1 .runtime/perf-runner/bin/python \
  -m unittest discover -s platform/tests -p test_environment_kind.py -v
```

Build/load the sample image before the live checks. They use explicit kind
context/kubeconfig, generated namespaces, and a packaged chart. They verify a
purchase, persisted state, payment decline/upstream error/timeout/unavailability,
seed failure blocking deployment, application readiness failure diagnostics,
ownership-checked cleanup, and preservation of another namespace's resources.
Per-run evidence is retained under `.runtime/runs/environment-check-*/`.

Payment outcome semantics: approved returns 200 from Checkout; declined returns
422; a stub HTTP 503 maps to 502; exceeding the payment deadline maps to 504.
With zero service backends, a rejected connection maps to 502 and a connection
that times out maps to 504. In every failed checkout case the order remains draft.
Live scenario changes probe payment health from the application Pod before
business requests; this avoids confusing rollout/network convergence with the
configured payment behavior. Write requests are not automatically retried.
