# Published LoadTestDefinition and OCI artifacts

A LoadTestDefinition connects deployment, database preparation and workload in
one versioned document. The [Checkout definition](../../LoadTestDefinitions/checkout.yaml)
refers to its application definition and test suite **by name**, and contains its load
directly in `spec.load`: one file holds the whole test. The
[platform config](../../platform/config/local.yaml) turns names into registry references. Resolution freezes the effective load into
the snapshot and generates the runner's internal profile from it.
There are no developer checkout paths. Seed arguments refer to files **inside
the seed image** (the application image by default), using that image's working directory.

The local registry is CNCF Distribution 2.8.3 on `localhost:5001`, started by
`platform/setup-local.sh` and mirrored into the kind node. Artifacts use OCI Distribution,
so moving to an authenticated registry (ECR, Artifact Registry, Harbor) means changing the
platform config, not the definitions. The registry is persistent and separate from run
namespaces; deleting a run never touches it.

## Definition contract

The packaged [JSON schema](../../platform/src/perf_runner/schemas/load-test-definition.schema.json)
is checked by `perf_runner.definition.load_definition` / `parse_definition`.
Typed models cover the application references, database and image seed, services,
profile and execution settings. Unknown fields and unsupported kinds/versions
fail validation. Required `spec` sections:

| Section | Configuration |
| --- | --- |
| `application` | `definition`: ApplicationDefinition name (`checkout` or `checkout:0.3.0`), and environment-to-output `bindings` |
| `database` | `type: postgres`, `version: '17.6'` (image supplied by the platform), database name, readiness timeout and `seed` |
| `database.seed` | Python module, argument list, timeout and bindings; optional `image` (default: the application image) |
| `services` | Named `payment-stub` services with delay and outcome (image supplied by the platform); empty map allowed |
| `suite` | TestSuite name (`checkout` or `checkout:0.3.0`); the platform chooses the runner image |
| `load` | `flow` (ordered journey/repeat/wait steps), `spawnRate`, `drainTimeoutSeconds`, `stages`, `thresholds`; journey names must exist in the suite bundle; same validator as the runner |
| `execution` | Request/readiness/startup time bounds and runner resource requests/limits |
| `cleanup` | `policy: always` and bounded cleanup timeout |

### Names and the platform config

| Written by the test author | Resolved through the platform config to |
| --- | --- |
| `application.definition: checkout[:V]` | `<registry>/applications/checkout:V` (default `latest`) |
| `suite: checkout[:V]` | `<registry>/suites/checkout:V` (default `latest`) |
| chart named in the AD (`http-service` 0.1.0) | `<registry>/charts/http-service:0.1.0` |
| `database: {type: postgres, version: '17.6'}` | `spec.databases.postgres.17.6` |
| `services.<name>.type: payment-stub` | `spec.services.payment-stub` |
| (nothing; platform-owned) | `spec.runnerImage` |
| image `checkout:0.3.0` in an AD or `seed.image` | `<registry>/checkout:0.3.0`; a full reference with a registry host is kept unchanged |

A name is lowercase letters, digits and dashes, optionally followed by `:version`. Links,
paths, schemes and registry addresses are rejected. The published artifact's
`metadata.name` must match the name it was fetched by. Services reuse the AD's supported
chart. Without a version, the latest published version is used. The run records each
requested reference and its resolved digest.

The output catalog is `database.host|port|database|user|password|url` and
`services.<name>.host|port|url`. Password/URL bindings represent generated Secret
references, not literal credentials. Seeding may bind only database outputs:
services and application do not exist yet. Order is database readiness → seed
completion → service readiness → application readiness → suite. Unknown bindings
and reserved service names fail validation.

Runner resources require positive CPU/memory requests and limits, with requests
no greater than limits, at most 16 CPU and 16 GiB. CPU uses cores/millicores;
memory uses Mi/Gi. The Job deadline derives from the profile duration + drain +
10 seconds execution margin + configured startup allowance. Resolution only records
this; the run lifecycle creates the Job. Platform guardrails (per-container maximum,
namespace quota) are checked separately during resolution; see [guardrails](guardrails.md).

All literal values, arguments, source and fixtures are nonsecret configuration.
Use generated Secret output bindings for credentials. Known sensitive application
environment names cannot contain literal values, and published AD Secret entries
must have run-owned output bindings. Arbitrary user code/data cannot be reliably
classified as secret: authors must keep credentials out of artifacts. Registry
authorization is passed separately and is never part of a definition/snapshot.

## Artifact formats

| Role | Format |
| --- | --- |
| Application, database, seed, stub service, runner | OCI/Docker container images, including image indexes |
| Chart | Standard Helm OCI chart config and gzip content layer |
| ApplicationDefinition | OCI image manifest; config `application/vnd.perf-runner.application-definition.v1+json`; single YAML layer `application/vnd.perf-runner.application-definition.v1+yaml` |
| User script/data bundle | OCI image manifest; config and layer `application/vnd.perf-runner.suite-bundle.v1+json`; layer is a JSON mapping of flat filenames to UTF-8 source text |

Custom artifacts use an empty `{}` config blob. The script bundle includes
`suite.yaml`, Python modules/helpers and optional JSON/YAML data. Its files retain
the existing 900 KiB budget. This representation transports the exact ConfigMap
content without archive extraction. The retained suite directory can be passed
to the existing `publish_suite_scripts(...)` and must produce the recorded
`scriptBundleSha256`. The load (`spec.load`) stays in the definition, outside the suite artifact.

Bundle validation compiles source and inspects declared function signatures
without importing/running user code. The adapter requires
top-level synchronous non-generator functions. Decorator/runtime behavior is
checked by the existing loader at execution; static preflight cannot prove flow
behavior. No runtime dependency installation is added.

Format references: [OCI Distribution API](https://distribution.github.io/distribution/spec/api/)
and [Helm OCI registry support](https://docs.helm.sh/docs/v3/topics/registries/).

## Publish the sample

`platform/setup-local.sh` does all of this. To do it by hand, install the CLI and start the
local registry once. If the named container already exists, reuse it rather than replacing
it. These commands do not touch kind:

```sh
.runtime/perf-runner/bin/pip install -e ./platform
docker run -d --name perf-artifacts-registry \
  --label perf-runner/component=artifact-registry --restart unless-stopped \
  -p 127.0.0.1:5001:5000 -v perf-artifacts-registry-data:/var/lib/registry \
  registry:2.8.3
# Platform team: runner image, supported charts and database images from the platform config.
.runtime/perf-runner/bin/python -m perf_runner.publication platform
# Sample: build the Checkout image named in its AD, publish the AD and suite by name.
.runtime/perf-runner/bin/python -m perf_runner.publication checkout --version 0.3.0
```

A team publishes its own artifacts by name; the name comes from each file's `metadata.name`:

```sh
./loadtest publish application ApplicationDefinitions/checkout.yaml --version 0.3.0
./loadtest publish suite TestSuites/checkout --version 0.3.0
```

Each is pushed as `:<version>` and `:latest`; the output shows the digest. Nothing in the
definition is rewritten. Checkout's image is also the payment-stub image (a demo shortcut
in the platform config) and the seed image (the default). Tags may move; resolution
records digests. Use a new version for intentional releases. Repeating publication under
an existing version can produce a new chart digest, because Helm package timestamps
change; previously resolved digests remain the evidence for that resolution.

## Resolve with an installed client

Resolution requires Python 3.11+, the installed host package and Helm on PATH.
It does not require Docker, a source checkout or Kubernetes access. Docker is
required for sample image publication.

```sh
.runtime/perf-runner/bin/python -m perf_runner.resolution \
  --definition LoadTestDefinitions/checkout.yaml \
  --output .runtime/resolved/checkout-001
```

Resolution verifies manifest digests, config/content blob sizes and hashes,
artifact media types, image/index metadata and image layer availability/size.
Image filesystems are not downloaded; Docker/Kubernetes verifies their contents
when pulling. It fetches the AD/chart/scripts, checks journeys against the
profile, validates output bindings, and renders effective Helm values. It never
contacts Kubernetes or executes seed/flow code. A given image reference is
resolved once per operation so reused roles share an identity.

The new private output directory contains `resolved.json`, status, the downloaded
chart, the pinned AD, the profile and exact script files. The final snapshot
records requested/resolved identities for AD, chart, app, database, setup,
services, runtime and script/data bundle, plus content hashes, execution limits
and setup order. Original definition tags are retained as provenance; use the
snapshot's pinned role references for execution. Run IDs, namespace-specific
Secret references and generated values are finalized only during provisioning.
Do not re-resolve original tags when executing an existing snapshot.

A failed operation has no ready `resolved.json`; the status records a redacted
error class. Existing output directories are rejected rather than overwritten.
Hard termination can leave partial files/status; without the final resolved
snapshot they are not ready for execution. Digest identity establishes content
integrity; artifact signing/attestation policy is not implemented.

HTTPS is the default. Plain HTTP requires `plainHttp: true` in the platform config and
is restricted to loopback development registries. The client accepts explicit
per-authority `Basic` or pre-issued `Bearer` authorization headers through the
`PERF_REGISTRY_AUTH` JSON environment variable. It does not discover credentials,
exchange authentication challenges, or follow redirects. Registries requiring
those features need a transport extension. Docker/Helm publication use their own
login configurations; no credentials are copied into images or retained evidence.

The registry and named data volume remain available for subsequent work. Stopping
this local registry is separate from test namespace cleanup. Do not remove its
volume if retained definitions still need their artifacts.

## Verification

Default contract/transport tests use an in-memory registry and real Helm render
checks. The opt-in test publishes to the running local registry and resolves from
a second working directory containing only the definition and platform config, with an installed
wheel and isolated Python imports:

```sh
.runtime/perf-runner/bin/pip wheel --no-deps --wheel-dir .runtime/artifact-dist ./platform
python3 -m venv .runtime/artifact-client
.runtime/artifact-client/bin/pip install \
  .runtime/artifact-dist/perf_runner-0.1.0-py3-none-any.whl
ARTIFACT_REGISTRY_TEST=1 .runtime/perf-runner/bin/python \
  -m unittest discover -s platform/tests -p test_registry_integration.py -v
```

It also checks missing-artifact failure and that a changed tag leaves the old
digest retrievable. Evidence stays under `.runtime/runs/artifact-check-*/`.
No application environment, seed Job or traffic is executed by this test.
