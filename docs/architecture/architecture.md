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
     - `StateStore` (persistence of state & cooldowns)
   - Contains orchestration services like `CredentialPool`.
3. **Infrastructure (`credweave.infrastructure`):**
   - Implements application ports with specific technologies (e.g. system clock, SQLite/Redis state stores, environment variable sources, cloud secret managers).
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
    Pool->>Store: update_state(IN_FLIGHT)
    Pool-->>App: Lease(credential, lease_id, acquired_at)

    Note over App: App invokes external provider<br/>using lease.credential.secrets

    alt Provider call succeeds
        App->>Pool: report(lease, Outcome.success())
        Pool->>Store: update_state(AVAILABLE, record_success)
    else Provider reports 429 / Rate Limited
        App->>Pool: report(lease, Outcome.rate_limited(retry_after=30))
        Pool->>Store: update_state(RATE_LIMITED, cooldown_until=t+30)
    else Provider reports Auth Failed / Expired
        App->>Pool: report(lease, Outcome.auth_failed(reason="Revoked"))
        Pool->>Store: update_state(UNHEALTHY)
    end
```

**Benefits of Leases:**
- Concurrency tracking: The pool tracks how many active leases exist for each credential (preventing exceeding account concurrency limits).
- Leak prevention: Expired or un-reported leases can be swept or returned to the pool after timeouts.
- Idempotent reporting: Each report is linked to a specific execution lease ID.

### 3.6 Equality and Hashing Semantics

`Credential` equality and hashing are strictly based on the credential's `id`:
```python
def __eq__(self, other: object) -> bool:
    if not isinstance(other, Credential):
        return NotImplemented
    return self.id == other.id


def __hash__(self) -> int:
    return hash((self.__class__, self.id))
```
**Rationale:**
Within a managed pool, a credential is an entity whose identity is uniquely determined by its identifier (e.g. `"prod-primary-openai"`). Even if its secrets are rotated in place, it represents the exact same identity for tracking metrics, cooldowns, and leases.

### 3.7 Build Backend Decision: Hatchling

CredWeave uses **Hatchling** (`hatchling.build`) as its PEP 517/621 build backend:
- **Simplicity:** Pure declarative configuration inside `pyproject.toml` with zero extraneous setup files (`setup.py`, `setup.cfg`).
- **Standard `src/` layout support:** Hatchling auto-discovers packages under `src/` without fragile custom discovery flags.
- **Typing compliance:** Flawlessly bundles `py.typed` without legacy `MANIFEST.in` requirements.
- **Fast, modern, reproducible:** Avoids legacy setuptools build hooks and deprecation warnings.

---

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
        FileSource["Json/YamlSource (Hot Reload)"]
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
