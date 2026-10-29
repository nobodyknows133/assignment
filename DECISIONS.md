# Decisions

## Invariants

- An order exists at most once per cart and at most once per idempotency key.
- Stock is nonnegative, and an order's quantity is deducted exactly once in the same transaction that creates its order.
- A coupon is available or redeemed; a coupon can be redeemed by at most one order.
- Failed checkout leaves the cart open, stock unchanged, and any supplied coupon available.
- Order line names, quantities, unit prices, line totals, gross, discount, and net are stored as historical facts.
- Report totals derive only from committed orders and coupons.

## Decision: PostgreSQL transactions and row-level locks

**Context:** Independent requests may compete for the last units of a product, a cart, or a coupon.

**Options considered:** In-process mutexes, optimistic updates with retry, or database transactions with row locks.

**Choice:** A single SQLAlchemy transaction locks the cart, all referenced product rows in sorted ID order, and (if supplied) the coupon with `SELECT ... FOR UPDATE`. Constraints additionally enforce unique cart orders, idempotency keys, coupon milestones/codes, and coupon redemption.

**Why:** Database locks coordinate concurrent workers sharing PostgreSQL; an in-process lock would not. Sorting product IDs reduces deadlock risk when carts overlap.

**Consequences:** Concurrent requests may wait on the same row. The database is required for the guarantees; SQLite tests validate basic behavior but cannot validate `FOR UPDATE`.

## Decision: Idempotency and checked-out carts

**Context:** A client may time out after the database commits and retry.

**Options considered:** Make checkout naturally repeatable with a cart token, require a caller key, or return conflict for all repeats.

**Choice:** Require `Idempotency-Key`, globally unique in the orders table. A matching key and cart returns the original order; the key used for a different cart returns `409`. Any checkout attempt on an already checked-out cart with another key returns `409`. A unique-constraint race is resolved by reading the committed matching order after rollback.

**Why:** The key handles retries across process restarts, while one-order-per-cart prevents a client from bypassing retry protection by changing keys.

**Consequences:** Clients must persist and reuse the key for retries. Keys are retained indefinitely with orders in this assignment; production retention policy would need to respect retry windows and order history.

## Decision: Cart price and stock semantics

**Context:** Cart contents do not reserve inventory and product prices can change before checkout.

**Options considered:** Reserve stock/price at add time, snapshot cart prices, or use live catalog state at checkout.

**Choice:** Adding/updating checks that the requested quantity is currently available but reserves nothing. Cart views show current catalog prices until checkout. Checkout revalidates stock and charges the then-current price, recording it in the order. A checked-out cart view uses its order snapshot.

**Why:** This avoids abandoned carts holding stock or prices while ensuring a visible failure if availability changed and an auditable final price.

**Consequences:** A cart may become unavailable after it was viewed; checkout returns `409 insufficient_inventory`. A price change is reflected in the final order rather than silently preserving the earlier display price.

## Decision: Coupon milestone and availability rules

**Context:** Generation timing, missed milestones, and coupon scope were unspecified.

**Options considered:** Automatically generate on checkout, allow one coupon for the latest milestone only, or let an administrator generate eligible milestones on demand.

**Choice:** After `n`, `2n`, ... committed orders, an admin request generates exactly one coupon for the oldest reached milestone that lacks one. If several milestones were missed, subsequent requests generate the next one. Generated coupons have no expiry, are globally redeemable once, apply to one order's entire pre-discount subtotal, and are not restricted by customer or minimum purchase.

**Why:** This gives the administrator explicit control and avoids losing a milestone if generation was delayed. A unique milestone constraint and a lock on the latest committed order serialize generator requests.

**Consequences:** Coupon creation is not automatic and eligibility is based on current committed order count. Authentication, expiry, usage limits by customer, and coupon revocation are deferred.

## Decision: Atomic checkout and payment boundary

**Context:** Inventory and coupon state must not be consumed if payment or validation fails.

**Options considered:** Call a payment provider inline, implement a payment abstraction, or model checkout as paid with no external integration.

**Choice:** Treat a successful database checkout as payment success. Validation, order creation, inventory decrement, cart transition, and coupon redemption are one database transaction.

**Why:** The assignment requires no real payment integration, and introducing a remote payment call would create a distributed transaction problem disproportionate to this service. There are no external side effects to compensate after rollback.

**Consequences:** The service cannot represent payment declines or asynchronous payment states. A real integration should use a payment state machine and transactional outbox/saga; it must not hold database locks while calling a provider.

## Decision: Money, discounts, and report calculations

**Context:** Floating-point arithmetic can produce inconsistent totals.

**Options considered:** Binary floats, integer minor units, or decimal database values and Python `Decimal`.

**Choice:** Prices and stored totals use `NUMERIC` and `Decimal`. Inputs in the catalog are two-decimal values. Line totals and discounts are rounded to cents with `ROUND_HALF_UP`; the coupon percentage is applied once to the gross order subtotal. Discount is capped at gross, and net is never negative.

**Why:** This is deterministic and explainable in stored snapshots and API responses.

**Consequences:** Currency is fixed to a generic two-decimal unit; multi-currency and currencies with different minor units are not modeled.

## Decision: Errors and administrative routes

**Context:** API clients need to distinguish validation, missing resources, and state conflicts.

**Options considered:** Generic errors, HTTP status only, or structured domain error codes.

**Choice:** Domain failures return stable `error.code` and human-readable `error.message`, using `404` for missing resources, `409` for state/resource conflicts, and FastAPI `422` for request schema validation. `/admin/*` is explicitly administrative but unauthenticated.

**Why:** Clients can branch on stable codes without parsing text, while the implementation remains small.

**Consequences:** Authentication, authorization, rate limiting, and error correlation IDs are not implemented and must be added before public deployment.

## Implemented and deferred

Implemented: product seeding, carts and item management, stock checks, immutable order line snapshots, idempotent checkout, row-locked coupon redemption, administrator milestone generation, reporting, and endpoint/rollback/PostgreSQL concurrency tests.

Deferred intentionally: user accounts and cart ownership, real payments, migrations, multi-currency, tax/shipping, inventory adjustment endpoints, coupon expiration and eligibility conditions, authentication/authorization, audit/event history, observability, API rate limiting, and automated cleanup/retention. The assignment's timebox favored transaction and retry correctness over those production concerns.

## Multiple service instances and production evolution

The core invariants already reside in PostgreSQL, so multiple app instances can share the database without a process-local coordination mechanism. At production scale I would add a migration tool, connection-pool sizing and timeouts, deadlock/serialization retry policy for safely idempotent operations, separate admin authorization, structured telemetry, and load tests. If payment is introduced, checkout should transition through explicit pending/paid/failed states and publish external work through an outbox; payment must not be called while holding inventory locks. For higher write volume, I would review stock row contention, index/query plans, and possibly use conditional atomic stock updates with row-count checks while retaining unique constraints and transaction boundaries.

## AI tool use

I used Copilot (an AI assistant in VS Code) to help draft and review the service structure, transaction flow, tests, and documentation. I reviewed the generated code and changed the testing strategy rather than treating an in-memory SQLite lock test as concurrency evidence: SQLite does not enforce PostgreSQL `SELECT FOR UPDATE`, so the suite separates quick SQLite behavior tests from an opt-in PostgreSQL test using concurrent independent sessions. I also reviewed the transaction paths to ensure invalid coupons roll back stock and order creation together.

## If given another two hours

I would first run the PostgreSQL concurrency test repeatedly under a real local PostgreSQL instance and add competing-coupon and simultaneous-generator scenarios. Next I would add Alembic migrations and a clear startup readiness check, then review isolation/lock behavior under injected transaction failures and enforce a bounded retry policy only for retryable database errors.
