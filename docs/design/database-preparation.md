# Database preparation

Every run gets its own PostgreSQL, created in the run namespace and seeded with the team's data
before the application starts. Teams never choose a database chart: they name an engine and
version, and the platform config supplies the image.

```yaml
# LoadTestDefinitions/checkout.yaml
spec:
  database:
    type: postgres
    version: '17.6'
    name: checkout
    readinessTimeoutSeconds: 150
    seed:
      module: checkout.database            # python -m checkout.database ...
      args: [--seed-file, fixtures/products.csv]
      bindings:
        DATABASE_URL: database.url
      timeoutSeconds: 60
      # image: checkout-setup:1.0          # optional; default is the application image
```

## How a run prepares the database

1. **Resources.** Generate a run-specific user and password, then create a run-labelled Secret
   (`database-credentials`), a ClusterIP Service and a PostgreSQL Deployment with `emptyDir`
   storage and explicit CPU/memory limits. The image is the platform's
   `databases.postgres.17.6`, pinned to a digest.
2. **Readiness.** Wait for `pg_isready -h 127.0.0.1` (the temporary init socket can be ready before
   the real TCP server), then for a ready Service endpoint, within `readinessTimeoutSeconds`.
3. **Seed Job.** Run `python -m <module> <args>` as a Kubernetes Job in the application image (or
   `seed.image`). File paths in `args` are inside that image. An init container waits until the
   database is reachable over the Service. The Job has `backoffLimit: 0`, an active deadline of
   `timeoutSeconds`, runs non-root with all capabilities dropped and no service-account token.
4. **Outputs.** Return `database.host`, `port`, `database`, `user`, and Secret references for
   `password` and `url`. `seed.bindings` and `application.bindings` map environment variables to
   these outputs; credentials are passed only as `secretKeyRef`, never as literal values.

A failed or timed-out seed stops the run before the application is deployed; the Job logs and
events are kept in the run's evidence. The namespace (and the database with it) is deleted at
the end of every run, so no data survives between runs. Reseeding an existing database is
refused.

## Checkout seed

`sample-service/checkout/database.py` accepts `--seed-file`. `sample-service/fixtures/products.csv`:

```csv
id,name,price_cents
coffee,Coffee,500
tea,Tea,300
```

The seed validates the whole CSV first (required columns, unique non-empty IDs, positive integer
prices), creates the Checkout tables and upserts all products in one transaction. Any parse or
SQL error rolls back and exits non-zero. Connection, statement and lock waits are bounded.

## Local development mode

[`docs/examples/local/`](../examples/local/README.md) keeps an earlier, source-based mode for
working on a seed module without publishing an image: `prepare_database(...)` creates the same
database resources, opens a `kubectl port-forward` to it, and runs the seed as a local Python
process from a working directory (`seed.workingDirectory`). Credentials are injected through
`DATABASE_URL` in the child environment only, and the port-forward and child process are always
reaped. Published runs never use this mode: they need no source checkout.

```python
def prepare_database(spec: DatabaseSpec, *, target: KubernetesTarget, run: RunContext) -> PreparedDatabase:
    ...
```

`PreparedDatabase` holds `host`, `port`, `database`, `user`, `password_secret`, `url_secret`
(namespace, Secret name, key) and a `SeedResult` (identity, timestamps, exit code, log paths).
No plaintext password or credential-bearing URL is returned or retained.

## Code

- Published runs: `platform/src/perf_runner/seed.py` (seed Job), `database.py` (resources),
  `jobs.py` (Job manifest).
- Local mode: `platform/src/perf_runner/database.py` (`prepare_database`), `environment.py`.
- Checks: `platform/tests/test_database.py`, `test_run.py` (default), `test_database_kind.py`,
  `test_run_kind.py` (opt-in, live cluster).
