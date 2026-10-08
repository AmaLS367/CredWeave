# CredWeave Architecture & Design Guide

## 1. System Boundary & Core Invariants

CredWeave is a provider-agnostic, protocol-independent credential lifecycle engine. Its responsibility is managing credential pools, scheduling, rotation, cooldowns, and health states.

### Architectural Invariants
1. **Lifecycle Decoupling:** CredWeave manages credentials and their state transitions. It never performs network calls or external provider requests itself.
2. **Protocol Agnosticism:** The core contains no HTTP, REST, gRPC, or provider-specific abstractions.
3. **Inward Dependency Direction:** Domain primitives have zero dependencies on infrastructure, frameworks, or storage backends.

---

## 2. Clean Architecture & Layering

CredWeave adheres to Clean Architecture adapted pragmatically for an idiomatic Python library. Dependencies strictly flow inward:

```text
       ┌────────────────────────────────────────────────────────┐
       │                       Public API                       │
       │                   (credweave package)                  │
       └───────────────────────────┬────────────────────────────┘
                                   │
                                   ▼
       ┌────────────────────────────────────────────────────────┐
       │                Infrastructure / Adapters               │
       │              (Clocks, Sources, Stores)                 │
       └───────────────────────────┬────────────────────────────┘
                                   │
                                   ▼
       ┌────────────────────────────────────────────────────────┐
       │                   Application Layer                    │
       │           (Services, Ports / Protocols)                │
       └───────────────────────────┬────────────────────────────┘
                                   │
                                   ▼
       ┌────────────────────────────────────────────────────────┐
       │                      Domain Layer                      │
       │               (Models, Outcomes, Errors)               │
       └────────────────────────────────────────────────────────┘
```

### The Inward Dependency Rule

1. **Domain (`credweave.domain`):**
   - Contains enterprise entities, value objects, domain enums, and domain errors.
   - Zero dependencies on any external framework, cloud SDK, database, or HTTP library.
   - Pure Python standard library only.
2. **Application (`credweave.application`):**
   - Coordinates use cases, lease lifecycle, and defines **Ports** (interfaces/protocols) for external dependencies:
     - `Clock` (timekeeping & delays)
     - `CredentialSource` (credential ingestion & hot reload)
     - `SelectionStrategy` (scheduling algorithms)
     - `StateStore` (persistence of state & cooldowns, plus the lease registry that backs concurrency caps and lease reclamation)
   - Contains orchestration services like `CredentialPool`.
3. **Infrastructure (`credweave.infrastructure`):**
   - Implements application ports with specific technologies (e.g. system clock, SQLite/Redis state stores, environment variable and JSON file sources, cloud secret managers).
   - Pluggable and optional; can be swapped without altering domain logic.
4. **Public API (`credweave`):**
   - Re-exports clean, typed, stable public interfaces. Hides internal modules (`_internal`).

---

## 3. Core Architectural Decisions

### 3.1 Generic Structured Credentials vs. Provider-Specific Models

Rather than creating distinct classes for `OpenAIApiKey`, `AwsCredentials`, `SshKeyPair`, or `CookieAuth`, CredWeave models credentials generically via `Credential`:

```python
Credential(
    id="modal-team-primary",
    secrets={
        "token_id": "ak_live_xyz123",
        "token_secret": "sk_live_secret789",
    },
    metadata={
        "account": "analytics-team",
        "tier": "enterprise",
        "region": "us-east-1",
    },
)
```

**Why this decision?**
1. **Universal Applicability:** A credential is fundamentally a named identity possessing secret material required to authenticate, along with metadata describing its capabilities.
2. **Zero Upstream Coupling:** Adding a new cloud provider or AI platform requires zero changes or releases in CredWeave.
3. **Multi-token support:** Accommodates key-secret pairs, session cookies, OAuth refresh tokens, mTLS certificates, or bearer tokens identically.

### 3.2 Strict Separation of Secrets and Metadata

`Credential` separates its payload into two explicit domains:
- **`secrets`:** Sensitive data required by the target service.
  - Redacted from all string formatting (`repr`, `str`).
  - Wrapped in read-only mapping proxies to prevent mutation.
- **`metadata`:** Diagnostic, scheduling, and descriptive attributes (provider name, tenant, region, tier, concurrency limits).
  - Safe for diagnostic inspection, logging, and strategy filtering.

### 3.3 The Core Does Not Understand HTTP

CredWeave deliberately contains no HTTP parsing, socket handling, or networking code.

**Why?**
1. Credentials are used across diverse protocols: REST/HTTP, gRPC, WebSocket, database connections, message queues, and proprietary binary RPCs.
2. HTTP clients vary widely (`httpx`, `aiohttp`, `requests`, `urllib3`, `curl_cffi`). Hardcoding one would bloat dependencies and cause version conflicts.
3. The application executing the call understands the protocol and knows how to map its responses to domain outcomes.

### 3.4 Why `Outcome` Exists Instead of Raw Status Codes

Rather than accepting raw HTTP status codes (like `429` or `401`), callers report an `Outcome` value object:

```python
outcome = Outcome.rate_limited(
    retry_after=60.0,
    reason="Provider rate limit exceeded on endpoint /v1/chat/completions",
)
await pool.report(lease, outcome)
```

**Why?**
- **Protocol Independence:** Different services convey throttling differently (e.g. HTTP 429, gRPC `RESOURCE_EXHAUSTED`, database connection errors, or JSON response bodies like `{"error": "rate_limit"}`).
- **Granular Classification:** An HTTP 400 could mean client payload error (not credential-related), or it could mean an invalid project ID tied to that specific key. The calling application determines whether an error reflects on the credential's health.
- **Rich Context:** An `Outcome` can carry recommended `retry_after` durations, categorized root causes, and non-secret telemetry.

### 3.5 The Acquire / Report Lease Lifecycle

Credential scheduling follows an explicit **lease** pattern:

```mermaid
sequenceDiagram
    autonumber
    actor App as Application
    participant Pool as CredentialPool
    participant Strategy as SelectionStrategy
    participant Store as StateStore
    participant Provider as External Provider API

    App->>Pool: acquire()
    Pool->>Store: get_all_records()
    Store-->>Pool: credential runtime states
    Pool->>Strategy: select(candidates, context)
    Strategy-->>Pool: selected candidate
    Pool->>Store: reserve_lease(credential_id, lease_id, max_concurrency)
    Store-->>Pool: slot claimed atomically (or refused: at capacity)
    Pool-->>App: Lease(credential, lease_id, acquired_at)

    Note over App: App invokes external provider<br/>using lease.credential.secrets

    alt Provider call succeeds
        App->>Pool: report(lease, Outcome.success())
        Pool->>Store: settle_lease(lease_id, success)
    else Provider reports 429 / Rate Limited
        App->>Pool: report(lease, Outcome.rate_limited(retry_after=30))
        Pool->>Store: settle_lease(lease_id, rate_limited)<br/>(state -> RATE_LIMITED, cooldown_until=t+30)
    else Provider reports Auth Failed / Expired
        App->>Pool: report(lease, Outcome.auth_failed(reason="Revoked"))
        Pool->>Store: settle_lease(lease_id, auth_failed)<br/>(state -> REVOKED)
    end
```

**Benefits of Leases:**
- Concurrency tracking: The state store tracks how many active leases exist for each credential and enforces optional per-credential caps (see 3.5.1).
- Leak prevention: With `lease_timeout`, expired or un-reported leases are reclaimed automatically and release their concurrency slot (see 3.5.2).
- Idempotent reporting: Each report is linked to a specific execution lease ID.

#### 3.5.1 Per-Credential Concurrency Caps

A cap limits how many leases of one credential may be in flight at once. `None` (the default) means unlimited.

- `CredentialPool(max_concurrency_per_credential=N)` sets the pool-wide default.
- A credential's non-secret `max_concurrency` metadata overrides it (an explicit `None` there means unlimited for that credential).
- Zero, negative, `bool`, string and other non-integer limits raise `ConfigurationError`. Limits on the initial credentials are validated at construction; limits on credentials supplied by a dynamic source are validated when they are first considered for a lease. `EnvSource` and `JsonSource` additionally validate the `max_concurrency` metadata when they load it, so an invalid value can never reach (and break) `acquire()` through a reload.

Enforcement is **atomic and lives in the `StateStore` port**, not in the pool: `reserve_lease()` checks the credential's in-flight count and claims the slot in a single step, so the cap holds across threads, asyncio tasks, mixed sync/async callers and any number of pools sharing one store. The pool never performs a read-check-write sequence.

- A credential at its cap is *temporarily ineligible*: it is simply not offered to the strategy. Its health state, failure counter and cooldown are untouched, so round-robin, failover and the other strategies naturally route around it and return to it as soon as a slot frees up.
- If a strategy picks a credential whose last slot is taken by a concurrent acquirer before the reservation lands, the pool refreshes its snapshot, excludes that credential for the rest of the call and selects again. If nothing is left it raises `NoCredentialsAvailableError`.

#### 3.5.2 Lease Timeouts and Automatic Reclamation

Every lease registered in the store carries its own deadline (`acquired_at + lease_timeout`, or none when no timeout is configured). A lease whose deadline has passed and which was never reported is *orphaned*; reclaiming it releases its concurrency slot.

- **Automatic:** every `acquire` and `report` (sync and async) first reclaims expired leases. No background thread or event-loop task is involved, so there is nothing to start, stop or leak.
- **Explicit:** `pool.reclaim_expired_leases()` / `await pool.reclaim_expired_leases_async()` do the same on demand and return the reclaimed `LeaseRecord`s, e.g. to free capacity eagerly or to log orphans.
- Reclamation applies **no** outcome (neither success nor failure) and leaves credential health exactly as it was; it only decrements in-flight accounting.
- It is exactly-once and idempotent: the lease is removed from the registry in the same atomic step that decrements the counter, so a lease can never be released twice and counters cannot go negative, whether the release came from a reclaim, a late report, or a race between the two.
- A reclaimed lease can no longer be reported: `report` raises `LeaseExpiredError` and applies nothing. (The store remembers a bounded number of recently reclaimed lease ids to distinguish those from never-issued ones, which raise `InvalidLeaseError`.)
- Because deadlines are stored with the leases and the registry is shared, a pool without `lease_timeout` still frees capacity held by expired leases of another pool on the same store.
- Reporting is retry-safe: `settle_lease()` computes the new state before it mutates anything, so if the store raises, the lease stays registered and can be reported again without leaking or double-releasing a slot.

### 3.6 Equality and Hashing Semantics

`Credential` equality and hashing are strictly based on the credential's `id`:
```python
def __eq__(self, other: object) -> bool:
    if not isinstance(other, Credential):
        return NotImplemented
    return self.id == other.id


def __hash__(self) -> int:
    return hash((Credential, self.id))
```
**Rationale:**
Within a managed pool, a credential is an entity whose identity is uniquely determined by its identifier (e.g. `"prod-primary-openai"`). Even if its secrets are rotated in place, it represents the exact same identity for tracking metrics, cooldowns, and leases.

### 3.7 Build Backend Decision: Hatchling

CredWeave uses **Hatchling** (`hatchling.build`) as its PEP 517/621 build backend:
- **Simplicity:** Pure declarative configuration inside `pyproject.toml` with zero extraneous setup files (`setup.py`, `setup.cfg`).
- **Standard `src/` layout support:** Hatchling auto-discovers packages under `src/` without fragile custom discovery flags.
- **Typing compliance:** Flawlessly bundles `py.typed` without legacy `MANIFEST.in` requirements.
- **Fast, modern, reproducible:** Avoids legacy setuptools build hooks and deprecation warnings.

### 3.8 Lifecycle Engine: Cooldown, Health & Backoff Rules

All credential-state business rules live in `LifecycleEngine` (`credweave.application.services.lifecycle`) and the pure `BackoffPolicy` value object (`credweave.domain.backoff`). The engine is a side-effect-free function from `(CredentialRecord, Outcome, now)` to a new `CredentialRecord`: no I/O, no locking, no clock reads, no networking. A `StateStore` only loads a record, calls the engine and persists the result atomically, so SQLite/Redis stores reuse the exact same rules as `MemoryStateStore`.

| Outcome | Result | Counts as health failure |
|---|---|---|
| `SUCCESS` | Resets failure progression; never overrides a stronger state set by another in-flight lease | no |
| `RATE_LIMITED` | `RATE_LIMITED` until `retry_after` (or the policy base delay if absent) | no |
| `QUOTA_EXHAUSTED` | `QUOTA_EXHAUSTED` until `retry_after`, indefinite if absent | no |
| `TRANSIENT_ERROR` | `COOLDOWN` for the backoff delay; `UNHEALTHY` once `max_consecutive_failures` is reached | yes |
| `PERMANENT_FAILURE` | `UNHEALTHY` | yes |
| `AUTH_FAILED` | `REVOKED` | yes |

**Backoff:** `delay(n) = min(max_delay, base_delay * multiplier ** (n - 1))` for the n-th consecutive failure (`multiplier=1` is a fixed cooldown). Optional proportional `jitter` in `[0, 1]` removes a random share of the capped delay, so `max_delay` stays a hard ceiling. Randomness is an injectable `RandomSource` (e.g. `random.Random(seed).random`) for deterministic tests.

**`retry_after`:** the upstream hint is never shortened or capped. With `RetryAfterMode.FLOOR` (default for explicit policies) a failure cools down for `max(policy_delay, retry_after)`; with `RetryAfterMode.OVERRIDE` the hint replaces the policy delay. Pools created without an explicit `backoff` use a fixed `default_cooldown` in `OVERRIDE` mode, which preserves the pre-engine behavior. Rate limits and quota exhaustion are throttling, not health signals, so for them the hint is exact and the failure progression is neither consulted nor advanced.

**Recovery and probes:** when a timed cooldown elapses the credential returns to `AVAILABLE` with its failure count intact. That window is the half-open "probe" state: the credential is merely eligible again, a further failure escalates the backoff, and a `SUCCESS` resets it. Whether and how to probe a provider stays entirely with the caller; CredWeave performs no network requests.

```python
pool = CredentialPool(
    credentials,
    backoff=BackoffPolicy.exponential(1.0, multiplier=2.0, max_delay=60.0, jitter=0.2),
    max_consecutive_failures=5,
    rng=random.Random(42).random,  # optional: deterministic jitter
)
```

---

### 3.9 Dynamic Credential Sources & Pull-Based Reload

`CredentialPool` re-reads its `CredentialSource` on every selection round and keys all state by the **stable credential id**. A source therefore only has to return a fresh immutable snapshot; no pool reconfiguration is needed.

- **`EnvSource`** is configured with environment variable *names* and rereads the environment on every call. It returns the same `Credential` objects while no value changed. A required variable that is unset or empty raises `CredentialSourceError` (an environment has no half-written intermediate state, so there is no last-known-good fallback).
- **`JsonSource`** reads a strictly validated JSON document (schema in `credweave/infrastructure/sources/json_source.py` and the README). It delegates change detection to the reusable `FileReloader` (`infrastructure/sources/reloading.py`), which a future `YamlSource` can reuse by supplying only a parser.
- **Pull-based, no background thread.** `FileReloader` `stat`-s the file on each read and re-reads only when its fingerprint (mtime, size, inode, device) changed; a short "racy timestamp" window falls back to comparing a content digest. Atomic `rename`/`os.replace` swaps and symlink swaps are detected. The async API runs the file work in a worker thread.
- **Last known good.** A malformed, unreadable or schema-invalid file never replaces the served snapshot; `ReloadStatus` (`JsonSource.reload_status`) reports the secret-safe error. Only the initial load raises.
- **Concurrency.** Refreshes are serialised by a lock and snapshots are immutable tuples swapped atomically, so readers never observe a half-loaded state and generations never go backwards.

Pool semantics under reload:

| Source change | Effect |
| :--- | :--- |
| Same id, new secrets/metadata | Future leases get the new `Credential`; the store record (usage, cooldown, failures) is kept. |
| Id removed | No new leases. Existing leases stay reportable and `active_leases` still shows the object they were granted with. The store record is kept, so re-adding the id restores its history. |
| New id | Eligible immediately; its store record is created lazily. |
| Change between candidate snapshot and `reserve_lease` | The reservation checks the snapshot's secret fingerprint under the store lock. If another pool rotated the credential in between, the candidate is refused (`STALE`) and another one is selected; no lease is ever granted on a superseded secret. |
| Secret rotated back to an earlier value | Refused. The store keeps the newer generation and gives no lease to the earlier secret, so a stale or reverted source cannot reactivate a revoked credential. |
| Secret rotated to a never-seen value | Adopted as the next generation only when the synchronising pool advances from the secret it last observed (`last_observed` equals the current generation). The lifecycle state is not changed: a `REVOKED` or `UNHEALTHY` credential stays out of rotation. |
| Never-seen value from a pool that is not in sync, or a pool built over a store holding another secret | Refused (fail closed). Without trustworthy source revisions an unseen secret cannot be ordered against the current one, so it is not adopted automatically; `authorize_secret` is required. |
| Residual limitation | A single source that rolls back to a never-seen secret while it is in sync with the store is indistinguishable from a legitimate rotation, and is adopted. Only source revisions would separate the two. Across processes, a store shared without a common fingerprint key cannot recognise secrets another process adopted, so no pool there can advance it: every rotation then needs `authorize_secret`. |
| Same-pool interleaving of sync/async acquire and `authorize_secret` | Every generation change is committed under the pool lock from a source read that no other generation change overtook. An asynchronous read taken before such a change is read again; a superseded candidate excludes only its exact secret. The source itself may change at any instant: a generation always reflects a read taken under the lock. |
| Explicit recovery or rollback | `CredentialPool.authorize_secret(id)` reads the source's current secret and makes it the active generation under a new generation number, then resets the credential to `AVAILABLE`. Leases granted under any earlier generation keep their outcomes discarded. Authorization re-reads the source after updating the store and follows it if it moved, so the store never stays on a secret the source has left. |
| Unbounded secret history | The store remembers up to 1024 adopted fingerprints per credential. Once it forgets one, it refuses unseen fingerprints in automatic synchronisation (fail closed) until `authorize_secret` is called; a forgotten secret therefore cannot be replayed in. |
| Pool built over a source that is unreadable | Construction raises `CredentialSourceError`. The source is read once at construction to record the baseline secrets; without that baseline a secret the source briefly reverted to could be adopted as a rotation. |
| Custom store without the generation contract | `CredentialPool` raises `ConfigurationError` at construction if the store has no `sync_credential` or its `reserve_lease` does not accept `secret_fingerprint`. |

## 4. Architecture Diagram

```mermaid
flowchart TD
    subgraph ClientApp["Client Application Space"]
        App["Application Workload"]
        HTTPClient["HTTP / gRPC Client (httpx, requests, aiohttp)"]
    end

    subgraph PublicAPI["Public API Layer"]
        PoolAPI["CredentialPool"]
        CredModel["Credential"]
        OutModel["Outcome"]
        LeaseModel["Lease"]
    end

    subgraph ApplicationLayer["Application Core"]
        CoordService["Pool Coordination Engine"]
        PortSource["CredentialSource Port"]
        PortStrat["SelectionStrategy Port"]
        PortStore["StateStore Port"]
        PortClock["Clock Port"]
    end

    subgraph InfrastructureLayer["Infrastructure Adapters (Future & Present)"]
        EnvSource["EnvSource"]
        FileSource["JsonSource (Hot Reload) / YamlSource (planned)"]
        VaultSource["Cloud Secret Managers (AWS, Vault)"]
        
        MemStore["MemoryStateStore"]
        SqlStore["SQLiteStateStore"]
        RedisStore["RedisStateStore (Distributed)"]
        
        SysClock["SystemClock"]
        TestClock["TestClock / FakeClock"]
    end

    subgraph DomainLayer["Domain Layer (Zero Dependencies)"]
        DomainEntities["Credential & Lease"]
        DomainOutcomes["Outcome & OutcomeType"]
        DomainStates["CredentialState"]
        DomainErrors["CredWeaveError Hierarchy"]
    end

    App --> PoolAPI
    App --> HTTPClient
    PoolAPI --> CoordService
    CoordService --> PortSource
    CoordService --> PortStrat
    CoordService --> PortStore
    CoordService --> PortClock
    CoordService --> DomainEntities

    PortSource -.-> EnvSource
    PortSource -.-> FileSource
    PortSource -.-> VaultSource

    PortStore -.-> MemStore
    PortStore -.-> SqlStore
    PortStore -.-> RedisStore

    PortClock -.-> SysClock
    PortClock -.-> TestClock

    DomainEntities -.-> DomainStates
    DomainEntities -.-> DomainErrors
```

---

## 5. Security & Secret Redaction Guidelines

> [!CAUTION]
> **CRITICAL SECURITY REQUIREMENT FOR ALL CONTRIBUTORS:**
> CredWeave manages sensitive credentials. Under no circumstances should secret values ever be logged, included in exception messages, or exposed via string representation.

1. **`repr()` and `str()` masking:**
   `Credential.__repr__` and `Credential.__str__` must ALWAYS mask secret values:
   ```python
   # Output will be:
   # Credential(id='test', secrets={'api_key': '***'}, metadata={...})
   ```
2. **Exception messages:**
   Exceptions must never format dictionary values or raw secrets into error strings. They may reference parameter names, key names, or credential IDs.
3. **Environment and config isolation:**
   `.env` and other secret configuration files must never be committed to git. Example files (`.env.example`) must contain obvious mock placeholders.
4. **Log hygiene:**
   Never log `lease.credential.secrets`. Use `lease.credential.id` or `lease.credential.metadata` for telemetry and debugging.
