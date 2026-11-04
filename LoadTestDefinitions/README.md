# LoadTestDefinitions

Each file is **one complete load test**: which application and suite (by name), a fresh
database and its seed step, stub services, the load, and execution limits.
[checkout.yaml](checkout.yaml) is the sample, commented section by section: 10 users, each
repeating *browse 3 products → wait 2 s → purchase*, ramped up over 10 s, then 30 s at 10 users
measured against the thresholds.

## Run one

```bash
./loadtest run LoadTestDefinitions/checkout.yaml
```

Exit code 0 means passed. Results go to `.runtime/runs/<run-id>/`: `report.md`,
`testresult.json` and the evidence folder.

## Sections

| Section | What you write |
| --- | --- |
| `application` | `definition: <name>` (or `<name>:<version>`) of your ApplicationDefinition, and `bindings` from env vars to generated outputs |
| `database` | `type: postgres`, `version: '17.6'`, a database `name`, and a `seed` step (`module`, `args`, `bindings`, `timeoutSeconds`); runs in your application image unless `seed.image` is set |
| `services` | Stubs for downstream services, e.g. `payments: {type: payment-stub, delayMs: 50, outcome: approved}` |
| `suite` | `<name>` (or `<name>:<version>`) of your TestSuite |
| `load` | The traffic and pass/fail limits (below) |
| `execution` | Timeouts and the load generator's CPU/memory |
| `cleanup` | `policy: always` and a timeout |

**Bindings** connect environment variables to outputs created for each run:
`database.host|port|database|user|password|url` and `services.<name>.host|port|url`.
Passwords and URLs are delivered through a run-owned Secret, never as literal values.

## The `load` section

| Field | Meaning |
| --- | --- |
| `flow` | What every user does, in order, then repeats. `{journey: <name>, repeat: <n>}` runs a journey n times; `{waitSeconds: <s>}` pauses (think time). |
| `stages` | Users over time. Each stage `ramp`s to a new `users` count or `hold`s it for `durationSeconds`. `measure: false` (e.g. ramp-up) is excluded from thresholds; correctness failures always fail. |
| `spawnRate` | Users added per second while ramping. |
| `drainTimeoutSeconds` | Time allowed for journeys still running at the end. |
| `thresholds` | Pass/fail limits: `scope` is `measured` or a stage name; `metric` is `journeyP95Ms`, `journeyFailureRatio` or `httpFailureRatio`. |

Journey names in `flow` must exist in the suite's `suite.yaml`. Everything, including the
platform resource limits, is checked before anything is deployed.

## Add your own

1. Write and publish your [ApplicationDefinition](../ApplicationDefinitions/README.md) and
   [TestSuite](../TestSuites/README.md).
2. Copy `checkout.yaml`, set `metadata.name`, `application.definition` and `suite` to your names,
   adjust the database seed, stubs and bindings, and write the `load`.
3. Run it with the command above.

Full rules: [published definition](../docs/design/published-definition.md) and
[load settings](../docs/design/load-profile.md).
