# CredWeave Roadmap

This document outlines the planned capabilities and feature roadmap for **CredWeave v0.1.0**.

---

## Foundation & Architectural Skeleton
- [x] Clean Architecture layer separation (`domain`, `application`, `infrastructure`, public API).
- [x] Generic immutable domain models (`Credential`, `Lease`, `Outcome`).
- [x] Strict secret value redaction in `repr()` and `str()` (`***`).
- [x] Application port protocols (`Clock`, `CredentialSource`, `SelectionStrategy`, `StateStore`).
- [x] Public API exports and versioning (`__version__ = "0.1.0"`).
- [x] Modern PEP 517/621 packaging with `hatchling` and zero runtime dependencies.
- [x] Comprehensive test suite, strict linting (`ruff`), and strict typing (`mypy`).

---

## Selection & Scheduling Strategies
- [x] `RoundRobinStrategy`: Even cyclic rotation among eligible credentials.
- [x] `WeightedStrategy`: Traffic allocation based on credential weights or quota capacities.
- [x] `LeastUsedStrategy`: Prioritizing credentials with lowest total usage count.
- [x] `LeastRecentlyUsedStrategy` (LRU): Maximizing recovery and idle time between uses.
- [x] `FailoverStrategy`: Strict priority-based cascading fallback groups.
- [x] `RandomStrategy`: Randomized selection with optional weights.

---

## Cooldown, Health & Backoff Engine
- [x] Automatic state transitions (`AVAILABLE` -> `RATE_LIMITED` / `COOLDOWN` / `UNHEALTHY`).
- [x] Fixed cooldown durations and exponential backoff with jitter.
- [x] Respecting upstream `retry_after` hints from provider responses.
- [x] Consecutive failure thresholds and automatic isolation of broken keys.
- [x] Automatic recovery when cooldown expires; probe eligibility is modeled as lifecycle state only (CredWeave never issues probe requests).

---

## Concurrency & Lease Management
- [x] Thread-safe and `asyncio`-safe in-memory state tracking (`MemoryStateStore`).
- [x] Dual synchronous (`pool.acquire_sync()`, `pool.report_sync()`) and asynchronous (`await pool.acquire()`, `await pool.report()`) ergonomics.
- [x] Per-credential concurrency caps (limiting in-flight parallel leases): a pool-wide `max_concurrency_per_credential` default with a per-credential `max_concurrency` metadata override, enforced by an atomic slot reservation in the state store.
- [x] Lease timeout tracking and automatic reclamation of orphaned leases: with `lease_timeout` set, expired leases release their concurrency slot automatically during acquire/report (or explicitly via `reclaim_expired_leases()`), without applying an outcome or changing credential health.

---

## Credential Sources & Dynamic Hot Reload
- [x] `StaticSource`: Static programmatic list of credentials.
- [x] `EnvSource`: Ingesting credentials and secret key-pairs from environment variables (configured with variable names, never values).
- [ ] `JsonSource` & `YamlSource`: Structured file-based credential ingestion. `JsonSource` is implemented (stdlib only); `YamlSource` is not, because the standard library has no YAML parser.
- [x] Pull-based hot reload for `EnvSource` and `JsonSource`: changes are detected on every `get_credentials()` (file `stat` fingerprint / environment reread), rotated credentials are served to future leases under their stable id without recreating the pool, and a bad reload keeps the last known good credentials. Reusable for future file-based sources via `FileReloader`.
- [ ] Background (push-based) hot reload: a watcher thread/task that detects file or environment changes without waiting for the next `get_credentials()` call.

---

## State Persistence & Distributed Coordination
- [ ] `SQLiteStateStore`: Embedded persistent store for long-lived single-node services.
- [ ] `RedisStateStore`: Distributed state storage for multi-worker and Kubernetes deployments.
- [ ] Distributed lease coordination and cluster-wide cooldown synchronization.

---

## Cloud Secret Managers & Client Middleware
- [ ] AWS Secrets Manager source adapter.
- [ ] GCP Secret Manager source adapter.
- [ ] HashiCorp Vault source adapter.
- [ ] Optional client middleware / interceptors for `httpx` and `aiohttp`.
