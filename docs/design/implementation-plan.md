# Implementation plan

## New-chat handoff — 2026-11-04

**All seven checkpoints are implemented for the local workflow; remaining work is
the follow-ups listed at the end of this file.** `python -m perf_runner.run run`
executes a published definition end to end and retains a record, diagnostics, the
suite result, a `TestResult`, a readable report and Prometheus resource usage. Read the [LoadTestRun lifecycle](load-test-run.md), the
[published definition contract](published-definition.md) and the
[load settings contract](load-profile.md) before expanding results.

### Decisions to preserve

- The application is user-supplied and may use any language. `sample-service/` is only the
  Checkout sample. ADs select our supported chart/version and inline values.
- Database provisioning uses nested `spec.database.type: postgres` and a seed
  step. No standalone DependencyDefinition or user-selected database chart.
- TestSuite YAML maps journey names to user Python functions. Flows/helpers/data
  are supplied through immutable, read-only ConfigMaps, outside the shared image.
- One LoadTestDefinition file is one complete test. Its `spec.load` holds an
  ordered per-user `flow` (`{journey, repeat}` / `{waitSeconds}`), spawn rate,
  drain, stages and thresholds. There are no separate LoadProfile files and no
  journey weights; the runner generates an internal profile document from
  `spec.load` and mounts it separately from the script bundle, so changing the
  load preserves `scriptBundleSha256`.
- Users never name the script ConfigMap: it is `<suite name>-scripts-<content hash>`.
- Server keep-alive must outlast the longest pause in a flow (sample app and
  payment stub use gunicorn `--keep-alive 30`; the 2 s default dropped connections).
- Locust 2.46.6 owns users and HTTP metrics. The adapter uses nonblocking native
  runner updates on one-second ticks; no arrival-rate/RPS model is implemented.
- Holds retain the preceding target; changes require explicit ramps. Start-stage
  attribution includes all HTTP/journey events of crossing attempts in that
  original stage. `measure` explicitly includes/excludes warmup and ramps.
- Stage and measured-union histograms consume events; never average percentiles
  or reset cumulative counters. No samples cannot pass. Correctness failures
  remain fatal even in excluded warmup. Performance failure is distinct from
  incomplete execution; cancellation during drain cannot report success.
- Host preflight imports the pure `suite_runtime.profile` contract through
  `perf_runner.load_profile`. Root packaging now includes both packages; refresh
  the editable installation after updating from the older package configuration.
  The image still copies only `platform/runner/suite_runtime/`.

### Entry points

| Area | Files |
| --- | --- |
| Architecture / engine | `docs/design/entities.md`, `load-engine.md`, `load-profile.md` |
| Profile contract / metrics | `platform/runner/suite_runtime/profile.py`, `profile_metrics.py` |
| Runtime / CLI / result | `platform/runner/suite_runtime/runner.py`, `__main__.py`, `results.py`; `platform/runner/result.schema.json` |
| Host profile validation | `platform/src/perf_runner/load_profile.py`, `pyproject.toml` |
| User test definitions | `LoadTestDefinitions/` (one file per test, load in `spec.load`) |
| User flows / bundles | `TestSuites/checkout/`, `platform/src/perf_runner/suite_scripts.py` |
| Definition / artifact transport | `platform/src/perf_runner/definition.py`, `registry.py`, `resolution.py`, `publication.py` |
| Definitions / contract | `LoadTestDefinitions/checkout.yaml`, `docs/design/published-definition.md` |
| Existing environment | `platform/src/perf_runner/environment.py`, `docs/examples/local/ENVIRONMENT.md` |
| Live progress | `platform/src/perf_runner/progress.py`; `progress.log` and `events.jsonl` in each run directory |
| Lifecycle | `platform/src/perf_runner/run.py`, `seed.py`, `jobs.py`, `kind_registry.py`; `docs/design/load-test-run.md` |
| Result / metrics / CI | `platform/src/perf_runner/report.py`, `metrics.py`; `.github/workflows/checks.yml`, `load-test.yml`; root `README.md` golden-test section |
| Checks | `platform/tests/test_load_profile.py`, `test_suite_local.py`, `test_suite_kind.py`, `test_run.py`, `test_run_kind.py`, `test_report.py` |

Abbreviated paths within a row are relative to the first file's directory.
The profile CLI is `python -m suite_runtime --suite <suite.yaml> --profile
<profile.yaml> ...`. Omit smoke journey/count/user/deadline/drain flags in profile
mode. See the runner README and profile design for commands and acceptance rules.

### Infrastructure boundaries and next slice

Explicit local target: cluster `performance-test`, context
`kind-performance-test`, kubeconfig `.runtime/performance-test/kubeconfig`, node
`performance-test-control-plane`. Recheck availability before live work; never
print kubeconfig/Secret contents. Run-owned namespaces are deleted after checks;
the shared cluster and unrelated demo namespaces are preserved. Local tools,
images, credentials and evidence under `.runtime/` are ignored and not portable.

Checkpoint 5 selects OCI container images, standard Helm OCI charts, a YAML AD
artifact and a JSON flat-file script artifact. Artifacts are now published to
local CNCF Distribution 2.8.3 at `localhost:5001`, container
`perf-artifacts-registry`, data volume `perf-artifacts-registry-data`. Its host
port is bound to 127.0.0.1. Keep this registry/data available for checkpoint 6;
it is separate from per-run cleanup. No external registry/account was used.

The complete definition is `LoadTestDefinitions/checkout.yaml` (load in `spec.load`). Resolve it with
`python -m perf_runner.resolution --definition <definition> --http-registry
localhost:5001 --output <new-directory>`. Root installation now also requires
jsonschema 4.25.1 and packages the definition schema. `resolved.json` is the ready
snapshot; use its immutable role references, not the original tags, for execution.
User source is validated statically without imports during resolution.

The full lifecycle runs with one command (see the root README "Run the golden
Checkout test"): validate/resolve → owned namespace → database + seed Job →
services → application → suite Job → collect → cleanup → metrics → report.
The kind node pulls from the host registry through a containerd mirror installed
by `python -m perf_runner.kind_registry` (`localhost:5001` inside the node is not
the host registry). Retain evidence before deleting owned namespaces; never delete
the shared cluster or registry as routine test cleanup.

Current published versions: application definition, suite and Checkout image `0.3.0`
(also `latest`); runner image `python-suite:0.3.0`; chart `http-service:0.1.0`; all under
`localhost:5001/performance` as set in `platform/config/local.yaml`.

Checks: default suite `.runtime/perf-runner/bin/python -m unittest discover -s platform/tests -v`;
opt-in real HTTP/engine checks `SUITE_LOCAL_TEST=1 ... -p test_suite_local.py`;
lifecycle checks against kind `RUN_KIND_TEST=1 ... -p test_run_kind.py`. The older
`SUITE_KIND_TEST` check uses `kind load` images and is currently broken (see
"Open issues and next steps" at the end of this file).

## Goal and constraints

Deliver one working Checkout performance test from published artifacts: deploy
isolated dependencies, prepare data, deploy the app, execute a staged workload,
retain results, and clean up. See [entity definitions](entities.md) for terminology.

- All new code maintained here for the sample application, setup, orchestration,
  and test execution is Python. User applications can use any language.
  YAML, Helm templates, Dockerfiles, and GitHub workflow configuration are allowed.
- Existing shell cluster initialization scripts already exist; converting them to
  Python is separate work, not a prerequisite for these checkpoints.
- Use native Kubernetes resources and Helm. No custom test operator, CRDs, or k6 Operator.
- Published execution references versioned artifacts. Local authoring may package
  user Python flows/fixtures into immutable ConfigMaps. Running suite Jobs consume
  a shared runner image and a script-bundle reference, without a checkout mount.
  Published bundles now use the OCI artifact transport described in checkpoint 5.
- Keep load behavior separate from journey code and application deployment settings.
- Start with one test runner, one namespace per run, and fixed application replicas.
- Dashboard definitions are deferred. Preserve the information needed to link or
  export resource metrics later.

## Current state

Already implemented:

- `platform/cluster/init-kind.sh`: dedicated kind cluster and isolated kubeconfig.
- `platform/cluster/init-monitoring.sh` and `platform/cluster/monitoring/`: pinned Prometheus/Grafana
  stack with Kubernetes/container/node metrics; no dashboards.
- `sample-service/checkout/`: Python Checkout API, HTTP payment stub, schema/seed command.
- `sample-service/Dockerfile` and `sample-service/compose.yaml`: container build and local integration environment.
- `sample-service/tests/test_api.py`: real-HTTP checks of the purchase flow and input validation.
- `docs/design/entities.md`: architecture vocabulary.
- `platform/charts/http-service/`: our supported generic HTTP deployment chart.
- `ApplicationDefinitions/checkout.yaml`: sample AD selecting a chart/version and supplying values.
- `platform/tests/test_chart.py` and `platform/tests/test_chart_kind.py`: Helm render/schema checks and opt-in kind readiness verification.
- `platform/src/perf_runner/database.py`: PostgreSQL engine provisioning, local Python seed
  execution, typed outputs, private result/log retention, and cancellation handling.
- `sample-service/fixtures/products.csv` and `checkout.database --seed-file`: real CSV seeding.
- `docs/examples/local/`: runnable database/environment specs, commands, cleanup,
  and Python API examples.
- `platform/src/perf_runner/application.py`, `services.py`, `environment.py`, and
  `kubernetes.py`: output binding, packaged-chart deployment, payment-stub service,
  environment composition, and explicit cluster/process/cleanup helpers.
- `TestSuites/checkout/`: user-authored suite YAML, Python flows, and JSON fixture.
- `platform/runner/`: shared Locust 2.46.6 runtime/image, dynamic script loader,
  client/SDK, and fail-closed JSON results; no application scenarios in the image.
- `LoadTestDefinitions/` and `platform/runner/suite_runtime/profile.py`: one
  file per test with `spec.load` (ordered flow with pauses, closed-user hold/ramp
  stages, scoped measurements, fail-closed thresholds); host validation shares the
  image contract.
- `platform/src/perf_runner/run.py` and helpers: one-command LoadTestRun lifecycle with live
  progress, retained evidence, `TestResult`, report and Prometheus resource usage.
- `.github/workflows/`: default checks and a manually dispatched load test.
- `platform/src/perf_runner/suite_scripts.py`: immutable ConfigMap publication with source
  snapshots and content hashes retained outside the namespace.
- `platform/runner/spikes/locust_engine.py` and `docs/design/load-engine.md`: executable
  engine selection evidence and closed-user scheduling limitations.

Previously verified: kind node and monitoring workloads were ready; Prometheus
received container, Pod, and node metrics. Application checks passed against real
PostgreSQL, including concurrent/repeated checkout. Decline, upstream error, and
timeout behavior were also exercised. Recheck current state before relying on it;
running containers and installed tools are machine-local, not repository guarantees.

All seven checkpoints work locally. Not yet done: running `load-test.yml` on
GitHub, hosted-registry auth, Grafana dashboards and HPA scaling verification
(configured but unverified). See "Open issues and next steps" at the end.

## Build order and checkpoints

```mermaid
flowchart LR
    A[1 ApplicationDefinition] --> B[2 Database engine + seed]
    B --> C[3 TestSuite + minimal result]
    C --> D[4 LoadProfile]
    D --> E[5 LoadTestDefinition]
    E --> F[6 LoadTestRun]
    F --> G[7 TestResult + CI delivery]
```

The following paths are proposed implementation locations, not existing interfaces.
Update this plan if an implementation choice changes them.

## Checkpoint 1 — ApplicationDefinition

**Outcome:** users create an AD by selecting one of our supported charts and
supplying values for their own application image. Checkout is the first example.

Implement:

- Maintain pre-built charts under `platform/charts/`; start with
  `http-service/`, providing `Chart.yaml`, generic defaults, values JSON schema,
  and Deployment, Service, and optional HPA templates.
- Store each user AD as `ApplicationDefinitions/<name>.yaml`, with
  `spec.chart.name`/`version` selecting a supported chart and `spec.values`
  containing inline application configuration. Keep sample source in `sample-service/`;
  user source may live anywhere.
- Support image repository/digest, command/arguments, port, resources, replicas,
  probes, literal environment variables, and Kubernetes Secret references.
- Keep application-specific dependency discovery out of templates. Accept resolved
  settings/Secret references through documented values.
- Default to fixed replicas. When HPA is enabled, omit the Deployment replica
  field so repeated Helm upgrades do not fight the autoscaler.
- Document the chart's configuration and build/package commands.

**Completion checks:**

- [x] Helm lint, render checks, and invalid-values rejection pass.
- [x] Rendered manifests contain expected resources, probes, and Secret references.
- [x] Application health becomes ready in kind when valid database settings and
  prepared data are supplied; it stays unready for an unavailable database.
- [x] Fixed replicas work. HPA is documented as configured but unverified;
  no metrics provider was installed and scaling was not exercised.

Do not add Metrics Server implicitly: the current monitoring stack does not provide
the resource-metrics API required by CPU-based HPA.

## Checkpoint 2 — Database engine, seed, and supporting services

**Outcome:** a fresh PostgreSQL and payment-stub environment can support the app.

Implement:

- Define `LoadTestDefinition.spec.database` with engine `type`, engine settings,
  and a Python seed step. Do not introduce a standalone DependencyDefinition or
  require users to choose a database chart.
- Implement the PostgreSQL engine adapter and `prepare_database(...)` according
  to [the function design](database-preparation.md). First prove local CSV data
  can be seeded into kind through a bounded local Python process/port-forward.
- Configure `spec.services.<name>` with `type: payment-stub`, image, `delayMs`,
  and `outcome`; reuse its Python module/image and the supported HTTP service
  chart internally. No standalone dependency-definition artifact is required.
- Bind `spec.application.bindings` environment names to named database/service
  outputs. Resolve literal values or namespaced Secret/key references outside
  application chart templates.
- Define resources, readiness, disposable database storage, credentials, and stub
  delay/outcome configuration. Generate credentials; keep them out of retained configuration.
- Return database host/port/name/user and namespaced Secret references for
  password and connection URL, for application environment binding.
- Add Python preparation tooling under `platform/src/perf_runner/`. Support local files
  for local development; published execution packages seed code/data in an image
  and uses a bounded Job after TCP database readiness.
- Implement only disposable PostgreSQL and a payment stub initially. Existing
  shared services and arbitrary recursive dependency graphs remain out of scope.

**Completion checks:**

- [x] Deploy into a new namespace, prepare data, deploy Checkout, and complete a purchase.
- [x] Delayed, declined, and unavailable payment configurations produce the documented API outcomes.
- [x] Failed preparation prevents traffic execution and produces useful diagnostics.
- [x] Removing this environment removes its owned storage/resources without affecting another namespace.

## Checkpoint 3 — TestSuite and minimal TestResult

**Outcome:** a packaged Python journey runs against an already prepared environment.

Decide first:

- Select a Python load engine using a small executable spike. Verify reusable
  functions, concurrency, staged scheduling, metric export, and nonzero failure exits.
- Explicitly establish whether it supports open arrival-rate workloads or only
  closed concurrent-user workloads. Do not label users/second or requests/second
  as interchangeable. Do not implement an entire load engine for this assessment.
- Record the selection and limitations in a short design decision document.

Implement:

- Add a shared runtime under `platform/runner/` with the script loader, client,
  SDK, engine dependencies, result writer, entry point, and Dockerfile.
- Store user-authored flows/helpers, suite YAML, and optional JSON fixtures under
  `TestSuites/<name>/`. Resolve declared `module:function` entries from a read-only
  ConfigMap; adding flows must not require rebuilding the shared runtime image.
- Start with create draft → change quantity → checkout → retrieve and assert
  persisted state. Add browse as a second independently schedulable journey.
- Supply base URL, run ID, and injected credentials through a documented contract.
- Use bounded request/polling timeouts and unique per-journey test data. Avoid
  automatic write retries without explicit idempotency support.
- Define a minimal versioned JSON result: run ID, completion state, journey counts,
  failures, timing summaries, threshold outcomes, and execution error details.
- Add one YAML TestSuite selecting the shared image, script ConfigMap, fixture,
  and named function references; keep traffic configuration outside that definition.
- Publish immutable run-owned script bundles with content hashes; retain their
  exact source/data contents before namespace cleanup.

**Completion checks:**

- [x] The shared image executes user-authored journeys from a read-only ConfigMap
  without a checkout mount; a newly added function runs with the same image ID.
- [x] Failed business assertions produce a failed journey and non-passing result.
- [x] Concurrent journeys do not overwrite each other's IDs/state.
- [x] Exceptions and interrupted execution cannot produce a false passing result.

## Checkpoint 4 — LoadProfile and LoadStage

**Outcome:** the same suite image supports different multi-stage workloads.

Implement:

- Add pure typed Python validation and a Locust adapter that can execute inside
  the shared suite image. Runtime execution belongs under
  `platform/runner/suite_runtime/`; share validation with host preflight through
  an explicit packaging/import arrangement. Store profile examples under
  `examples/`, separate from user flow bundles.
- Define journey selection/mix, supported load model, target load, stage duration,
  concurrency bounds, graceful drain, and acceptance criteria.
- Define transitions precisely: ramp toward a target or hold a target. Define
  whether warmup/ramp periods contribute to thresholds and how samples are scoped.
- State whether thresholds apply per stage or across the measured workload.
  Keep stage names in results. Conditional progression/data resets are separate
  orchestration features and are deferred.
- Validate duration/concurrency/resource limits before starting workload execution.

**Completion checks:**

- [x] Smoke and baseline profiles execute the same image without a rebuild.
- [x] A multi-stage workload follows the documented schedule and reports stages separately.
- [x] Warmup inclusion/exclusion follows the documented acceptance semantics.
- [x] Unknown journeys, invalid schedules, and unsupported models fail validation.
- [x] Generator saturation is visible; unmet scheduled work is not silently ignored.

## Checkpoint 5 — LoadTestDefinition and artifact resolution

**Outcome:** a complete definition can be validated and resolved without a source checkout.

Implement:

- Define a versioned YAML schema and typed Python models connecting the AD,
  nested database engine/seed and supporting services, shared suite image/user
  script bundle, inline load profile, execution, and cleanup. Preserve existing
  contracts rather than reintroducing a standalone DependencyDefinition.
- Select and document the registry/artifact format. Helm OCI charts and container
  images are the proposed starting point; the registry provider is not yet chosen.
- Publish supported charts, application/setup images, shared suite runtime image,
  and a versioned user-script/data bundle. Choose the published bundle transport
  and resolve it into the existing immutable ConfigMap contract. Start with explicit
  publication commands; automate them in checkpoint 7.
- Resolve mutable references to immutable artifact identities and record the
  effective configuration. Never retain registry credentials or plaintext secrets.
- Validate bindings, output keys, setup ordering, required values, journey names,
  and supported schema versions. Reject local-path references in published definitions.

**Completion checks:**

- [x] A complete example references registry artifacts and contains no source-file paths.
- [x] A clean environment with only the definition and registry access can resolve its inputs.
- [x] Missing artifacts, invalid bindings, and unsupported configuration fail before provisioning.
- [x] The resolved snapshot identifies exact chart, application, setup, suite-runtime,
  and user-script/data bundle artifacts.

## Checkpoint 6 — LoadTestRun

**Outcome:** one Python command executes the entire test lifecycle.

Implement:

- Add a Python package/CLI under `platform/src/perf_runner/`, with packaging metadata and
  installation instructions. Keep entity validation, Helm/Kubernetes operations,
  execution, and result collection in small modules.
- Generate a unique run ID/name and namespace; persist a run record and resolved
  snapshot outside that namespace before provisioning.
- Execute validate → provision dependencies → prepare data → deploy app → wait
  for readiness → run suite Job → collect → clean up → finalize record.
- Use explicit Kubernetes context/namespace, bounded waits and Job deadlines.
  Disable automatic retries of load execution Jobs.
- Use ownership labels and generated resource names. Delete only resources owned
  by this run. Never delete the shared kind cluster as routine per-run cleanup.
- Capture diagnostics before deletion. Handle ordinary exceptions and termination
  signals; preserve the primary outcome and record cleanup failures separately.
- Detect an interrupted existing run and report it. Automatic resume/retry is not
  required initially; never silently execute its traffic a second time.
- Document hard-termination limits and provide an explicit cleanup command for
  an abandoned run. A shared long-lived environment would need independent expiry cleanup.

**Completion checks:**

- [x] A complete run succeeds with one CLI invocation and a unique retained record.
- [x] Two runs have isolated namespaces, credentials, and result paths.
- [x] Dependency startup, preparation, app readiness, threshold failure, timeout,
  and ordinary cancellation paths retain diagnostics and attempt owned-resource cleanup.
- [x] Collection or cleanup failure cannot overwrite a test failure or falsely report success.
- [x] Hard interruption leaves enough identifiers for explicit abandoned-run cleanup.

## Checkpoint 7 — TestResult and GitHub Actions delivery

**Outcome:** users can launch tests and inspect retained evidence after environment deletion.

Implement:

- Expand the minimal result schema with resolved artifact identities, stage
  summaries, absolute timestamps, performance/execution/collection/cleanup outcomes,
  report locations, and resource-usage evidence.
- Export Prometheus resource data for the run namespace/time window, covering app,
  dependencies, preparation, and generator where samples exist. State missing data;
  very short-lived Pods may disappear between scrapes.
- Retain structured results, a readable report, logs, and metric exports. Do not
  aggregate separate runner percentiles into a global percentile.
- Keep Grafana dashboard implementation deferred. When a dashboard exists, use
  run/namespace variables and absolute timestamps; a link alone is not durable evidence.
- Add GitHub workflows for artifact publication and manually dispatched execution.
  Authenticate to the chosen registry without baking credentials into images.
- Reuse the Python CLI in CI. Upload artifacts and diagnostics even when the test
  fails; preserve failure status after successful upload/cleanup.
- For ephemeral CI kind clusters, delete the cluster after evidence upload. Keep
  local/shared-cluster teardown explicit and separate from per-run cleanup.

**Completion checks:**

- [x] Workflow summary shows outcomes and links to downloadable evidence.
- [x] Reports remain useful after the namespace and ephemeral cluster are deleted.
- [x] A failed test still uploads evidence and fails the workflow.
- [x] Incomplete results and unavailable metrics are clearly identified.
- [x] A new session/user can reproduce the golden test using documented commands.

## Handoff procedure

1. Read `docs/design/entities.md`, this plan, and the relevant existing code.
2. Recheck available tools and current infrastructure. Do not assume previous
   containers or the local `.runtime/` tools exist elsewhere.
3. Implement the first unfinished checkpoint; avoid building all schemas before
   validating a working slice. Record decisions when open contracts are resolved.
4. Run checks relevant to the change. Separate mock/render checks from real
   integration verification in the progress record.
5. Update the status below with changed files, verification evidence, unresolved
   issues, and the exact next action. Never mark a checkpoint complete solely
   because its code exists.

## Progress record

| Checkpoint | Status | Next action |
| --- | --- | --- |
| 1 — ApplicationDefinition | Complete (HPA scaling unverified) | Reuse existing chart/AD contract |
| 2 — Database engine + seed/services | Complete; published seed runs as a native Job (checkpoint 6) | Reuse environment preparation |
| 3 — TestSuite | Complete: shared image + auto-named user-flow ConfigMaps | Reuse runtime and bundle contract |
| 4 — LoadProfile / LoadStage | Complete; now authored as `spec.load` in the definition (ordered flow, no weights) | Reuse internal profile contract |
| 5 — LoadTestDefinition | Complete with local OCI publication/resolution | Reuse typed contract and pinned snapshot |
| 6 — LoadTestRun | Complete for local workflow | Reuse run record, suite result and diagnostics |
| 7 — TestResult/CI | Complete locally; workflows not executed on GitHub | Run `load-test.yml` once on GitHub and fix runner-specific issues |

### Checkpoint 1 evidence — 2026-11-02

- Added `platform/charts/http-service/Chart.yaml`, `values.yaml`,
  `values.schema.json`, and
  Deployment/Service/optional HPA templates. Values bind literal environment
  settings and existing namespaced Secret keys; no dependency discovery or
  preparation occurs in templates. Digest overrides tag. HPA omits Deployment
  replicas and validates ordered bounds and a CPU request.
- Added chart values/build/package/install/verification documentation in
  `platform/charts/http-service/README.md`; linked it from `sample-service/README.md` and updated root test
  instructions. Added pinned test-only YAML parsing dependency in
  `platform/tests/requirements.txt`.
- Initial verification before the AD separation: Helm 4.3.0 strict lint and
  12 passing tests (six chart tests,
  including 13 invalid-value cases, and six existing initialization tests).
  Chart checks validate rendered selectors/probes/resources/Secret references,
  HPA replica ownership, and packaged-chart rendering; no cluster is used.
- Initial real integration before the AD separation: `CHECKOUT_KIND_TEST=1 ... -m unittest discover -s platform/tests
  -p test_chart_kind.py -v` passed against kind `performance-test`, Kubernetes
  v1.37.0. A generated namespace held disposable PostgreSQL 17.6, a bounded seed
  Job, and two chart releases. Both fixed replicas with prepared data became
  ready; both unavailable-database replicas stayed running/unready with zero
  restarts, healthy `/healthz`, and no ready Service endpoints. The test deleted
  its namespace. No existing cluster/monitoring resources were changed.
- Local image-loading note: bulk loading application plus PostgreSQL hit a
  missing multi-platform archive digest. Loading only `checkout:chart-test`
  succeeded; the database was available to Kubernetes. Documented verification
  loads only the application image.
- Limitations: HPA has render coverage only; resource-metrics API/scaling remain
  unverified. Artifact publication and payment-flow integration are later
  checkpoints. No registry was selected or artifacts published here.

### AD separation

- Separated sample source (`sample-service/`), our generic chart catalog
  (`platform/charts/http-service/`), and user ADs
  (`ApplicationDefinitions/<name>.yaml`). An AD is the selected chart/version plus
  values; applications can come from any repository and language.
- Removed Checkout defaults from the supported chart. The Checkout AD supplies
  its own image, probe paths, payment settings, and database Secret binding.
- Updated tests to render the generic chart and the concrete AD independently;
  the live check now reads the AD's selected chart/version and values.
- Verification after separation: all 13 local tests passed (seven chart/AD tests
  and six initialization tests); strict lint passed with the Checkout AD values.
  The updated live kind test passed in 38 seconds and removed its namespace.
  Two prepared-database replicas became ready; two unavailable-database replicas
  stayed unready with healthy processes and no ready Service endpoints.
- Fixed a test setup race discovered during this verification: PostgreSQL
  readiness now checks TCP instead of the temporary initialization socket before
  starting preparation. The initial failed attempt also removed its namespace.
- Consolidated each AD into one YAML document with `apiVersion`,
  `kind: ApplicationDefinition`, `metadata.name`, and `spec`. Chart name/version
  are in `spec.chart`; Helm values are inline in `spec.values`. Removed the
  separate chart selection and values files and updated both checks to read the
  single document. All 13 local checks pass with the single-file AD, including
  chart selection and rendering its inline values. The live check was not rerun
  for this format-only change; the earlier live evidence above predates it.
- AD publication and automatic artifact resolution were deferred here and
  are implemented in checkpoint 5 below.

### Database preparation implementation

- Implemented `prepare_database(spec, *, target, run)` and typed inputs/outputs
  under `platform/src/perf_runner/database.py`, with package metadata in `pyproject.toml`.
  The caller supplies an existing namespace labelled `perf-runner/run-id` with
  the matching run ID. Existing resources/attempts are rejected rather than
  overwritten or silently reseeded.
- PostgreSQL 17.6 uses native Secret/Service/Deployment resources and disposable
  `emptyDir` storage. TCP readiness and ready Service endpoints precede seeding.
  Credentials are generated; returned endpoints use cluster DNS while the local
  seed receives a temporary localhost URL through its environment.
- Added CSV validation and transactional upsert to `sample-service/checkout/database.py`,
  preserved the original `--seed` option, added `sample-service/fixtures/products.csv`, and
  included fixtures in the application image. Local seed code/data need no image
  build or source mount into kind.
- Added local example YAML, CLI/API/cleanup instructions in `docs/examples/local/`,
  and updated root/application/design documentation. Test dependencies now
  install the runner and sample application requirements.
- Local checks: 18 passing tests, with nine opt-in live tests skipped. Coverage
  includes Helm/AD checks, CSV/definition validation, process cleanup, and existing
  initialization checks.
- Live PostgreSQL checks: seven database integration cases passed (98 seconds)
  plus a separate CLI SIGTERM/cancellation case (12 seconds). SQL queries inside
  PostgreSQL verified original fixture rows and a modified local CSV containing
  coffee at 725 cents and cocoa at 425 cents. Invalid CSV left no schema; SQL
  failure rolled back a created table/insert. Timeout, unavailable connection,
  readiness timeout, credential redaction, reseed rejection, and local process
  cleanup were exercised. Final targeted live rechecks of modified-CSV loading
  and CLI cancellation also passed after input validation was tightened (27
  seconds). Generated test namespaces were removed by test cleanup.
- Initial live checks exposed an empty-output case for `kubectl get
  --ignore-not-found`; switched the existence check to resource names and handled
  empty output without trying to parse JSON.
- Limitations: local seeding only; published seed-image Jobs and immutable image
  resolution remain future work. This CLI leaves the database running for the
  caller and requires explicit namespace cleanup. Automatic AD binding is added
  in the following integration step. The complete test lifecycle is not yet
  implemented, and HPA scaling remains unverified.

### Environment composition — 2026-11-03

- Added generic `bind_application_values(...)` and `deploy_application(...)` in
  `platform/src/perf_runner/application.py`. The AD remains one chart-selection/values YAML.
  A caller supplies explicit output bindings; Secret refs are namespace-checked,
  conflicting literal/Secret entries are replaced, and the original AD is unchanged.
- Added `PaymentStubSpec`/`deploy_payment_stub(...)` in `services.py`, and
  `prepare_environment(...)` plus local CLI/fragment loader in `environment.py`.
  Preparation is ordered database/seed → services → application readiness.
  Invalid binding/chart values fail preflight; seed failure prevents deployment.
- Added `kubernetes.py` for explicit cluster commands, bounded HTTP forwarding,
  command process-group cleanup, and ownership-checked namespace deletion.
  The HTTP chart now accepts runner-controlled `runId` labels on resources/Pods;
  selectors remain stable. Existing releases must match run ownership and chart.
- Added `docs/examples/local/environment.yaml` and `ENVIRONMENT.md`, and updated AD,
  application, chart, root, and architecture documentation. The CLI consumes a
  packaged chart archive; local source/image builds remain authoring steps.
- Local verification: 25 passing checks (13 opt-in live checks skipped), including
  AD bindings, cross-namespace rejection, service settings, seed-failure ordering,
  chart ownership labels, subprocess timeouts, and the existing tests. The local
  suite also passed with ResourceWarning treated as an error.
- Live verification: four environment tests passed in 100 seconds against
  explicit context `kind-performance-test` / cluster `performance-test`, node
  `performance-test-control-plane`. A full create/change/pay/read journey persisted
  a paid order for 1500 cents. Approved/declined/upstream-error/timeout responses
  were 200/422/502/504; zero payment backends produced a connection timeout (504)
  and retained draft state. A rejected connection would instead map to 502.
- Live tests also exercised readiness failure with retained diagnostics, seed
  failure blocking both service and app deployment, rejection of unowned namespace
  cleanup, and deleting one environment while preserving a sentinel in another
  owned namespace. Generated namespaces were removed. Evidence is retained locally
  at `.runtime/runs/environment-check-abd173ab/verification.json` with component
  results and deployment records alongside it.
- Deployment waits include explicit rollout completion. Scenario changes probe
  payment health from the API Pod before sending business requests so dataplane
  convergence is not mistaken for the configured payment outcome. Write requests
  are not retried. Helm release queries use status flags supported across Helm
  3/4 rather than the removed Helm 4 `list --all` flag.
- Limitations: local authoring/execution only; published seed Jobs, registry/digest
  resolution, the complete LoadTestRun lifecycle remain
  later work. Suite execution is implemented in the next checkpoint below.
  `prepare_environment` leaves resources for the caller; caller-owned cleanup
  remains required after success/failure. HPA scaling remains unverified.

### User-authored TestSuite — 2026-11-03

- Selected Locust 2.46.6 using the executable spike and decision document.
  The spike changed native shape targets from two to four users, reached four
  concurrent requests, exported request/failure/latency metrics, and verified
  exit 0 for success and exit 1 for failure. Scheduling remains closed-user only.
- The suite is a YAML definition plus user Python flows/helpers and optional JSON
  data. `TestSuites/checkout/suite.yaml` selects the shared runner image and
  ConfigMap and maps journey names to `scenarios:purchase`/`scenarios:browse`.
  No application scenarios or fixtures are copied into the runner image.
- Moved runtime/client/results into `platform/runner/suite_runtime/` and sample
  flow/data files into `TestSuites/checkout/`. The loader snapshots mounted files,
  resolves functions and relative helper imports in an isolated module namespace,
  rejects missing/async/generator entries, and compiles user code with assertions
  enabled even under optimized Python. Context data is copied per attempt.
- Added `publish_suite_scripts(...)` and its CLI under
  `platform/src/perf_runner/suite_scripts.py`. Bundles become immutable run-owned ConfigMaps
  with a content-hash suffix; edits create new bundles rather than mutate active
  runs. The exact ConfigMap contents are retained outside the namespace. The
  initial adapter accepts flat Python/JSON/YAML text files up to 900 KiB.
- Results include the dynamic suite name, loaded function references, source and
  fixture SHA-256 hashes, generic ID samples, journey/HTTP metrics, bounded errors,
  and fail-closed state. The finite smoke batch is capped at 20000 total attempts.
  `thresholdOutcomes` is empty in this finite smoke mode; profile mode is
  implemented in checkpoint 4 below.
- Source-only checks cover catalog/SDK/result behavior, script snapshots/hashes,
  ConfigMap projected symlinks, missing/async entries, bundle budget enforcement,
  and user/helper assertions under `python -O`. Seven real HTTP/Locust checks
  cover concurrent journeys, business failure, malformed JSON, deadlines, request
  timeout, cancellation, hard-kill incomplete results, and isolated context data.
- Kind verification mounts an immutable ConfigMap read-only into native Jobs
  using a shared `python-suite:0.1.0` image. It tests single/concurrent Checkout
  journeys, wrong-price business failure, and cancellation. It then adds a
  `healthcheck` function/helper through a second ConfigMap and proves that its
  runner image ID is identical while its script digest changes. Original bundles
  remain immutable and source snapshots survive generated namespace cleanup.
- Final verification: 34 default checks passed (21 opt-in checks skipped), seven
  real HTTP/Locust checks passed, and five native Job cases passed in a 39-second
  kind integration run. The single/concurrent/new-flow Jobs exited 0, assertion
  failure exited 1, and cancellation exited 130. All used the same shared image
  identity; the newly added flow had a different bundle SHA-256. Evidence and
  archived ConfigMap contents remain under
  `.runtime/runs/suite-check-5af77ffe/`; the generated namespace was removed.
- Limits: no runtime pip installation, nested Python packages, or large binary
  data bundles in this initial ConfigMap adapter. An image variant is needed only
  for additional dependencies; flow edits need no image rebuild. Published bundle
  transport/registry resolution and LoadProfile support are implemented in
  checkpoints 4–5 below; the full LoadTestRun lifecycle remains later work. Timing histograms are approximate and
  separate percentiles must not be averaged.

### LoadProfile / LoadStage — 2026-11-03

- Added the shared typed parser/models in `suite_runtime/profile.py`, host
  re-exports and explicit package discovery. Validation rejects unknown fields,
  unsupported models, unknown journeys, invalid durations/mixes/threshold scopes,
  concurrency/spawn bounds and schedules that cannot meet the tick margin.
- Added separate smoke/baseline YAML examples and the documented measurement
  contract in `docs/design/load-profile.md`. Runtime options derive execution
  deadlines from duration/drain and expose an outer Job deadline with startup
  allowance. Smoke defaults are preserved.
- Added timed weighted selection and nonblocking Locust user updates, with
  start-stage event attribution, independent stage/measured histograms, target
  observations, bounded admission/drain, attempt/ID resource guards, CPU warnings,
  and fail-closed handling of missed ticks and unfinished transitions.
- Results retain the resolved profile, per-stage timing/sample summaries and
  threshold outcomes. Timed workloads use null scheduled counts. Execution
  completeness and performance acceptance are separate. No-sample thresholds,
  empty measured stages, interrupted drain and cancellation cannot pass.
- Updated schema, runner/architecture/root documentation and tests. Native Jobs
  mount a separate immutable profile ConfigMap, retain it with results, derive
  deadlines, and disable retries. Both profiles retain the same image ID and
  script-bundle hash; adding a new user flow still changes only the bundle.

Verification:

- Default suite: 36 passed, 26 opt-in checks skipped (62 discovered). The pure
  profile tests cover both examples, stage boundaries and 18 invalid-definition
  mutations. Host preflight import was verified without loading Locust.
- Real HTTP/Locust suite: 12 tests passed in 66 seconds. Checks include both
  profiles with unchanged script digest, measured-stage sample accounting,
  failed thresholds, no HTTP samples, crossing journeys, forced drain,
  cancellation during execution and final drain, hard-kill incomplete output,
  and a deliberately stalled generator that crosses profile end and must fail.
- Final native kind verification: nine Job cases passed in 86 seconds. Finite
  single/concurrent and added-flow Jobs passed; both smoke/baseline profiles
  passed using the same shared image ID and script digest. The threshold-only
  failure exited 1 with completed execution/failed performance. Finite and
  profile cancellation exited 130. Business assertion failure still exited 1.
- Final evidence: `.runtime/runs/suite-check-3a82b987/suite-verification.json`,
  per-case `suite/<case>/result.json`, Job manifests/logs, archived profile
  ConfigMaps and `suite-scripts/<sha256>/configmap.json`. The generated namespace
  was removed by test cleanup. Earlier profile evidence remains at
  `.runtime/runs/suite-check-19c9f805/`.
- Local shared image rebuilt/loaded as `python-suite:0.1.0`; image tags are local
  authoring references, not published immutable artifacts. No registry delivery
  is implied by these checks.

Limits: one local runner, approximate histogram percentiles and one-second target
updates. A spawn/stop operation that falls behind fails the schedule; no exact
arrival-rate guarantee. No resource-metrics API/HPA exercise, published artifact
transport, registry resolution, full lifecycle or CI publication was added.

### Published LoadTestDefinition and artifact resolution — 2026-11-03

- Added `platform/src/perf_runner/definition.py`, packaged JSON schema, typed published
  seed/database/application/execution models and a complete registry-only example
  at `examples/published/checkout.yaml`. Validation connects bindings, supported
  service/engine/chart contracts, inline profiles, deadlines, resources and cleanup.
- Added `registry.py`, `publication.py` and `resolution.py`. Container images use
  OCI/Docker manifests; charts use Helm OCI; AD YAML and exact flat UTF-8 script
  maps use versioned single-layer OCI artifacts. Definitions contain no local
  source paths. Seed file paths are image-relative.
- Publication builds/pushes the sample application/setup/payment and shared
  runtime images, mirrors PostgreSQL 17.6, packages/pushes the chart, and publishes
  the AD and script/data bundle. Release descriptor edits use temporary copies.
- Resolution verifies metadata/content digests, artifact types, image layer
  availability, chart selection/rendering, bindings, journey names and statically
  declared function signatures before provisioning. It performs no cluster calls
  and imports no user code. Profile changes preserve the script bundle identity.
- Private retained snapshots identify all eight artifact roles with immutable
  references and keep exact source/fixtures plus resolved configuration and
  execution policy. Failed/missing artifacts leave no ready snapshot. Credentials
  are supplied separately; raw registry/authentication errors are not retained.
- The registry client handles Helm's optional JSON body media type via its
  response Content-Type header, hashing the original bytes. It rejects foreign
  upload origins and redirects. HTTPS is default; explicit loopback HTTP supports
  this local registry. Direct Basic/pre-issued Bearer headers are supported;
  challenge exchange and redirected object storage need a future adapter.

Verification:

- Eight targeted default tests cover the complete typed example, 18 invalid
  definition mutations plus kind/version rejection, local/unqualified references,
  resource/binding checks, real Helm rendering with an in-memory registry, missing
  artifacts, unknown journeys, bundle integrity/path restrictions, no user-code
  execution, credential origin isolation and Helm manifest compatibility.
- Real OCI round trip passed in six seconds using CNCF Distribution 2.8.3 on
  `localhost:5001`. Built an installable wheel and installed it in a separate
  `.runtime/artifact-client` environment. Isolated Python (`-I`) loaded the wheel
  from site-packages in a working directory initially containing only the
  definition. All eight artifact roles resolved to digests. Missing bundle
  rejection and tag mutation/old-digest retrieval passed.
- Evidence: `.runtime/runs/artifact-check-0fcc8094/verification.json`,
  `resolved/resolved.json`, downloaded chart/source/profile, publication and
  resolution logs, and the rejected missing-artifact status. Earlier failure
  evidence under `artifact-check-84a913f3` records the Helm compatibility issue
  fixed before the passing run. No application environment or workload was started.
- Final default suite: 44 passed; 27 opt-in checks skipped (71 discovered),
  including all prior contract/render checks. Traffic/cluster suites were not
  rerun for this artifact-only slice; their checkpoint 4 evidence remains above.

Limits: local registry publication, not external hosting or CI. Published seed
Jobs and runtime consumption of the resolved snapshot are checkpoint 6 work.
The local registry is host-accessible; no kind mirror wiring was changed. Static
preflight does not execute scripts or prove their business behavior. Signing,
automated auth challenge exchange, and object-storage redirects are not added.

### LoadTestRun lifecycle — 2026-11-03

- Added `run.py` (lifecycle, status, abandoned-run cleanup CLI), `seed.py`
  (published database + native seed Job), `jobs.py` (Job manifests, bounded wait,
  evidence collection) and `kind_registry.py`. `database.py` now exposes
  `database_resources(...)`, shared by local and published preparation; `Kubernetes`
  namespace deletion takes the definition's cleanup timeout. Behaviour, exit codes and
  interruption limits are documented in `docs/design/load-test-run.md`.
- Seed and suite are Jobs with `backoffLimit: 0`, active deadlines, non-root, no token.
  Images come from the resolved digests; unpullable images fail after a 45 s grace.
  The suite Job mounts separate immutable script/profile ConfigMaps read-only and the
  script digest is checked against the snapshot. Passwords are generated per run, bound
  only by `secretKeyRef`, and redacted from every retained text file.
- Registry reachability: the registry container was attached to the `kind` network and
  `localhost:5001` mapped to it via containerd `hosts.toml` on
  `performance-test-control-plane` (`python -m perf_runner.kind_registry`, idempotent).
  A probe image never seen by the node pulled by digest through it.
- A race found by live concurrent runs: Service endpoints were Ready but the seed pod got
  "connection refused" (node Service rules not yet converged). The seed Job now has a TCP
  wait init container. A Job pod ignoring SIGTERM held the deadline for 30 s, so seed
  pods use a 5 s grace period.

Verification:

- Default suite: 100 passed, 34 opt-in skipped. `platform/tests/test_run.py` (22 cases, fake
  cluster) covers outcome classification, cleanup/collection failure never overriding a
  test failure, cancellation, no-namespace-no-delete, secret redaction, interruption
  detection via lock, ownership-checked abandoned cleanup, Job manifests and wait bounds.
- Live (`RUN_KIND_TEST=1 ... -m unittest discover -s platform/tests -p test_run_kind.py`): 7 passed in 174 s on
  `kind-performance-test`. Cases: complete run with two concurrent runs (distinct
  namespaces, database users, result paths, same runtime image), threshold failure
  (exit 1, execution completed / performance failed), missing seed file, seed deadline
  (`http.server` hang, DeadlineExceeded), application readiness timeout, SIGTERM during
  the suite Job (exit 130, diagnostics, cleanup ok), and SIGKILL (reported interrupted,
  namespace remained, explicit `cleanup` removed it). No `lt-` namespace remained.
- First standalone run: `.runtime/runs/lt-checkout-baseline-6a19bc8e/` (40 s, passed;
  1254 measured samples, p95 76 ms). Earlier failed concurrent attempt is retained under
  other `lt-checkout-baseline-*` directories as evidence of the Service race fixed above.

Limits: local cluster and registry only; one runner Job; services/application share the
readiness timeout; setup images need `python`; seed pods have no database wait beyond
TCP. A second signal during cleanup can leave a namespace for explicit `cleanup`.
No expiry cleanup for a shared environment. Resource-usage export, HPA scaling, readable
report and CI delivery remain checkpoint 7.

### TestResult, report, metrics and CI — 2026-11-03

- Added `metrics.py` (Prometheus via the Kubernetes API service proxy; actual scraped
  samples, CPU from counter deltas between scrapes, per-role totals, explicit missing
  roles), `report.py` (`testresult.json`, `report.md`, evidence index, limitations) and
  wired both into `run.py`. Metrics are exported after cleanup, after waiting up to 16 s
  so one scrape lands after the last traffic. The report needs only retained files.
- Added `.github/workflows/checks.yml` (default suite on pull requests) and
  `load-test.yml` (manual dispatch: job-local registry, ephemeral kind + monitoring,
  publish, run, summary + evidence link, upload always, cluster/registry teardown, then
  fail with the runner's exit code). Root README now has the golden-test commands.

Verification:

- Default suite: 109 passed, 34 opt-in skipped (`test_report.py` adds 9 pure checks:
  pod-role mapping, counter-reset-safe CPU rates, window filtering, container totals,
  Prometheus failure stated not raised, failed-run report, missing-suite report,
  cleanup/collection limitations, single-scrape CPU limitation).
- Live lifecycle suite: 7 passed in 192 s, now also asserting `testresult.json` and
  `report.md` for passed, threshold-failed, seed-failed (stages empty, "performance is
  unknown") and cancelled runs. No `lt-` namespace remained.
- Local rehearsal of the CI chain: `publication checkout --version 0.1.1` then
  `run run` against the definition it wrote passed (exit 0). This is not a GitHub run.
- Real finding: Prometheus scrapes every 15 s and the default workload keeps most Pods
  for ~20 s, so only the database yields CPU; application/generator/payments have one
  memory sample and no CPU. The report states this under limitations.

Limits: the workflows were validated only as YAML plus the local rehearsal; GitHub-hosted
runner tool versions (Helm, kind, Docker) are unverified. Actions are referenced by major
tag, not commit SHA. The registry client cannot do token-challenge auth, so hosted
registries such as GHCR are not supported (CI uses a job-local registry). No Grafana
dashboard. HPA scaling remains unverified. Runs are local, one generator, one replica.

### User folders for profiles and definitions — 2026-11-03

- Added top-level `load-profiles/` (`smoke`, `baseline`, new `extended` with a 60 s hold) and
  `LoadTestDefinitions/` (`checkout.yaml`, renamed `metadata.name: checkout`), each with a
  README for users. Removed `examples/profiles/` and `examples/published/`; `docs/examples/local/`
  (local database/environment authoring fragments) is unchanged.
- A definition now names its profile (`loadProfile: baseline`); `load_definition` loads
  `load-profiles/<name>.yaml` (sibling folder by default, `--profiles-dir` to change), checks
  the name matches `metadata.name`, and inlines it before validation, so the resolved
  snapshot still freezes the complete profile. Inline profile objects remain accepted.
  `run --profile <name>` selects another profile for one run; the run record stores
  `profile` and `profileSource`. Publication validates its template the same way and keeps
  the name; `load-test.yml` gains a `profile` input.
- Verification: default suite 113 passed (4 new profile-reference checks); live `--profile
  smoke` run from the new folders passed; live lifecycle suite 7 passed in 184 s, no `lt-`
  namespace left.

### Ordered user flow and managed script ConfigMap — 2026-11-04

- LoadProfile `journeys` weights were replaced by `flow`: ordered steps every user repeats,
  `{journey, repeat?}` or `{waitSeconds}`. The runtime walks a per-user cycle and applies
  pauses as Locust waits between tasks (a stopping user ends at once, never mid-journey).
  Old weight maps are rejected with a message. Definition JSON schema updated.
- All three user profiles now model the requested shopper: browse 3 items → wait 2 s →
  purchase. `baseline`: ramp to 10 users over 10 s, hold 30 s (measured). `smoke`: 1 user.
  `extended`: 10 users for 90 s.
- `browse` now views one product page: the sample app gained `GET /products/<id>`
  (404 when missing); the journey picks a product at random from fixture prices.
- TestSuite `spec.scripts.configMap` was removed; the runner names the ConfigMap
  `<suite name>-scripts-<content hash>` (e.g. `checkout-scripts-140ee308e8df`).
- Real finding: the first 10-user run failed (10/168 purchases, `POST /orders` connection
  dropped). gunicorn's default 2 s keep-alive raced the 2 s pause on reused connections.
  The sample app and payment stub now run with `--keep-alive 30`; the rerun passed with 0
  failures. Artifacts republished as `0.2.1` (`0.2.0` contains the 2 s default; `0.1.0` the
  old browse); the definition references `0.2.1`.
- Verification: default suite 115 passed; app API tests passed against the rebuilt local
  compose stack; real Locust engine checks 13 passed, including a new one asserting the
  server sees browse ×3, a >=2 s gap, then a purchase, per cycle; live 10-user baseline
  passed (522 browse / 164 purchase, journey p95 110 ms).
- Lifecycle cluster suite (`RUN_KIND_TEST`): 7 passed in 246 s against `0.2.1`.
- Open issue: the older native-Job suite (`SUITE_KIND_TEST`), which deploys images loaded
  with `kind load` (`checkout:0.1.0`, `python-suite:0.1.0`), now fails before any traffic:
  after rebuilding and reloading those images, the kubelet (v1.37) reports them "not
  present" (also with `pullPolicy: Never`) and tries Docker Hub, although `crictl`/`ctr`
  list them as complete. A plain probe pod reproduces it. Cause not confirmed (suspected
  kubelet image-verification records for images it did not pull). Registry-based runs are
  unaffected. Suggested fix: move that test to registry images via the kind mirror.

### Load settings moved into the definition — 2026-11-04

- Removed the separate LoadProfile files and `load-profiles/` folder: a profile only named
  journeys, so its link to a test suite was implicit. Each LoadTestDefinition now carries
  its load in `spec.load` (`flow`, `spawnRate`, `drainTimeoutSeconds`, `stages`,
  `thresholds`; `model` implied). One file = one complete test.
- The runner still builds an internal LoadProfile document from `spec.load` (name = the
  definition name) for the runner image; users never see it. Removed `--profile`,
  `--profiles-dir`, by-name lookup, the record's profile fields and the workflow's profile
  input. Old `loadProfile` fields and weight maps are rejected by validation.
- Engine tests use `platform/tests/load_fixtures.py` (the definition's own load, plus a short test-only
  smoke shape). Default suite 111 passed.


### Rich lifecycle progress — 2026-11-04

- Added live stderr output with UTC timestamps, monotonic elapsed time, numbered
  lifecycle steps, start/end/failure events and roughly five-second heartbeats for
  blocking readiness waits and cleanup. The command explains artifact resolution,
  database readiness, seed execution, service/application installation, workload,
  diagnostics, cleanup, metrics and report generation.
- Added `--quiet` (retains logs) and `--progress-interval` (0.1–60 seconds).
  stdout remains the final JSON summary. Each run keeps private, flushed
  `progress.log` and `events.jsonl`, referenced in its record and readable report.
  Phase entries also retain elapsed seconds. File/console write failures mark
  collection incomplete and cannot replace a primary test failure.
- Native Job status changes show Pod/init/container state and exit/terminal reasons.
  The runtime emits bounded structured stage/tick/drain/finish events when
  `PERF_RUN_PROGRESS=1`. The host relays supported events for the matching run ID,
  deduplicated by sequence; it does not print arbitrary flow/application output.
  Actual users/targets, journey/failure counts and measurement inclusion are shown.
  These observations do not replace the final Job/result acceptance checks.
- Console and retained messages redact generated credentials and credential-bearing
  URLs, and remove terminal controls. Seven new default checks cover heartbeats,
  flushing, durations, private permissions, quiet mode, redaction, validated/deduped
  events, and logging/report failure outcomes. The HTTP check verifies progress
  events remain distinct from the final JSON and omit bearer credentials.
- Published the runner and a suite release descriptor as `0.2.2` to the existing
  local registry. The sample definition selects `checkout-suite:0.2.2`; its app,
  setup and service images stay on `0.2.1`. User flow code is unchanged. The offline
  registry fixture now supports independent app/runtime release versions.
- Fixed malformed sample runner limits (`512256MiMi` and missing CPU limit) to
  `cpu: '1', memory: 512Mi`; requests remain `200m` / `256Mi`.

Verification:

- Default suite: 83 passed, 36 opt-in checks skipped (119 discovered).
- Existing HTTP/Locust suite: 13 passed in 106 seconds; the added live-progress
  check passed separately in 10 seconds (14 distinct HTTP checks total).
- Full baseline succeeded in 70 seconds with 10/10 users during the measured hold,
  all thresholds passed (measured journey p95 94 ms), diagnostics retained and
  owned namespace removed. Evidence: `.runtime/runs/lt-checkout-08b6a8c9/`, including
  `progress.log`, `events.jsonl`, `report.md`, and `logging-verification.json`.
- Three targeted native lifecycle regressions passed in 119 seconds: threshold
  failure (`lt-checkout-53859eea`, exit 1), ordinary cancellation
  (`lt-checkout-8a123775`, exit 130), and seed deadline
  (`lt-checkout-74c1dc02`, exit 2). All retained progress/diagnostics and cleaned
  their owned namespace. Test command outputs are in `.runtime/progress-*-tests.log`.
- The pre-existing direct kind-loaded-image test issue is unchanged; this slice
  uses the working registry/mirror path. No shared cluster or registry teardown.

Use the command and options in `docs/design/load-test-run.md`. Older runner images
still show host/Pod progress and final results; stage telemetry requires `0.2.2`.

### Latest golden run — 2026-11-04

`python -m perf_runner.run run --definition LoadTestDefinitions/checkout.yaml ...`
(10 users: browse ×3 → wait 2 s → purchase) passed, exit 0, in 71 s; the load phase took 45 s.
Run `lt-checkout-f2a5fb4f`.

- Thresholds: measured journey p95 94 ms (≤ 2000, 584 samples); steady journey failures 0;
  measured HTTP failures 0 (1,022 samples).
- Journeys: browse 534/534 (p95 13 ms), purchase 168/168 (p95 120 ms).
- Peak resources: application 0.12 cores / 75 MiB, database 0.12 / 24 MiB, generator
  0.03 / 41 MiB, payments 0.01 / 53 MiB. The seed Job ended before a scrape (stated as a
  limitation in the report).
- Evidence: `.runtime/runs/lt-checkout-f2a5fb4f/` (`report.md`, `testresult.json`,
  `progress.log`, `diagnostics/` with pod logs, events and resources, `metrics/`).
  Console log: `.runtime/test-logs/loadtest-20261104-035932.log`. Namespace removed.

### Platform guardrails — 2026-11-04

Resource limits and network isolation for every run namespace; see [guardrails](guardrails.md).

- New `platform/src/perf_runner/guardrails.py`: platform `Policy`, namespace objects (LimitRange,
  ResourceQuota, four NetworkPolicies) and a pre-flight resource plan built from the real
  manifests (rendered chart, database, seed, suite). Resolution rejects an oversized test with
  `GuardrailError` before any namespace exists; the plan is stored as `resourcePlan`.
- `run.py` applies the objects right after creating the namespace and records them in
  `guardrails.json` and `run.json`.
- Seed and suite init containers now declare requests/limits (`INIT_RESOURCES`).
- Decision: no cloud IaC. README explains the cluster requirements. Other guardrails
  (reaper, admission policy, PSA, RBAC, node pools, budgets) are documented only.

Verification: default suite 96 passed / 36 skipped (13 new checks); `GUARDRAILS_KIND_TEST=1`
3 passed; golden run `lt-checkout-*` passed with guardrails (5 Pods, 4.5 CPU / 1792Mi limits);
oversized runner limit (3 CPU) rejected at resolve, exit 2, nothing deployed. Finding:
kindnet programs NetworkPolicy for a new Pod asynchronously (~1 s); the live check waits.
Deliverables added: `WRITEUP.md`, `AI-USAGE.md`; repository initialised (`git init`, branch
`main`, not pushed).

### References by name and platform config — 2026-11-04

Test authors no longer write registry addresses.

- `LoadTestDefinitions/checkout.yaml` now says `application: {definition: checkout}` and
  `suite: checkout` (`name` or `name:version`; default `latest`). It no longer has chart,
  database image, seed image (defaults to the application image) or stub image fields.
- New `platform/src/perf_runner/platform.py` + `platform/config/local.yaml` (owned by DevOps;
  `--platform` / `PERF_PLATFORM`): registry prefix, plain-HTTP flag, runner image, database
  images, stub images. Layout: `applications/<name>`, `suites/<name>`, `charts/<chart>`;
  short image names live in the platform registry, full references are kept.
- The TestSuite no longer declares the runner image (the loader allows it to be absent;
  resolution and publication reject it). Runner image `python-suite:0.3.0` is published.
- `perf_runner.publication`: `application <file>`, `suite <folder>` (by `metadata.name`,
  `:version` + `:latest`), `platform`, `checkout` (sample). No definition rewriting;
  `--http-registry` removed from `run`/`resolution` (comes from the platform config).
- Resolution checks that the published `metadata.name` matches the requested name, and
  records requested reference + digest for every role.

Verification: default suite 140 run / 101 passed / 39 skipped (new `test_platform.py`);
published platform + Checkout 0.3.0 to the local registry; golden run with the plain
name-based definition passed (`lt-checkout-0d3e3709`, p95 110 ms, 0 failures).

### Repository layout for users — 2026-11-04

Root reduced from 13 folders to 6 (+ `.github`). Teams only touch the three PascalCase
entity folders; everything DevOps owns is under `platform/`:

- `ApplicationDefinitions/`, `TestSuites/<name>/`, `LoadTestDefinitions/` (team-authored).
- `platform/`: `config/` (platform config per environment), `charts/`, `cluster/` (kind
  config, monitoring values, init scripts), `runner/` (load-generator image and
  `suite_runtime`), `src/perf_runner/` (CLI), `tests/`, `pyproject.toml`.
- `sample-service/` (was `app/`), `docs/` (design + `examples/local`).
- Install with `pip install -e ./platform` (or `pip install -r platform/tests/requirements.txt`);
  run tests with `python -m unittest discover -s platform/tests`. Commands still run from
  the repository root. Paths in older progress entries below were updated to the new layout.

### One-command setup, run guide and docs refresh — 2026-11-05

- `platform/setup-local.sh` (idempotent): prerequisites, CLI venv, kind + monitoring, local
  registry + kind mirror, platform artifacts, Checkout sample. `platform/teardown-local.sh`
  removes the cluster and registry (`--purge` also the registry volume).
- Platform config gained `cluster.kubeconfig` / `cluster.context`, so a run is just
  `python -m perf_runner.run run --definition <file>`; flags still override.
- Root README rewritten as a run guide (prerequisites → setup → run → results → guardrail
  demo → write your own → clean up). Cluster/monitoring details moved to
  `platform/cluster/README.md`. Team guides rewritten: `ApplicationDefinitions/`,
  `TestSuites/`, `LoadTestDefinitions/` READMEs. Design docs updated (lifecycle, database
  preparation, published definition, load settings, engine rationale, entities).
- Verified: setup re-run on the existing environment (exit 0), run with only `--definition`
  passed (`lt-checkout-ec3215b5`), README guardrail example rejected at resolve (exit 2),
  every relative Markdown link resolves, default suite passes.

### Run dashboard and example output — 2026-11-05

- New `perf_runner.dashboard`: one PNG per run (users, journeys/s, journey p50/p95/p99, endpoint
  p95/p50, CPU and memory per component), drawn from retained files only and embedded at the
  top of `report.md`. Missing matplotlib or a drawing error becomes a limitation, never a failed
  run. Replaces `usage_chart.py`. `platform[charts]` is installed by setup, CI and test requirements.
- Prometheus samples are also exported as `metrics/resource-usage.csv` (one row per sample, with
  seconds since the load started). Scrape interval lowered to 5 s; the kubelet's cAdvisor still
  refreshes container stats only about every 10–15 s, which is the real sample spacing (documented;
  the post-load wait stays 16 s so the last sample lands).
- `example-output/lt-checkout-37eb5e0b/`: a complete real run (absolute paths made relative,
  chart archive and lock file omitted), with a README explaining each file.
- Verified: default suite 148 run / 109 passed / 39 skipped (new `test_dashboard.py`);
  three live runs passed with the dashboard generated.

## Open issues and next steps

Ordered by suggested priority. None started.

1. **Cancelled runs lose the suite result.** On cancellation the suite Job is not stopped
   before collection, so the partial result is not captured. Fix: stop the Job, wait
   briefly for its final result line, then collect.
2. **`SUITE_KIND_TEST` is broken.** The kubelet (v1.37) reports `kind load` images as
   "not present" even with `pullPolicy: Never`. Move the test to registry images through
   the kind mirror, as the lifecycle tests already do.
3. **Run `load-test.yml` on GitHub** once and fix runner-specific issues. Not pushed yet
   (local commit only). Actions are now pinned by SHA, kind v0.33.0 is installed with a
   checksum check, and the workflow runs `GUARDRAILS_KIND_TEST` before the load test.
4. **Hosted-registry auth.** The registry client has no token-challenge auth, so GHCR and
   similar are unsupported; CI uses a job-local registry.
5. **CPU evidence for short-lived Pods.** cAdvisor refreshes about every 10–15 s, so the seed
   Job usually has no CPU sample. A shorter kubelet housekeeping interval needs a cluster
   recreate; until then it is stated in the report.
6. **Grafana dashboard** using run/namespace variables and absolute timestamps.
7. **HPA scaling verification** (configured in the chart, never exercised).
