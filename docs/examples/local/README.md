# Local database preparation

> **Advanced, local development only.** For normal use, follow the root
> [README](../../../README.md): `platform/setup-local.sh`, then one `perf_runner.run` command.
> This mode runs pieces of the lifecycle from source, without publishing images.

This step creates disposable PostgreSQL in kind and runs a Python seed on your machine
against it. It accepts real local CSV data; no source directory is mounted into
kind and no custom database chart is required.

From the repository root, with Docker/kind running and the dedicated cluster
initialized:

```sh
python3 -m venv .runtime/perf-runner
.runtime/perf-runner/bin/pip install -e ./platform -r sample-service/requirements.txt

kubectl --kubeconfig .runtime/performance-test/kubeconfig \
  --context kind-performance-test create namespace perf-seed-demo
kubectl --kubeconfig .runtime/performance-test/kubeconfig \
  --context kind-performance-test label namespace perf-seed-demo \
  perf-runner/run-id=perf-seed-demo

.runtime/perf-runner/bin/python -m perf_runner.database \
  --definition docs/examples/local/database.yaml \
  --kubeconfig .runtime/performance-test/kubeconfig \
  --context kind-performance-test \
  --namespace perf-seed-demo --run-id perf-seed-demo \
  --artifacts .runtime/runs/perf-seed-demo
```

PostgreSQL `17.6` is pulled into kind if not cached. The runner supports that
engine/version initially. It runs `checkout.database` from `sample-service/`, reading
`sample-service/fixtures/products.csv`; file arguments resolve from the seed working
directory. Change that CSV or point the seed args at another CSV to load your
own catalog. Required columns are `id,name,price_cents`, in that order, with
unique nonempty IDs, nonempty names, and positive 32-bit integer prices.
Rows are upserted in a single transaction; existing orders are preserved.
`--seed` remains supported for the original Compose sample.

The command returns JSON with the in-cluster host/port, database name, generated
username, and Secret references. It retains a timestamped outcome, seed exit code,
engine image, resource names, and private logs under the artifact directory.
Password/connection URL values stay in a generated Kubernetes Secret. For the
sample AD, override `spec.values.secretEnv.DATABASE_URL` with:

```yaml
name: database-credentials
key: url
```

The Secret must be in the application's namespace. The database stays running
for subsequent application deployment. The CLI does not implement the entire
test lifecycle or automatic AD binding yet.

A retained attempt directory and existing-resource checks prevent accidental
reseeding. To make a new attempt, use a fresh run ID, owned namespace, and artifact
directory. The function rejects namespaces lacking the matching ownership label.
A failed or interrupted attempt must never be treated as successful preparation.

Explicit cleanup after the application/test is finished:

```sh
kubectl --kubeconfig .runtime/performance-test/kubeconfig \
  --context kind-performance-test delete namespace perf-seed-demo --wait=true --timeout=120s
```

Namespace deletion removes the database, Secret, Service, and disposable storage.
Retained local results remain available. Ordinary errors/timeouts and CLI SIGINT/
SIGTERM stop the local seed and port-forward process groups. A hard kill or host
crash may leave local processes/resources; the retained run and process IDs help
identify them for explicit cleanup. Raw seed logs are private and may contain
user-provided sensitive output; do not upload them automatically.

## Call from a larger Python runner

```python
from pathlib import Path
from perf_runner.database import (
    KubernetesTarget, RunContext, load_database_spec, prepare_database,
)

# The caller creates a namespace labelled perf-runner/run-id=<run ID> first.
database = prepare_database(
    load_database_spec(Path("docs/examples/local/database.yaml")),
    target=KubernetesTarget(
        Path(".runtime/performance-test/kubeconfig"), "kind-performance-test"
    ),
    run=RunContext("perf-seed-demo", "perf-seed-demo", Path(".runtime/runs/perf-seed-demo")),
)
# Deploy the application with database.url_secret, then execute its workload.
# The caller owns resource cleanup in a finally path, preserving primary errors.
```

This adapter is for local development. Published runs execute the seed as a Kubernetes Job
from the application image instead; see [database preparation](../../design/database-preparation.md).

## Checks

```sh
.runtime/perf-runner/bin/pip install -r platform/tests/requirements.txt
.runtime/perf-runner/bin/python -m unittest discover -s platform/tests -v
DATABASE_KIND_TEST=1 .runtime/perf-runner/bin/python \
  -m unittest discover -s platform/tests -p test_database_kind.py -v
```

Live checks use generated namespaces and remove them automatically. They verify
original and modified CSV contents using SQL inside PostgreSQL, bad-file handling,
SQL transaction rollback, credential redaction, timeouts, unavailable databases,
readiness deadlines, accidental-reseed rejection, CLI cancellation, and local
process cleanup. Other application HTTP integration checks remain separate.

For the next composed step, see [full environment preparation](ENVIRONMENT.md):
seed the database, deploy the payment stub, bind outputs into the AD, and wait for
Checkout readiness using a single Python function/command.
