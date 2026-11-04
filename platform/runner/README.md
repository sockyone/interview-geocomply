# Shared Python TestSuite runner (load generator)

The runner image contains Locust, the HTTP client, script loader, and result
writer. User flows and fixtures live outside the image. Adding or changing a
flow means publishing the suite again, without rebuilding the runner. The platform
config chooses the runner image (`runnerImage`); `perf_runner.publication platform`
builds and publishes it. Teams normally only need the [TestSuites guide](../../TestSuites/README.md);
this page is for working on the runner itself.

[Checkout](../../TestSuites/checkout/suite.yaml) is an example user-authored suite:

```text
TestSuites/checkout/
  suite.yaml       # named function references
  scenarios.py     # user-defined purchase and browse flows
  fixtures.json    # expected product/quantity data
```

Its definition contains:

```yaml
spec:
  fixtures: fixtures.json
  journeys:
    purchase:
      callable: scenarios:purchase
    browse:
      callable: scenarios:browse
```

## Add your own flow

Create `TestSuites/checkout/custom_flow.py`:

```python
def create_and_update(context):
    response = context.client.post(
        '/orders', name='/orders',
        json={'productId': 'coffee', 'quantity': 1},
    )
    assert response.status_code == 201
    order_id = response.json()['id']

    # Add any additional API steps or business checks here.
    response = context.client.patch(
        f'/orders/{order_id}', name='/orders/{id}',
        json={'quantity': 4},
    )
    assert response.status_code == 200
    assert response.json()['quantity'] == 4
```

Register the function in `suite.yaml`:

```yaml
journeys:
  create-and-update:
    callable: custom_flow:create_and_update
    description: Create a draft and change its quantity
```

Keep the existing journey entries if you want to run them too. Each function
receives a fresh context with:

| Context field | Purpose |
| --- | --- |
| `client` | Instrumented `get`, `post`, `put`, `patch`, `delete`, and `request` methods |
| `run_id` | Current run identity |
| `data` / `fixture` | Independent per-attempt copy of this suite's JSON fixture |
| `check(condition, message)` | Explicit business assertion helper |
| `record_id(value, namespace='default')` | Optional uniqueness check and bounded result evidence |

Normal Python `assert` statements are supported and remain active under Python
optimization: user modules are compiled with optimization disabled. Helpers can
be separate flat Python files, imported using `from .helpers import ...`.
Keep API calls inside the flow functions; module imports run once when the bundle
is loaded. Functions must be synchronous and accept one context; missing functions,
async/generator entries, and unknown journey names fail validation.

Use stable metric names for paths containing IDs, as shown above. The client
applies bounded connect/read timeouts and tracing/authentication headers. It does
not retry writes or follow redirects. State for a flow should stay in its local
variables/context; avoid shared mutable module state between concurrent attempts.
Scripts execute as supplied. The shared image includes Python 3.13, Locust,
requests, and PyYAML; extra third-party dependencies require a suitable runner
image version, without baking the user's scenarios into that image.

## Run locally from files

From the repository root:

```sh
python3 -m venv .runtime/test-suite
.runtime/test-suite/bin/pip install -r platform/runner/requirements.txt
PYTHONPATH=platform/runner .runtime/test-suite/bin/python -m suite_runtime \
  --suite TestSuites/checkout/suite.yaml --list-journeys
PYTHONPATH=platform/runner .runtime/test-suite/bin/python -m suite_runtime \
  --suite TestSuites/checkout/suite.yaml \
  --base-url http://127.0.0.1:18081 --run-id user-smoke-001 \
  --journey purchase --result .runtime/suite-results/user-smoke-001.json
```

The application must already be prepared/reachable. See the
[environment example](../../docs/examples/local/ENVIRONMENT.md). A changed/new flow is
loaded from the bundle on the next execution. Use a fresh result file/run ID.

## Build the image by hand (development)

```sh
docker build -t python-suite:0.1.0 platform/runner
.runtime/bin/kind load docker-image --name performance-test python-suite:0.1.0
```

To run locally in Docker, mount your bundle read-only:

```sh
docker run --rm \
  --mount "type=bind,source=$PWD/TestSuites/checkout,target=/suite-input,readonly" \
  python-suite:0.1.0 --suite /suite-input/suite.yaml \
  --base-url http://host.docker.internal:18081 --run-id docker-smoke-001 \
  --journey purchase --print-result
```

The bind mount is for local development. Kubernetes Jobs mount an immutable
ConfigMap rather than the developer's source checkout.

## Publish user scripts to kind

Use an existing run-owned namespace. For the environment example:

```sh
.runtime/perf-runner/bin/python -m perf_runner.suite_scripts \
  --definition TestSuites/checkout/suite.yaml \
  --scripts TestSuites/checkout \
  --kubeconfig .runtime/performance-test/kubeconfig \
  --context kind-performance-test \
  --namespace perf-environment-demo --run-id perf-environment-demo \
  --artifacts .runtime/runs/perf-environment-demo
```

The publisher copies YAML, Python modules, and JSON data into an immutable
ConfigMap named `<suite name>-scripts-<content hash>`, for example
`checkout-scripts-140ee308e8df`. Users never choose or manage this name.
It returns the actual ConfigMap name and SHA-256, and retains the exact contents
under the artifact directory before provisioning. It reuses only an identical,
run-owned immutable bundle. An edit creates a new name, leaving running Jobs and
previous bundles unchanged. Credentials belong in Secrets, outside script data.

A native Job uses the returned ConfigMap name in a read-only volume:

```yaml
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 120
  template:
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      volumes:
        - name: user-flows
          configMap:
            name: REPLACE_WITH_PUBLISHER_OUTPUT
      containers:
        - name: suite
          image: python-suite:0.1.0
          args: [--suite, /suite-input/suite.yaml, --journey, purchase, --print-result]
          env:
            - name: BASE_URL
              value: http://application-http-service.perf-environment-demo.svc.cluster.local:8080
            - name: RUN_ID
              value: user-smoke-001
          volumeMounts:
            - name: user-flows
              mountPath: /suite-input
              readOnly: true
```

Supply Job metadata/namespace and replace the ConfigMap name before applying.
The complete executable kind verification creates these resources for you:

```sh
SUITE_KIND_TEST=1 .runtime/perf-runner/bin/python \
  -m unittest discover -s platform/tests -p test_suite_kind.py -v
```

Build/load `checkout:0.3.0` and install the local runner first as described in the
environment example. The check provisions an isolated Checkout environment,
publishes/mounts the bundle, and proves a newly added flow/helper runs with the
same image ID and a different bundle hash. It also covers single/concurrent
journeys, smoke/baseline profiles, threshold failure, business failure, and
cancellation. Results, Job manifests, image IDs,
and script snapshots survive cleanup under `.runtime/runs/suite-check-*/`.

The initial bundle supports flat `.py`, `.json`, and YAML files, up to 900 KiB.
This leaves space below Kubernetes' [1 MiB ConfigMap limit](https://kubernetes.io/docs/concepts/configuration/configmap/).
Larger suites/data should use a separately versioned bundle artifact in a future
adapter. Published bundles now use the [OCI artifact transport](../../docs/design/published-definition.md),
which resolves back into the same immutable ConfigMap file contract.

## Execution and results

`--base-url`/`BASE_URL`, `--run-id`/`RUN_ID`, and optional `AUTH_BEARER_TOKEN` are
execution inputs. `--journey` is repeatable; the default selects all declared
journeys. `--iterations` sets attempts per journey (default 1, max 10000), and
`--users` caps closed concurrent users (default 1, max 256). A batch supports at
most 20000 total journey attempts. Request/execution/
drain deadlines default to 3/60/2 seconds, with maxima of 60/3600/30 seconds.
`--fixture` can override the suite's JSON expectations. `--result`/`RESULT_PATH`
defaults to `/results/result.json`; `--print-result` also emits JSON for Job logs.

The [result schema](result.schema.json) includes the dynamic suite name, function
references, script/fixture hashes, per-journey counts/timings, stable HTTP metrics,
failure/error samples, generator warnings, and timestamps. `dataSamples` contains
at most 100 IDs recorded by user flows. It contains no bearer credential or
response bodies. Source bundles must not embed secrets.

Exit 0 requires complete successful work and no HTTP/engine failure. Failures or
deadlines exit 1; handled SIGINT/SIGTERM exits 130. A result starts incomplete,
updates atomically, and cannot be silently reused. Consumers must require exit 0
and completed/passed JSON with matching identity/counts. Hard termination may
leave only incomplete output or no collectable final artifact.

The model is closed concurrent users. Use `--profile <internal-profile>.yaml` (generated from a definition's `spec.load`)
for an ordered per-user flow (journeys, repeats, pauses), hold/ramp stages, measured periods, and thresholds. Profile
flags replace smoke selection/count/user/deadline/drain flags. Mount the profile
separately from user scripts and derive the Job deadline from `profile.job_deadline`.
See the [LoadProfile contract](../../docs/design/load-profile.md) for stage
attribution, no-sample rules, resource bounds and separate execution/performance
outcomes. `thresholdOutcomes` remains empty in finite smoke mode.
Timing percentiles are approximate engine histograms; do not average separate
percentiles. See the [engine decision](../../docs/design/load-engine.md).

## Checks

```sh
.runtime/perf-runner/bin/python -m unittest discover -s platform/tests -v
SUITE_LOCAL_TEST=1 .runtime/perf-runner/bin/python \
  -m unittest discover -s platform/tests -p test_suite_local.py -v
```
