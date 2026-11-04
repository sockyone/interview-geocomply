# Load engine decision: Locust 2.46.6

Use Locust as a library for the initial Python TestSuite. Locust owns user
concurrency, HTTP sessions/metrics, and custom load shapes; the suite adapter
loads user-authored journey functions from an immutable mounted script bundle and
supplies fail-closed results. The shared runner image contains no scenarios. This is a single
local runner, without distributed aggregation or a custom load engine.

Primary references: [Locust API 2.46.6](https://docs.locust.io/en/2.46.6/api.html),
[using Locust as a library](https://docs.locust.io/en/stable/use-as-lib.html), and
[upstream custom shape implementation](https://github.com/locustio/locust/blob/2.46.6/locust/runners.py).
The selected version is pinned in `platform/runner/requirements.txt`.

## Why Locust, and when to revisit

- Teams write journeys as plain Python functions; the runner image needs no per-team build.
- Locust runs as a library inside one Pod per test, so no extra controller or CRD is needed.
- The LoadTestDefinition keeps journeys (TestSuite) separate from the load shape (`spec.load`),
  so the engine can change behind the same contract.

Revisit when tests need more load than one generator Pod can produce, or open arrival-rate
(RPS) targets: k6 with its Kubernetes operator runs distributed tests natively and supports
arrival-rate executors, at the cost of JavaScript journeys and an operator to run.

## Executable evidence

`platform/runner/spikes/locust_engine.py` starts an isolated synthetic HTTP server and
executes a reusable Python function through a Locust HttpUser/LoadTestShape.

```sh
python3 -m venv .runtime/test-suite
.runtime/test-suite/bin/pip install -r platform/runner/requirements.txt
.runtime/test-suite/bin/python platform/runner/spikes/locust_engine.py \
  --result .runtime/spikes/locust-pass.json
.runtime/test-suite/bin/python platform/runner/spikes/locust_engine.py --fail \
  --result .runtime/spikes/locust-fail.json
```

Verified locally: target users changed from 2 to 4; four simultaneous HTTP
requests were observed. The successful spike exported 210 requests, zero failures,
and p95 40 ms with exit 0. The failing target exported 210 requests, 210 failures,
and p95 40 ms with exit 1. These are synthetic evidence, not Checkout benchmarks.
The executable checks stage transitions, concurrent activity, and nonempty metrics
before reporting success. Exact sample counts/timings vary on another machine.

## Supported model and limitations

The adapter supports a **closed concurrent-user model**. Each user waits for its
journey's requests before doing another task. A users-per-second spawn rate
controls how quickly users are added; it is not an open journey arrival rate or
requests-per-second target. Open arrival-rate scheduling is not supported here.
If that becomes required, reassess the engine/adapter before implementing it.

The first suite entry point executes a finite smoke batch: selected journeys,
iterations per journey, and a maximum concurrent user count. A shared bounded
queue assigns each scheduled attempt once; Locust runs the users. This remains
available alongside timed
[LoadProfile schedules](load-profile.md). Neither mode puts traffic volume in
TestSuite YAML.

The spike uses native LoadTestShape scheduling to establish capability for the
profile implementation. Locust's shape loop ticks roughly once a second and
spawning
can block a shape transition; the LoadProfile adapter validates stage lengths
and spawn bounds and records actual transitions. It calls the nonblocking local
runner API on one-second ticks and fails on unfinished transitions. Subsecond or
exact open-loop
arrival guarantees are not claimed.

Request and whole-journey timings are separate. A synthetic `JOURNEY` request type
feeds Locust's histogram for each complete journey attempt. HTTP entries use
stable route names without order IDs. Percentiles use Locust's bucketed histogram
and are approximate; do not average percentiles across entries/runners. Profile stage/measured HTTP
percentiles consume their own scoped events rather than aggregate percentiles.

Assertions are explicit exceptions (active with Python optimization). Failed HTTP
requests, business assertions, unexpected exceptions, cancellation, deadline
expiry, unfinished work, or engine errors cannot produce a passing result.
Consumers must require both process/Job exit 0 and a completed/passed result with
the expected run ID and counts. A hard kill leaves the initial result incomplete
where available; it cannot guarantee final log/result collection.
