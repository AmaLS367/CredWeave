# Changelog

## 0.1.0 — first release

CredWeave is a provider-agnostic credential pool for Python: leasing, concurrency caps, cooldown
and health tracking, failover, and secret rotation with generation safety. It has no runtime
dependencies. Supported on Python 3.10 to 3.13 in CI, and additionally tested on 3.14.

### Features

- `CredentialPool` with synchronous (`acquire_sync`, `report_sync`) and asynchronous (`acquire`,
  `report`) APIs over one shared state.
- Lease model with outcomes: `success`, `rate_limited`, `quota_exhausted`, `transient_error`,
  `auth_failed`, `permanent_failure`, `consecutive_failures_exceeded`.
- Cooldowns with fixed or exponential backoff, optional jitter, and upstream `retry_after`.
- Per-credential concurrency caps (`max_concurrency_per_credential`, or the `max_concurrency`
  metadata override) and lease timeouts (`lease_timeout`) with automatic slot reclamation.
- Selection strategies: round-robin, least-used, least-recently-used, weighted, random, failover.
- Credential sources: `StaticSource`, `EnvSource` (environment variable names only), and
  `JsonSource` (strict schema, pull-based reload with last-known-good semantics).
- `MemoryStateStore`, thread-safe and usable from several event loops.
- Secret handling: secret values are masked in `repr`/`str`, metadata is deeply immutable, and
  rotation detection uses a per-process keyed HMAC fingerprint that is never persisted.

### Behavior to know before upgrading from a development snapshot

These describe the released behavior. They are the rules integrators most often trip over.

- **Rotation is not blindly trusted.** A secret the store has never seen is adopted only by a pool
  that is advancing from the secret it last observed. A pool built over a store that already holds a
  different secret gets no leases from its unseen secret until `pool.authorize_secret(credential_id)`
  is called. Rotations are never adopted from a stale or reverted source.
- **Rotation does not recover a credential.** A `REVOKED` or `UNHEALTHY` credential stays out of
  rotation after its secret changes. Recover it with `pool.authorize_secret(credential_id)` after
  repairing the secret in the source.
- **Rolling back needs `authorize_secret`.** After a source reverts to an earlier secret, the pool
  refuses that secret. A later forward rotation is not adopted automatically either. Call
  `authorize_secret` once the source holds the intended secret.
- **Outcomes for superseded secrets are discarded.** A lease granted under an earlier secret frees
  its concurrency slot, but its outcome (for example `auth_failed`) does not change the credential.
- **`reset_credential` clears `REVOKED` without a secret check.** It is an explicit administrative
  reset. Prefer `authorize_secret` after a secret change.
- **Late reports are rejected.** Reporting a lease after its `lease_timeout` raises
  `LeaseExpiredError` and applies no outcome.
- **Custom state stores must implement the generation contract.** `CredentialPool` raises
  `ConfigurationError` at construction if `sync_credential` or the `secret_fingerprint` argument of
  `reserve_lease` is missing. The async registry methods are part of the protocol too.
- **Construction reads the source once.** An unreadable source raises `CredentialSourceError` from
  the constructor, so no pool starts with an unknown baseline.
- **`JsonSource` is strict.** Unknown fields, duplicate keys, non-string secrets, and `NaN` are
  rejected. A malformed file after startup keeps the last good credentials and reports the error on
  `reload_status`.

### Operational limitations

- **State is per process.** `MemoryStateStore` is not shared across processes or restarts. A
  distributed store must replace the per-process fingerprint key with a shared one; otherwise no
  process can recognise a secret another process adopted.
- **Unreported leases hold slots.** Without `lease_timeout`, a lease that is never reported keeps its
  concurrency slot until the process exits. With a cap, the credential can then stay unavailable.
- **Reclaimed-lease memory is bounded.** The store remembers the last 4096 reclaimed leases. Older
  ones, if reported late, raise `InvalidLeaseError` instead of `LeaseExpiredError`.
- **Rotation history is bounded.** The store remembers 1024 secret fingerprints per credential.
  Beyond that it refuses unseen fingerprints automatically until `authorize_secret` is called.
- **Blocking reads inside the pool lock.** Synchronous acquires read the source while holding the
  pool lock. An asynchronous acquire on the event loop can wait briefly for that lock while a file
  source is being read on another thread.
- **Free-text masking is best effort.** Known secret values of three or more characters are redacted
  from reasons and diagnostics, along with recognised token patterns. Shorter values and unrecognised
  formats are not guaranteed to be masked.
- **Windows file replacement.** Replacing a credentials file that a reader has open can fail for the
  writer with a sharing violation. The reader keeps the last good snapshot and recovers on the next
  successful read.
- **Value objects are not hashable.** `Lease`, `Outcome` and `CredentialRecord` raise `TypeError` on
  `hash()`. Use `lease.lease_id` as the key. `Credential` is hashable by id.
- **No YAML source.** `YamlSource` needs a parser the standard library does not provide.
