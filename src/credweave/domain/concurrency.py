"""Per-credential concurrency limit configuration and validation.

A concurrency limit caps how many leases of one credential may be in flight at once.
``None`` means unlimited. Limits come from the pool-wide default
(``CredentialPool(max_concurrency_per_credential=...)``) and may be overridden per credential
through the non-secret ``max_concurrency`` metadata key.
"""

from credweave.domain.errors import ConfigurationError

MAX_CONCURRENCY_METADATA_KEY = "max_concurrency"


def validate_max_concurrency(value: object, description: str) -> int | None:
    """Validate a concurrency limit: ``None`` (unlimited) or an integer of at least 1.

    ``bool`` is rejected explicitly because it is an ``int`` subclass. Messages only ever
    mention the type of a rejected value, never the value itself.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(
            f"{description} must be a positive integer or None, got {type(value).__name__}."
        )
    if value < 1:
        raise ConfigurationError(f"{description} must be at least 1, got {value}.")
    return value
