# Checkout and Rewards Service

A small FastAPI service backed by PostgreSQL. Checkout, inventory decrement, order snapshots, and coupon redemption commit in one database transaction. PostgreSQL row locks and unique constraints protect the invariants when requests overlap.

## Run locally

Requirements: Python 3.11+ and Docker Compose.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
docker compose up -d db
```

Wait for the database health check, then start the API:

```sh
DATABASE_URL='postgresql+psycopg://checkout:checkout@localhost:5432/checkout' \
  .venv/bin/uvicorn app.main:app --reload
```

Tables and five sample products are created on startup. The seeded products are visible at `GET /products`; `p-005` has only two units. Interactive OpenAPI docs are available at `/docs`.

Reward configuration is controlled by `REWARD_ORDER_INTERVAL` (`n`, default `5`) and `REWARD_DISCOUNT_PERCENT` (`x`, default `10`). They are read when the application starts.

## API

All amounts are decimal strings in JSON, expressed in the currency's two-decimal minor unit. No authentication is implemented. Paths under `/admin` are administrative operations and are intentionally unprotected for this assignment.

| Method and path | Request | Success | Important errors |
|---|---|---|---|
| `GET /products` | None | `200` product IDs, names, current unit prices, and available stock | — |
| `POST /carts` | None | `201` new open cart | — |
| `GET /carts/{cart_id}` | None | `200` cart lines and gross total | `404 cart_not_found` |
| `POST /carts/{cart_id}/items` | `{"product_id":"p-001","quantity":2}` | `201` updated cart; quantity adds to any existing line | `404` unknown cart/product; `409` checked-out cart or current stock limit; `422` invalid quantity |
| `PUT /carts/{cart_id}/items/{product_id}` | `{"product_id":"p-001","quantity":2}` | `200` cart with line quantity replaced | `400 product_id_mismatch`; `404` missing cart/product/line; `409` stock limit or checked-out cart; `422` invalid quantity |
| `DELETE /carts/{cart_id}/items/{product_id}` | None | `200` updated cart | `404` missing cart/line; `409 cart_checked_out` |
| `POST /carts/{cart_id}/checkout` | `Idempotency-Key: <client token>` header; optional `{"coupon_code":"..."}` | `200` immutable order snapshot including gross, discount, and net totals | `404` missing cart/coupon; `409` empty/already-checked-out cart, unavailable stock/coupon, or conflicting idempotency key; `400` blank key; `422` missing/overlong key |
| `GET /orders/{order_id}` | None | `200` immutable order and line snapshots | `404 order_not_found` |
| `POST /admin/coupons/generate` | None | `201` one newly generated coupon for the oldest unrewarded reached milestone | `409 coupon_not_eligible` or `coupon_already_generated` |
| `GET /admin/report` | None | `200` order, quantity, revenue, and coupon aggregates | — |

Domain errors use `{"error":{"code":"...","message":"..."}}`. Validation errors retain FastAPI's standard `422` format.

Cart responses contain `id`, `status`, `items` (`product_id`, `name`, `quantity`, `unit_price`, `line_total`), and `gross_total`. Order responses contain `id`, `cart_id`, `idempotency_key`, `coupon_code`, `created_at`, immutable `lines` (`product_id`, `product_name`, `quantity`, `unit_price`, `line_total`), and `gross_total`, `discount_total`, and `net_total`. The report contains `successfully_purchased` rows (`product_id`, `product_name`, `quantity`), `gross_revenue`, `total_discounts`, `net_revenue`, `coupons_generated`, `coupons_available`, `coupons_redeemed`, and `total_orders`.

### Checkout/retry examples

```sh
curl -X POST http://localhost:8000/carts
curl -X POST http://localhost:8000/carts/<cart-id>/items \
  -H 'Content-Type: application/json' \
  -d '{"product_id":"p-001","quantity":2}'
curl -X POST http://localhost:8000/carts/<cart-id>/checkout \
  -H 'Idempotency-Key: order-attempt-2026-001' \
  -H 'Content-Type: application/json' \
  -d '{"coupon_code":null}'
```

Send the same key when retrying the same checkout. The stored order is returned without another inventory decrement. Reusing the key for another cart is a `409`.

## Tests

```sh
.venv/bin/python -m pytest -q
```

The standard suite uses isolated SQLite databases for fast endpoint and rollback tests; SQLite is not used to claim row-level concurrency guarantees. To run the PostgreSQL locking test, point `TEST_DATABASE_URL` at a disposable PostgreSQL database where the test user can create/drop schemas:

```sh
TEST_DATABASE_URL='postgresql+psycopg://checkout:checkout@localhost:5432/checkout' \
  .venv/bin/python -m pytest -q tests/test_postgres_concurrency.py
```

The concurrency test creates a uniquely named temporary schema, checks three competing checkouts against two units, and submits the same idempotency key concurrently.

## Operational notes

This assignment uses `Base.metadata.create_all()` at startup rather than a migration system. The service assumes PostgreSQL's default `READ COMMITTED` isolation level. It does not integrate a payment processor; a checkout that passes all validations is treated as paid, so no external side effect can escape a rolled-back database transaction.
