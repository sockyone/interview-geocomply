# ApplicationDefinitions

An ApplicationDefinition describes **your service as it runs in a load test**: which image,
how many replicas or autoscaling, CPU and memory, health checks and environment. It selects one
of the platform's supported Helm charts and gives its values. One file per service:
`ApplicationDefinitions/<name>.yaml`. Tests refer to it by `metadata.name`.

```yaml
apiVersion: perf-runner/v1alpha1
kind: ApplicationDefinition
metadata:
  name: checkout                      # tests refer to this name
spec:
  chart:
    name: http-service                # supported chart (platform/charts/)
    version: 0.1.0
  values:
    image:
      repository: checkout            # short name = platform registry; or ghcr.io/acme/checkout
      tag: "0.3.0"
    port: 8080
    replicaCount: 1                   # or autoscaling: {enabled: true, minReplicas: 1, maxReplicas: 4}
    resources:
      requests: {cpu: 100m, memory: 128Mi}
      limits: {cpu: "1", memory: 256Mi}
    probes:
      liveness: {path: /healthz}
      readiness: {path: /readyz}
    env:
      PAYMENT_TIMEOUT_MS: "1000"      # plain settings only
```

[checkout.yaml](checkout.yaml) is the sample.

## Rules

- **Values from the test are bound, not written.** The database URL, stub URLs and generated
  passwords come from the LoadTestDefinition's `application.bindings`
  (`DATABASE_URL: database.url`). Names that look sensitive (`PASSWORD`, `TOKEN`, `SECRET`,
  `DATABASE_URL`, …) are rejected as literal `env` values.
- **Resources must fit the platform limits:** at most 2 CPU / 2Gi per container, and every pod in
  the test together at most 8 CPU / 8Gi of limits and 20 pods. With autoscaling, `maxReplicas`
  counts. A test that does not fit is rejected before anything is deployed
  ([guardrails](../docs/design/guardrails.md)).
- The `http-service` chart runs one HTTP container with a Deployment, Service and optional HPA.
  All values are listed in its [README](../platform/charts/http-service/README.md).

## Publish

```bash
./loadtest publish application ApplicationDefinitions/checkout.yaml --version 0.3.0
```

It is published as `checkout:0.3.0` and `checkout:latest`. Build and push your service image
yourself, as you already do in your service's CI.

To check your values against the chart locally:

```bash
.runtime/perf-runner/bin/python -c "import yaml,sys; print(yaml.safe_dump(yaml.safe_load(open('ApplicationDefinitions/checkout.yaml'))['spec']['values']))" > .runtime/values.yaml
helm template checkout platform/charts/http-service -f .runtime/values.yaml
```
