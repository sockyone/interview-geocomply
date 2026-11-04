# Load settings (`spec.load`) and the internal LoadProfile

Users write the load in a LoadTestDefinition's `spec.load` (see
[`LoadTestDefinitions/checkout.yaml`](../../LoadTestDefinitions/checkout.yaml)); there is no
separate profile file. The runner turns that section into an internal `LoadProfile`
document (adding `apiVersion`, `kind`, `metadata.name` = the definition name, and
`model: closed-users`), mounts it into the suite Job through its own read-only
ConfigMap, and retains it with the result. The fields below are the `spec.load` fields;
`model` is implied, since closed users is the only model.

## Contract

Use `apiVersion: perf-runner/v1alpha1`, `kind: LoadProfile`, a DNS-label
`metadata.name`, and these required `spec` fields. Unknown fields are rejected.

| Field | Meaning / bounds |
| --- | --- |
| `model` | Only `closed-users` |
| `flow` | 1–50 ordered steps every user repeats: `{journey, repeat?}` (repeat 1–100) or `{waitSeconds}` (0.1–600) |
| `spawnRate` | 1–100 users spawned per second; not a request or journey arrival rate |
| `drainTimeoutSeconds` | 0.1–30 seconds to finish running journeys after stop/shrink |
| `stages` | 1–32 ordered stages, at most 3600 total seconds |
| `thresholds` | Up to 100 `{scope, metric, max}` acceptance limits; may be empty |

Each stage requires `name`, `transition: hold|ramp`, integer `durationSeconds`
(3–3600), integer `users` (1–256), and boolean `measure`. Names must be unique DNS
labels; `measured` is reserved. At least one stage must be measured. There is one
local Locust runner. Resource guards cap journey starts and tracked IDs at 100000
per run; crossing these limits fails execution. Kubernetes CPU/memory resources
belong to Job configuration, not this traffic contract.

Every user walks the same `flow` from its first step and loops back to the start
after the last. Example: `[{journey: browse, repeat: 3}, {waitSeconds: 2}, {journey:
purchase}]` means view 3 product pages, pause 2 s, buy, repeat. There is no random
mix: the order and counts are exact. A pause is a Locust wait between tasks, so a
user being stopped (ramp-down or the end) stops immediately, never mid-journey; a
pause before the first journey is applied after the last one. Each user executes one
synchronous journey at a time. Journey data is copied per attempt; HTTP writes are
never retried. Weighted `journeys` maps from earlier versions are rejected.

Pauses mean idle client connections: the application must keep idle HTTP
connections open longer than the longest pause, or reused connections can be
closed under a request. This was found with gunicorn's 2 s default against a 2 s
pause and fixed in the sample with `--keep-alive 30`.

## Schedule and drain

The adapter requests integer user targets from Locust's `LocalRunner.start`
(nonblocking), on one-second ticks. Locust owns user creation, stopping, sessions,
and HTTP instrumentation. This uses the same runner API as native shapes, with
an explicit outer deadline and completeness checks. It does not use the native
shape worker, whose blocking spawn operation can skip transitions.

Intervals are half-open `[start, end)` on the monotonic clock. A hold retains the
previous target; a different target requires an explicit ramp. The first hold
starts from zero and must fit within one second of spawning. A ramp interpolates
from the previous stage's target (initially zero), rounding to the nearest integer.
For tick `k` within a stage, fraction is `min(1, (k+1)/(duration-1))`. This reaches
the final target a tick before the boundary, leaving time to observe achievement.
A ramp requires `abs(target-previous) <= spawnRate * (duration-2)`. Intermediate
ramp targets can be zero. Subsecond stages, exact arrival rates and RPS targets
are unsupported.

Each stage records absolute start/end timestamps, elapsed tick times, requested
users and actual users before each update. A stage must observe its final target
at a tick. More than one second of tick lateness, an unfinished spawn/stop operation
when a new target is due, or an unachieved stage target fails execution. These are
conservative guards; long-running journeys during shrink can fail rather than
silently extending or skipping the schedule. `reachedTarget` establishes observed
concurrency, not application throughput or constant concurrency between ticks.

At profile end, no further journeys start. Active attempts get the bounded drain;
forced interruption fails execution. Cancellation similarly stops admission and
drains, exits 130, and cannot pass. The runtime execution deadline is profile
duration + drain + 10 seconds; a recommended outer Job deadline adds 60 seconds
for startup. The returned `jobDeadlineSeconds` and host `profile.job_deadline`
provide that value. Slow image pulls may need a larger external startup allowance.
Flow code must cooperate with gevent; an outer Job deadline is still needed for
code that blocks the process. There are no automatic traffic retries.

## Measurements and acceptance

A journey and **all of its HTTP events** belong to the stage in which that
journey started, even if it finishes in another stage or during final drain.
Warmup and ramp inclusion are explicit through `measure`, independent of the
transition type. All stages retain their own summaries. The `measured` summary
includes only events attributed to stages with `measure: true`.

Separate Locust histograms consume individual HTTP and JOURNEY events for every
stage and for the measured union. No cumulative counter is reset. Percentiles
are approximate histogram percentiles, never averages of stage percentiles.
Both successful and failed attempt events contribute to timing/sample counts;
interrupted execution invalidates threshold acceptance.

Threshold scope is `measured` or the name of a measured stage. Supported metrics:

- `journeyP95Ms`: p95 whole-journey milliseconds, inclusive maximum.
- `journeyFailureRatio`: failed journey events / journey events, maximum 0–1.
- `httpFailureRatio`: failed HTTP events / HTTP events, maximum 0–1.

Thresholds aggregate the selected mix; per-journey/route threshold selectors are
not supported. No samples produces `no-samples`, never a pass. Each measured stage
also requires at least one journey sample even with no thresholds. Correctness
remains strict: any business assertion or HTTP failure fails the run, including
in excluded warmup. Thresholds are additional performance gates, not permission
to ignore correctness failures.

A duration-based workload has no finite scheduled-attempt count, so journey
`scheduled` is null. Completeness requires the whole schedule, observed targets,
fully drained attempts, and no execution errors. Results distinguish
`executionOutcome: completed|failed|incomplete` from
`performanceOutcome: passed|failed|incomplete`. Incomplete execution makes every
threshold `incomplete`. A completed workload can fail performance independently
of execution. Overall `outcome` passes only when both pass. Consumers must still
require exit 0 and matching run ID; started/hard-killed results remain incomplete.
CPU saturation warnings remain in `generatorWarnings`. Short runs may finish
before Locust samples CPU, so no warning does not establish generator headroom.

## Host preflight and execution

`perf_runner.load_profile` re-exports the pure model/parser in
`suite_runtime.profile`. The `platform/` Python distribution packages both modules;
install/refresh with `pip install -e ./platform`. Importing preflight needs PyYAML but not
Locust. The runner image copies `suite_runtime` only and uses the identical validator, so
`spec.load` is validated the same way on the host (when the definition is loaded) and in
the load-generator Pod.

```sh
PYTHONPATH=platform/runner .runtime/test-suite/bin/python -m suite_runtime \
  --suite TestSuites/checkout/suite.yaml \
  --profile <internal-profile>.yaml \
  --base-url http://127.0.0.1:8080 --run-id baseline-check \
  --result .runtime/baseline-check.json
```

That runner-image command is for engine development; the internal file has the
shape described above. `--profile` cannot be combined with smoke journey/iteration/user/deadline/drain
flags. `--request-timeout` remains configurable. Without `--profile`, the existing
finite smoke-batch interface and scheduled-count checks remain supported.
