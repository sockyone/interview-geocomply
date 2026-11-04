# Checkout sample application

Small Python application for synthetic performance tests. The API uses PostgreSQL
and calls a separate Python HTTP payment stub. All application and setup logic is
Python; Docker/Compose files describe how to run it.

## Run locally

From the repository root:

```sh
docker compose -p performance-sample -f sample-service/compose.yaml up --build -d --wait
python3 -m unittest discover -s sample-service/tests -v
```

The API is at http://localhost:18080. Compose starts a dedicated PostgreSQL instance,
executes the Python schema/seed command, and starts the API and payment stub.
The database is disposable; the password in Compose is synthetic local-only data.
If your installation provides standalone Compose, use `docker-compose` instead of
`docker compose` in these commands.

Clean up this sample independently of kind:

```sh
docker compose -p performance-sample -f sample-service/compose.yaml down -v
```

## API

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/healthz` | Process health, no dependency check |
| GET | `/readyz` | Checks database connectivity and required tables |
| GET | `/products` | Lists the seeded catalog |
| POST | `/orders` | Creates a draft; JSON `productId` and integer `quantity` (1–100) |
| PATCH | `/orders/{id}` | Changes a draft's `quantity`; paid orders return 409 |
| POST | `/orders/{id}/checkout` | Calls payment service and persists paid state |
| GET | `/orders/{id}` | Reads persisted order state |

Example journey: create `{"productId":"coffee","quantity":2}`, capture `id`,
PATCH `{"quantity":3}`, POST checkout with `{}`, then GET and verify `status=paid`
and `amountCents=1500`. Repeating checkout of a paid order returns the same result.
Missing resources return 404; invalid input returns 400. Draft creation is not
idempotent: do not blindly retry it.

Checkout calls `POST ${PAYMENT_BASE_URL}/charges` with `orderId`, `amountCents`, and
`currency: USD`. The stub returns an approved payment after a configured delay,
a decline (402), or an upstream error (503). The API maps declines to 422,
upstream errors to 502 and timeouts to 504. Unsuccessful checkout leaves a draft.
The stub derives a stable payment ID from the order ID for synthetic idempotency;
it is not a real payment ledger and does not enforce amount consistency.

## Configuration

API:
- `DATABASE_URL`: required PostgreSQL connection string.
- `PAYMENT_BASE_URL`: payment HTTP endpoint, default `http://payments:8080`.
- `PAYMENT_TIMEOUT_MS`: positive timeout, default `1000`.

Payment stub:
- `PAYMENT_DELAY_MS`: nonnegative delay, default `50`.
- `PAYMENT_OUTCOME`: `approved`, `declined`, or `error`.

For example, recreate the Compose payment service with
`PAYMENT_OUTCOME=declined docker compose -p performance-sample -f sample-service/compose.yaml up -d payments`.
Set `PAYMENT_DELAY_MS=1500` to exercise API timeouts. Restore defaults before
running the happy-path test suite.

Prepare data explicitly using the same image:

```sh
python -m checkout.database --seed
```

To seed from a real local CSV instead, run
`python -m checkout.database --seed-file fixtures/products.csv`. This validates
all rows before writing and upserts the catalog in one transaction. See the
[local kind database example](../docs/examples/local/README.md) to provision PostgreSQL
and feed local data into it through the Python runner.

Both commands require `DATABASE_URL`. The original `--seed` command creates the
schema if missing and upserts coffee
(500 cents) and tea (300 cents). It does not reset existing orders. This is a small
bootstrap command, not a general schema migration framework.

## Measurement boundaries

The service intentionally uses one database connection per request. Connection
setup is therefore included in latency. Gunicorn runs two processes with four
threads each. Checkout holds an order row lock during a bounded payment call to
serialize concurrent updates/checkouts. Payment and database commits are not an
atomic distributed transaction; a real provider requires durable idempotency and
reconciliation. No authentication, production data, or real payments are involved.

Deploy the application with the [Checkout ApplicationDefinition](../ApplicationDefinitions/README.md).
It selects our supported HTTP service chart and configures the image/command,
resources, fixed replicas or optional HPA,
health/readiness probes, and resolved environment/Secret bindings. Database
preparation remains explicit; workload definitions stay separate.

The [environment preparation command](../docs/examples/local/ENVIRONMENT.md) provisions
PostgreSQL, seeds local data, deploys the payment stub, and binds their generated
outputs into the Checkout AD. Its live checks exercise a complete purchase in kind.
