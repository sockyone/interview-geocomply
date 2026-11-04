# LoadTestRun lifecycle

`./loadtest run` (the `loadtest` CLI, `perf_runner.cli`; `python -m perf_runner.run run` underneath) executes one published
[LoadTestDefinition](published-definition.md) from artifacts to a retained record:

```
validate → resolve names + guardrail pre-flight → namespace + guardrails → database (+ seed Job)
        → services → application → suite Job → collect diagnostics → cleanup → final record
```

```bash
./loadtest run LoadTestDefinitions/checkout.yaml
./loadtest status                      # active / interrupted / finished
./loadtest cleanup --run-id <lt-...>   # remove an abandoned run's namespace
```

The registry, images and cluster come from the [platform config](../../platform/config/local.yaml)
(`--platform` or `PERF_PLATFORM` selects another). `--kubeconfig` and `--context` override its
`cluster` settings; the user's default kubeconfig context is never used. Runs are kept in
`.runtime/runs/` (`--runs-dir` to change). Registry credentials, if any, come from
`PERF_REGISTRY_AUTH` (see [published definition](published-definition.md)).

## Live progress and retained logs

The run command prints timestamped progress to stderr by default, with total elapsed
seconds, the current phase and numbered steps `[1/11]` through `[11/11]`. It explains
artifact resolution, PostgreSQL readiness and seed execution, service/application
deployment, workload execution, evidence collection, cleanup, metrics and reporting.
During a blocking readiness wait or cleanup, a heartbeat says what is still running
roughly every five seconds. Timestamps in the log use UTC (`Z`); elapsed times use a
monotonic clock.

For native Jobs, changes in Pod/container status are shown, including image-pull
problems, init-container activity, exit codes and terminal Job reasons. With the
telemetry-enabled runner, workload messages show the actual stage, workload elapsed
and planned duration, actual/target users, completed journeys, failures and whether
the stage is measured. These observations come from the runner, not an estimated
schedule. Stage changes and roughly five-second updates are relayed from a bounded
log poll; they can arrive a few seconds after the event. The final stage summaries
and threshold results remain the acceptance evidence.

Example output (abbreviated; timings and counts vary):

```text
03:20:14Z +    0.2s INFO  [resolve] [2/11] Resolving and validating artifacts
03:20:17Z +    4.0s INFO  [database] Starting seed Job: checkout.database (deadline 60s; no retries)
03:20:37Z +   23.7s INFO  [suite] Stage steady: 10/40s workload | users 10/10 target | journeys 118 | failed 0 | measured
03:21:08Z +   54.4s INFO  [suite] Threshold measured/journeyP95Ms: passed (actual=94, max=2000, samples=584)
03:21:13Z +   60.2s INFO  [cleanup] [9/11] Removing run resources — still working (5s in this step)
03:21:24Z +   70.4s INFO  [run] PASSED | execution=completed | performance=passed | cleanup=ok | exit=0
```

Every run retains two private, flushed files alongside `run.json`:

- `progress.log`: the readable lifecycle log, usable with `tail -f` during a run.
- `events.jsonl`: structured events with run ID, UTC timestamp, elapsed seconds,
  phase, severity and event type. Runtime observations carry their original fields.

The files survive namespace deletion and are linked from the report. The run record
also retains each provisioning phase's `elapsedSeconds`. Console and retained
messages redact generated credentials and credential-bearing URLs, remove terminal
control characters, and relay only supported structured workload events. Raw
application logs and final result documents are collected separately, rather than
printed into the progress stream.

Use `--progress-interval 10` to reduce heartbeat frequency, or `--quiet` to suppress
live progress while retaining both files. The final JSON summary remains on stdout;
`Record: ...` remains on stderr. For example:

```sh
./loadtest run LoadTestDefinitions/checkout.yaml > .runtime/last-run-summary.json
```

Library callers get the same retained logs; `LoadTestRun(..., quiet=True,
progress_interval=10)` configures output. A progress-log write failure is treated as
a collection failure, so an otherwise passing run exits nonzero. It cannot replace
a primary test failure. Workload log polling is best-effort and does not decide test
success; the bounded Job wait and final result gates still do that. Older runner
images remain usable but provide only host/Pod progress and final stage summaries.
The platform config selects runner image `0.3.0` (live workload events need `0.2.2` or later).

## Isolation and ownership

- Run ID `lt-<name>-<8 hex>` is also the namespace. The namespace is labelled
  `perf-runner/run-id=<id>`; every resource the run creates carries the label.
- Database user/password are generated per run and exist only in that run's Secret.
  They are consumed through `secretKeyRef`; they are never placed in a Job manifest,
  record, or retained log (all retained text is redacted before the record is finalized).
- `runs/<id>/` holds `run.json`, the resolved snapshot (`resolved/`), per-phase evidence,
  `suite/` (Job manifest, logs, `result.json`) and `diagnostics/` (resources, events, every
  container log). It is created before anything is provisioned and is never reused.
- Before any workload starts, the namespace gets a LimitRange, a ResourceQuota and
  default-deny NetworkPolicies; the accepted resource plan and the objects are kept in
  `guardrails.json`. See [guardrails](guardrails.md).
- Cleanup deletes only a namespace whose label matches this run, then verifies it is gone.
  The shared cluster, registry and unrelated namespaces are never touched.

## Execution

- Seed and suite are native Jobs with `backoffLimit: 0` (no retried traffic),
  `activeDeadlineSeconds`, non-root, no service-account token, dropped capabilities.
- Images come only from the resolved snapshot (digests). A Job whose image cannot be
  pulled or configured fails after a 45 s grace instead of waiting for its deadline.
- The seed Job runs `python -m <module> <args>` in the application image (or `seed.image`
  when set), bound to database outputs. That image therefore needs a `python` executable.
- The suite Job mounts the immutable script ConfigMap and a separate immutable profile
  ConfigMap read-only. The script digest is checked against the resolved snapshot.
- Services and the application share `applicationReadinessTimeoutSeconds`.

## Outcomes and exit codes

The record separates the primary test outcome from retention and cleanup:

| Field | Values |
| --- | --- |
| `outcome.execution` | runtime's `executionOutcome`, or `not-run` |
| `outcome.performance` | runtime's `performanceOutcome`, or `not-run` |
| `outcome.collection`, `outcome.cleanup` | `ok`, `failed`, or `skipped` (no namespace was created) |
| `status` | `passed`, `failed`, `error`, `cancelled`, `passed-with-errors` |

| Exit | Status | Meaning |
| --- | --- | --- |
| 0 | `passed` | complete execution, thresholds passed, evidence retained, namespace removed |
| 1 | `failed` | execution completed but thresholds or journey correctness failed |
| 2 | `error` | a lifecycle phase failed, or execution was incomplete / produced no result |
| 3 | `passed-with-errors` | test passed but collection or cleanup failed (see `errors`, `cleanupHint`) |
| 130 | `cancelled` | SIGINT/SIGTERM; diagnostics and cleanup were still attempted |

A cleanup or collection failure never changes `failed`/`error`/`cancelled`; it is
recorded beside them. It downgrades only a passing run.

## Interruption

SIGINT/SIGTERM cancel the active phase, collect diagnostics, and clean up. A second
signal during cleanup can interrupt it; the record then says `cleanup: failed`.

SIGKILL, power loss or a lost host cannot run cleanup. Each run holds an exclusive lock
on `run.lock` for its lifetime, so a record still saying `running` whose lock is free is
reported **interrupted** by `status` and by the next `run` (which never resumes or
re-executes it). The record contains the namespace, context and kubeconfig path, so
`cleanup --run-id` can delete that namespace after re-checking ownership; it refuses a
run that is still active. A shared long-lived environment would additionally need
independent expiry cleanup (not implemented).

## Registry reachability from kind

Resolved references keep their `localhost:5001/...` authority. `python -m
perf_runner.kind_registry` attaches the registry container to the `kind` network and
writes a containerd `hosts.toml` mapping that authority to it inside the node. Required
once per cluster node and idempotent; `platform/setup-local.sh` does it for you.

## TestResult, report and resource metrics

After cleanup the runner writes, inside the run directory (no cluster access needed):

- `testresult.json` — `TestResult`: status/outcomes, absolute run and suite timestamps,
  the resolved artifact identities, the resolved profile, per-stage and measured
  summaries (engine histograms; percentiles are never averaged), threshold outcomes,
  per-journey and per-endpoint figures, resource usage, errors, **limitations**, and an
  index of evidence files.
- `report.md` — the same content for people, with the dashboard image at the top.
- `dashboard.png` — one image with six panels: simulated users (target vs. actual), completed
  journeys per second, latency per journey (p50/p95/p99), p95/p50 latency per HTTP endpoint, and
  CPU and memory per component over time. Drawn by `perf_runner.dashboard` from retained files
  only; redraw any run with `python -m perf_runner.dashboard --run-dir .runtime/runs/<run-id>`.
  Needs matplotlib (`pip install -e './platform[charts]'`, done by `setup-local.sh`); without it
  the run still passes and the report lists the missing dashboard as a limitation.
- `metrics/resource-usage.json`, `series.json` and `resource-usage.csv` — Prometheus samples for
  the run namespace and window, via the Kubernetes API proxy, summarized per role (application,
  database, seed, generator, each service). The CSV has one row per container sample, with
  seconds since the load started. Prometheus scrapes every 5 s, but the kubelet's cAdvisor
  refreshes container stats only about every 10–15 s, so that is the real sample spacing; CPU is
  the rate between consecutive samples.

Missing data is stated rather than hidden: an unavailable Prometheus, roles with no
samples, and Pods too short-lived for a CPU rate (a Pod needs two samples for a CPU rate) all appear under
`limitations`. A missing suite result produces a report that says performance is unknown.

## GitHub Actions

`checks.yml` runs the default (no cluster) suite on pull requests. `load-test.yml` is
manually dispatched: it starts a job-local registry and ephemeral kind cluster, publishes
the platform artifacts and the sample by name, checks that the cluster enforces the
guardrails, runs the same CLI, copies the report to the workflow summary,
uploads the run directories **even when the test fails**, deletes the cluster and
registry, and only then fails the workflow with the runner's exit code. No registry
credentials are involved. Hosted registries that require a token-challenge exchange
(for example GHCR) need a registry-client extension; the client currently accepts only
pre-issued `Basic`/`Bearer` headers (`PERF_REGISTRY_AUTH`).
