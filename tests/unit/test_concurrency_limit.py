"""Unit tests for per-credential concurrency limit validation."""

import pytest

from credweave.domain.concurrency import (
    MAX_CONCURRENCY_METADATA_KEY,
    validate_max_concurrency,
)
from credweave.domain.errors import ConfigurationError


def test_metadata_key_name() -> None:
    assert MAX_CONCURRENCY_METADATA_KEY == "max_concurrency"


@pytest.mark.parametrize("value", [1, 2, 10, 10_000])
def test_valid_limits_pass_through(value: int) -> None:
    assert validate_max_concurrency(value, "limit") == value


def test_none_means_unlimited() -> None:
    assert validate_max_concurrency(None, "limit") is None


@pytest.mark.parametrize(
    "value",
    [0, -1, -100, True, False, "2", "", 1.0, 2.5, float("inf"), float("nan"), [1], {}, object()],
)
def test_invalid_limits_raise_configuration_error(value: object) -> None:
    with pytest.raises(ConfigurationError, match="my-limit"):
        validate_max_concurrency(value, "my-limit")


def test_error_message_never_echoes_non_numeric_values() -> None:
    with pytest.raises(ConfigurationError) as exc_info:
        validate_max_concurrency("sk-super-secret", "limit")
    assert "sk-super-secret" not in str(exc_info.value)
