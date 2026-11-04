# Supported HTTP service chart

`http-service` version `0.1.0` is our pre-built deployment chart for a
single-container HTTP application. It supports images built from any language
and repository. It renders a Deployment, ClusterIP Service, and optional
`autoscaling/v2` HPA. Resource names are `<release>-http-service`.

Users select this chart/version and supply values to create an
[ApplicationDefinition](../../../ApplicationDefinitions/README.md). The chart is
maintained here; application source and user ADs have their own locations.

## Values contract

| Value | Default | Meaning |
| --- | --- | --- |
| `image.repository` | `registry.example/application` | User's container repository; default is a placeholder |
| `image.tag` | `0.1.0` | Tag used when digest is empty |
| `image.digest` | empty | `sha256:<64 lowercase hex>`; overrides tag |
| `image.pullPolicy` | `IfNotPresent` | Kubernetes image pull policy |
| `imagePullSecrets` | `[]` | Existing namespaced registry Secrets, as `[{name: registry}]` |
| `command`, `args` | `[]` | String arrays; empty arrays preserve image defaults |
| `port` | `8080` | Container HTTP port; the application must listen here |
| `service.port` | `8080` | Internal Service port, targeting the named container port |
| `runId` | empty | Runner-supplied ownership label; does not change selectors |
| `replicaCount` | `1` | Fixed positive replica count when HPA is disabled |
| `resources` | See `values.yaml` | CPU/memory requests and limits; quantities are strings |
| `probes.liveness` | `/`, 10s period, 2s timeout | Application HTTP health probe |
| `probes.readiness` | `/`, 5s period, 10s timeout | Application HTTP readiness probe |
| `env` | `{}` | Map of environment names to literal strings |
| `secretEnv` | `{}` | Map of environment names to `{name, key}` references |
| `autoscaling.enabled` | `false` | Render an HPA and omit Deployment `replicas` |
| `autoscaling.minReplicas`, `maxReplicas` | `1`, `4` | Positive ordered bounds |
| `autoscaling.targetCPUUtilizationPercentage` | `70` | CPU target relative to CPU requests |

Both probes also accept `initialDelaySeconds`, `periodSeconds`, `timeoutSeconds`,
and `failureThreshold`. Paths must begin with `/`. Users configure paths for
their application; the chart has no database or payment knowledge. Partial
values merge with defaults. JSON schema rejects unknown settings, wrong types,
invalid ports, and malformed Secret bindings. Duplicate names in `env` and
`secretEnv`, reversed HPA bounds, and HPA without a CPU request fail rendering.
Kubernetes performs final resource quantity/limit validation on installation.
Quote quantity values, including whole CPUs.

Dependencies, preparation, endpoint resolution, and Secret creation are external
to this chart. Secrets must exist in the release namespace. Supply only Secret
references and non-secret settings in AD values; Helm retains supplied values in
release metadata. The chart never reads Secret contents during rendering.

## Package and deploy

From the repository root:

```sh
helm lint platform/charts/http-service --strict
helm package platform/charts/http-service --destination artifacts/charts
# Extract spec.values from the AD first; see the AD authoring instructions.
helm template my-application artifacts/charts/http-service-0.1.0.tgz \
  -f .runtime/my-application-values.yaml
helm upgrade --install my-application artifacts/charts/http-service-0.1.0.tgz \
  --kubeconfig "$KUBECONFIG" --kube-context kind-performance-test \
  --namespace my-run -f .runtime/my-application-values.yaml --wait --timeout 180s
```

The user supplies a published application image, preferably by digest, regardless
of where it was built. Publish this chart independently using
`helm push ... oci://<registry>/<charts>` once a registry is selected. Registry
selection and automatic artifact resolution are later checkpoints. See the
[Checkout AD](../../../ApplicationDefinitions/README.md) for a concrete example.

HPA is configured and render-checked; **scaling is unverified**. A resource
metrics provider such as Metrics Server is required and is not installed here.
Prometheus alone does not supply the resource-metrics API. HPA also requires a
positive CPU request. Keep it disabled for the initial performance-test workflow.
When disabling HPA, Helm restores `replicaCount` on the Deployment.

## Verification

```sh
python3 -m venv .runtime/chart-checks
.runtime/chart-checks/bin/pip install -r platform/tests/requirements.txt
.runtime/chart-checks/bin/python -m unittest discover -s platform/tests -v
```

Checks cover lint, schema rejection, generic chart rendering, the Checkout AD's
chart selection and values, HPA ownership of replicas, and chart packaging. For
live verification with the sample application:

```sh
docker build -t checkout:chart-test app
kind load docker-image --name performance-test checkout:chart-test
CHECKOUT_KIND_TEST=1 .runtime/chart-checks/bin/python \
  -m unittest discover -s platform/tests -p test_chart_kind.py -v
```

The live test reads the Checkout AD, overrides its image tag and database Secret
for the generated test namespace, and uses the selected supported chart. It
explicitly uses `.runtime/performance-test/kubeconfig` and context
`kind-performance-test`. It creates disposable PostgreSQL 17.6 and a bounded
seed Job. Two healthy replicas must become ready; two replicas with an unavailable
database must remain running/unready with zero restarts, healthy `/healthz`, and
no ready Service endpoints. Its generated namespace is removed in `finally`.
This checks the sample AD's database behavior; other applications define their
own readiness semantics. Payment journeys and HPA scaling are not exercised.
