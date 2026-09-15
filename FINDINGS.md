# Property Revenue Dashboard — Findings & Fixes

Four defects, all on the path `GET /api/v1/dashboard/summary` → Redis cache → revenue
aggregation. Together they explain the three reports: wrong March totals, revenue
belonging to another company, and totals off by a few cents.

| # | Defect | File | Client impact |
|---|--------|------|---------------|
| 1 | Cache key ignored the tenant | `backend/app/services/cache.py` | Ocean Rentals saw Sunset Properties' revenue |
| 2 | Month bounds computed in UTC, not the property timezone | `backend/app/services/reservations.py` | March total wrong by one booking (1 250.00) |
| 3 | `Decimal` → `float` → `Math.round` pipeline | `backend/app/api/v1/dashboard.py`, `frontend/.../RevenueSummary.tsx` | Totals drifting by cents |
| 4 | DB pool never initialised, silently replaced by hardcoded mock data | `backend/app/core/database_pool.py`, `reservations.py` | Every figure on the dashboard was fake |

---

## Bug 1 — Cross-tenant revenue leak (privacy)

**Symptom.** Ocean Rentals: *"sometimes when we refresh, we see revenue numbers that
look like they belong to another company."*

**Root cause.** `cache.py` keyed the Redis entry on the property id alone:

```python
cache_key = f"revenue:{property_id}"      # before
```

Property ids are only unique *within* a tenant — `database/schema.sql` declares
`PRIMARY KEY (id, tenant_id)`, and `database/seed.sql` gives `prop-001` to **both**
`tenant-a` (Beach House Alpha, Paris) and `tenant-b` (Mountain Lodge Beta, New York).
Whichever client requested `prop-001` first populated the entry; for the next 5 minutes
the other client was served that same entry. The "sometimes" in the report is the
5‑minute TTL.

A second, quieter path to the same leak: `dashboard.py` fell back to a shared
`"default_tenant"` bucket whenever the tenant could not be resolved.

**Fix.**
- `build_cache_key()` → `revenue:{tenant_id}:{property_id}:{period}`.
- The cached payload carries its `tenant_id` and is discarded (and logged as an error)
  if it does not match the caller — defence in depth against a future key bug.
- `dashboard.py` returns **403** instead of falling back to `"default_tenant"`.
- Every SQL statement filters on `tenant_id`; a property that belongs to another tenant
  now returns **404** rather than data.

**Proof.**
```
$ redis-cli KEYS 'revenue:*'
revenue:tenant-a:prop-001:all-time
revenue:tenant-b:prop-001:all-time     # distinct entries

Sunset  prop-001 → 2250.00 (4 bookings, Europe/Paris)
Ocean   prop-001 →    0.00 (0 bookings, America/New_York)
Sunset  prop-005 → HTTP 404            # owned by tenant-b
```

---

## Bug 2 — Month boundaries built in UTC (accuracy)

**Symptom.** Sunset Properties: *"we're showing different totals for March."*

**Root cause.** `calculate_monthly_revenue()` built naive datetimes:

```python
start_date = datetime(year, month, 1)     # before — no tzinfo
end_date   = datetime(year, month + 1, 1)
```

Postgres compares these against `check_in_date`, a `TIMESTAMP WITH TIME ZONE`, so the
naive values were read as UTC. But "March" for a Paris property starts at
`2024-03-01 00:00+01:00` = `2024-02-29 23:00Z`. Seed row `res-tz-1` checks in at
`2024-02-29 23:30Z` — that is **00:30 on 1 March in Paris**, a March booking that the
UTC bounds pushed into February. 1 250.00 disappeared from the client's March report.

The same function also ignored `tenant_id` even though its SQL referenced it — the
cross-tenant hole of Bug 1, again.

**Fix.** `month_bounds_utc()` anchors the bounds in the property's own timezone
(read from `properties.timezone`) using `zoneinfo`, and keeps them timezone-aware so
Postgres normalises both sides correctly. `tzdata` added to `requirements.txt` —
`python:3.11-slim` ships without the IANA database.

**Proof.**
```
Sunset prop-001, March 2024    → 2250.00 (4 bookings)   # includes res-tz-1
Sunset prop-001, February 2024 →    0.00 (0 bookings)
```
The same instant lands in **February** for a New York property — covered by
`test_same_booking_belongs_to_february_in_new_york`.

---

## Bug 3 — Money went through `float` (accuracy)

**Symptom.** Finance: totals *"slightly off by a few cents"*.

**Root cause.** Two rounding mistakes in the same pipeline.

1. Amounts are `NUMERIC(10, 3)` — deliberately sub-cent. Nothing in the code defined
   *when* to round to cents, and rounding per row instead of on the total loses money:
   `333.333 + 333.333 + 333.334` is **1000.00** when rounded once, **999.99** when each
   row is rounded first.
2. `dashboard.py` did `float(revenue_data['total'])` and the frontend then did
   `Math.round(total * 100) / 100`. JSON numbers are IEEE-754 doubles; amounts like
   `1080.40` are not exactly representable, so each hop can shift the last cent.
   The component even shipped a "Precision Mismatch Detected" badge — a warning about
   its own arithmetic.

**Fix.** Money stays `Decimal` from Postgres to the response, is rounded **once** on the
final total with `ROUND_HALF_UP` (not Python's default banker's rounding), and is sent
as a decimal **string** (`"2250.00"`). The frontend formats that string with
`Intl.NumberFormat` on the integer part only — it never builds a `Number` from it.

**Proof.** `test_rounding_each_row_loses_a_cent` pins the 999.99 / 1000.00 difference.

---

## Bug 4 — The dashboard was serving mock data

**Symptom.** None reported — which is the worrying part. Every number the CEO's clients
argued about was invented in Python.

**Root cause.** Two independent failures, both swallowed:

```python
# database_pool.py — these settings do not exist on Settings
database_url = f"...{settings.supabase_db_user}:{settings.supabase_db_password}@..."
```
`AttributeError` on every startup → `session_factory = None`. And even with a working
engine, `get_session()` was an `async def` returning a session, so
`async with db_pool.get_session()` raised `AttributeError: __aenter__`.

`calculate_total_revenue()` caught **every** exception and returned a hardcoded table
(`prop-001 → 1000.00`, `prop-002 → 4975.50`, …) presented as real revenue. Note that the
mock returns the same figure for `prop-001` regardless of tenant — a second source of
the "numbers from another company" report.

**Fix.**
- The pool reads `DATABASE_URL` (already provided by `docker-compose.yml`) and
  normalises it to the `asyncpg` dialect; `initialize()` is idempotent and the module
  singleton is reused instead of building a new engine per request.
- `get_session()` is a real `@asynccontextmanager`.
- The explicit `poolclass=QueuePool` was dropped — an async engine needs an async-aware
  pool, which SQLAlchemy picks by default.
- The mock fallback is gone. A database failure now surfaces as a 500 instead of a
  plausible-looking number. Only Redis errors are tolerated (the cache is optional);
  they are logged and the value is recomputed.

---

## Also removed

`X-Simulated-Tenant`, a client-controlled header sent by `RevenueSummary.tsx` naming the
tenant to read. The backend ignored it, so it was not exploitable — but a request in
which the client names its own tenant is exactly the shape of the bug we just fixed, and
it should not exist in a multi-tenant codebase.

---

## Verifying

```bash
docker compose up --build -d

# unit tests — 12 tests, no extra dependency
docker compose exec backend python -m unittest discover -s tests -v

# end to end
TA=$(curl -s -X POST localhost:8000/api/v1/auth/login -H 'Content-Type: application/json' \
     -d '{"email":"sunset@propertyflow.com","password":"client_a_2024"}' | jq -r .access_token)
TB=$(curl -s -X POST localhost:8000/api/v1/auth/login -H 'Content-Type: application/json' \
     -d '{"email":"ocean@propertyflow.com","password":"client_b_2024"}'  | jq -r .access_token)

curl -s -H "Authorization: Bearer $TA" 'localhost:8000/api/v1/dashboard/summary?property_id=prop-001'
curl -s -H "Authorization: Bearer $TB" 'localhost:8000/api/v1/dashboard/summary?property_id=prop-001'
curl -s -H "Authorization: Bearer $TA" 'localhost:8000/api/v1/dashboard/summary?property_id=prop-001&month=3&year=2024'
```

## API change

`total_revenue` is now a decimal **string** (`"2250.00"`) instead of a JSON number, and
the response carries `period` and `timezone` so a total is always attributable to a
period and a locale. The frontend was updated accordingly.

---

# Second pass — code review follow-up

A review of the first commit found one defect that mattered and a dozen smaller ones.

## Bug 1 was only half fixed

Scoping the cache key and removing the `"default_tenant"` bucket was not enough:
`TenantResolver.resolve_tenant_id()` — untouched by the first commit — ended with

```python
# Default fallback
return "tenant-a"
```

Every email it did not recognise was handed Sunset Properties' tenant. The new 403 guard
in `dashboard.py` was therefore unreachable, and any authenticated user outside the three
seeded accounts read a real client's revenue through a correctly-scoped cache key.

**Fix.** The resolver takes the tenant from the identity itself (`app_metadata` /
`user_metadata`) first, falls back to an explicit map of the seeded demo accounts, and
otherwise returns `None`. There is no default tenant anywhere on the path now.

```
stranger@example.com (valid JWT) → GET /dashboard/summary?property_id=prop-001
before: 200 {"total_revenue":"2250.00"}   # Sunset's revenue
after:  403 {"detail":"No tenant associated with this account"}
```

## Other fixes in this pass

| Area | Fix |
|------|-----|
| `RevenueSummary.tsx` | `error` was never reset, so one 404 pinned the card to the error state for the whole session; added an `ignore` flag so a slow response cannot overwrite a newer property's data; `formatAmount` returns `null` instead of throwing during render on a malformed payload |
| `RevenueSummary.tsx` | The decimal mark was hardcoded to `.` while grouping came from the locale — a German viewer saw `1.000.00`. Both now come from `Intl.NumberFormat` |
| `cache.py` | `json.loads` moved inside the guarded block (a corrupt entry 500'd for a full TTL); the cached payload is now checked on **all three** key components, and key parts are percent-escaped so a `:` in a property id cannot collide two entries |
| `reservations.py` | `COALESCE(currency, 'USD')` — a NULL currency created a phantom second group and then crashed `sorted()`; an unusable `properties.timezone` falls back to UTC with an error log instead of a 500 |
| `main.py` / `database_pool.py` | The pool is initialised and **disposed** in `lifespan`; `close()` takes the lock. Without disposal, `uvicorn --reload` leaked an engine per reload |
| `secureApi.ts` | A half-specified period is forwarded so the backend's 400 surfaces, instead of being silently downgraded to the all-time total |

## Simplifications

- `calculate_monthly_revenue()` was a second implementation of the month-bounds and
  round-once rules. It is now a three-line wrapper over `calculate_total_revenue()`, so
  those rules exist in one place.
- `build_cache_key()`'s period label was inlined twice; extracted to `_period_label()`
  and reused by the cache-validation check.

## Reviewed and deliberately not changed

- **"Merge the timezone lookup and the aggregate into one query."** Correct that it is
  two round-trips. Kept as two: the month bounds stay a pure, unit-tested Python function
  (`month_bounds_utc`), which is worth more on a timezone-correctness fix than saving
  ~1 ms behind a 5-minute cache.
- **"DST-at-midnight breaks the bounds."** `zoneinfo` with `fold=0` already resolves a
  non-existent local midnight to the transition instant — the first instant of the month —
  and an ambiguous one to its first occurrence. Both are the wanted boundary. The real
  half of that finding, an unparseable timezone string, is fixed.
- **`node_modules` (72 348 files) and `frontend/.env` are committed to the repository.**
  Real, and worth raising with the team, but untracking them is a ~72k-file diff that
  would bury this debugging work. `frontend/.env` was checked: it holds two localhost
  URLs, no secrets. `.gitignore` covers only `__pycache__` and says why.
- **The property selector lists all five seeded properties across both tenants**, so two
  of five entries 404 for each client. The honest fix is a tenant-scoped properties
  endpoint, which is outside "do not rebuild the system". The frontend now says
  *"This property is not available for your account"* rather than a generic failure.

---

# Third pass — cleanup

A reuse / simplification / efficiency / altitude review of the two commits above.

## Put things where they belong

- **Tenant resolution now happens in exactly one function.** The metadata-first chain had
  been inlined in `authenticate_request`, so the websocket path (`auth.py`) and
  `/auth/me` still used the old single-step call — the same user could be told a
  different tenant depending on how they connected. It moved into
  `TenantResolver.resolve_tenant_id`, which is now the only place a tenant is minted.
- **`require_tenant` is a shared FastAPI dependency.** The "no default tenant" rule was
  a hand-rolled block inside `/dashboard/summary`. It now lives next to
  `authenticate_request`, so the next tenant-scoped endpoint inherits it instead of
  re-implementing it.
- **`period_label()` has one definition**, in `reservations.py` next to the month-bounds
  logic. The label is both the API's `period` field and part of the cache key; two
  copies of the format string could drift apart silently.
- **The cache uses the app's Redis client.** `services/cache.py` was opening its own
  unpooled `redis.Redis.from_url(os.getenv("REDIS_URL", "...localhost..."))` — a second
  connection, configured from a different source than `settings.redis_url`, never
  health-checked, never closed. It now uses the pooled `core/redis_client` singleton that
  `lifespan` already manages, whose `get`/`set` are already failure-tolerant. That
  deleted the hand-rolled try/except scaffolding *and* the duplicate TTL constant.

## Removed

- `calculate_monthly_revenue()` — no callers, and a second copy of rules that live in
  `calculate_total_revenue`. The broken original is quoted in Bug 2 above; keeping a
  dead wrapper adds nothing.
- `get_db_session()` — a FastAPI dependency with zero callers that read as the canonical
  way to get a session while the real code path used the pool directly.
- The double-checked lock in `DatabasePool.initialize` — asyncio does not preempt between
  the check and the `await`, so the outer check only made a reader look for a subtlety
  that is not there.
- The cache-payload re-validation — unreachable once the key components are escaped, and
  a silent 100 % cache-miss trap if the two label formats ever diverged. The escaping is
  the real guarantee, and it is unit-tested.
- Three unreachable fallbacks in `formatAmount`, already excluded by its `DECIMAL` guard.

## Fixed

- **The startup hook warmed nothing.** `create_async_engine` is lazy, so
  `db_pool.initialize()` in `lifespan` built an object and opened no socket: the first
  user still paid the connect handshake, and a bad `DATABASE_URL` was only discovered
  then. It now opens one connection at boot.
- **404s were recognised by regex on the error message.** `secureApi` collapsed every
  non-401/403 failure into `Error("API request failed: " + detail)`, so the component
  matched `/not found/i` against backend prose. There is now an `ApiError` carrying
  `status`; the component branches on `status === 404`, and 4xx responses are no longer
  retried three times with backoff.
- `Intl.NumberFormat` is constructed once at module scope rather than per render.

## Reviewed and deliberately not changed

- **AUTOCOMMIT / `engine.connect()` instead of a Session**, to skip the `BEGIN`/`ROLLBACK`
  around two read-only SELECTs. Real, but it changes transaction semantics for every
  future user of the pool to save a round-trip behind a 5-minute cache.
- **`asyncio.gather` the timezone and aggregate queries on the all-time path.** Saves one
  RTT, but issues the aggregate before `PropertyNotFound` is known and holds two
  connections per request — complexity in the wrong direction for the gain.
- **Negative-caching `PropertyNotFound`.** Would collapse an id-enumeration burst into one
  query per id per TTL, but adds a second payload shape to the cache for a path that is
  one indexed primary-key lookup.
- **Threading the session in via `Depends(get_db_session)`.** The right long-term shape,
  and it would make the service unit-testable without a live pool — but it changes the
  service signatures and the endpoint's contract, which is past "do not rebuild".
