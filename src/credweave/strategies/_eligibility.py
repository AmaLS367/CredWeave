"""Shared candidate eligibility filtering and configuration validation for strategies.

Every built-in strategy funnels its candidates through :func:`select_eligible` so that
availability, required tags and preferred metadata are interpreted identically.
"""

import math
from collections.abc import Mapping, Sequence
from typing import Any

from credweave.application.ports.strategy import CredentialCandidate, SelectionContext
from credweave.domain.errors import ConfigurationError


def _candidate_tags(candidate: CredentialCandidate) -> frozenset[str]:
    tags = candidate.credential.get_metadata("tags")
    if isinstance(tags, str):
        return frozenset({tags})
    if isinstance(tags, (set, frozenset, list, tuple)):
        return frozenset(str(t) for t in tags)
    return frozenset()


def lookup_metadata(candidate: CredentialCandidate, key: str) -> tuple[bool, Any]:
    """Find ``key`` in credential metadata, then runtime metadata; returns (found, value)."""
    if key in candidate.credential.metadata:
        return True, candidate.credential.metadata[key]
    if key in candidate.metadata:
        return True, candidate.metadata[key]
    return False, None


def _matches_preferred(candidate: CredentialCandidate, preferred: Mapping[str, Any]) -> bool:
    for key, expected in preferred.items():
        found, actual = lookup_metadata(candidate, key)
        if not found or actual != expected:
            return False
    return True


def select_eligible(
    candidates: Sequence[CredentialCandidate],
    context: SelectionContext | None = None,
) -> list[CredentialCandidate]:
    """Return the candidates a strategy may choose from, preserving input order.

    Rules:
        1. Only ``AVAILABLE`` candidates are eligible.
        2. ``context.required_tags`` is a hard filter: every tag must be present in the
           credential's ``tags`` metadata.
        3. ``context.preferred_metadata`` is a soft filter: if at least one remaining
           candidate matches *all* preferred key/value pairs (looked up in credential
           metadata, then runtime metadata), only those candidates are returned;
           otherwise all remaining candidates are returned unchanged.
    """
    eligible = [c for c in candidates if c.is_available]
    if context is None:
        return eligible

    if context.required_tags:
        required = context.required_tags
        eligible = [c for c in eligible if required <= _candidate_tags(c)]

    if context.preferred_metadata and eligible:
        preferred = context.preferred_metadata
        matching = [c for c in eligible if _matches_preferred(c, preferred)]
        if matching:
            return matching
    return eligible


def validate_metadata_key(value: object, parameter: str) -> str:
    """Validate a metadata key name used by a strategy."""
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{parameter} must be a non-empty string.")
    return value


def validate_weight(value: object, description: str) -> float:
    """Validate a weight: a finite, strictly positive real number (bool is rejected)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{description} must be a number, got {type(value).__name__}.")
    weight = float(value)
    if not math.isfinite(weight) or weight <= 0:
        raise ConfigurationError(f"{description} must be a finite number greater than 0.")
    return weight


def candidate_weight(candidate: CredentialCandidate, key: str, default: float) -> float:
    """Read and validate a candidate's weight from its metadata, falling back to ``default``."""
    found, raw = lookup_metadata(candidate, key)
    if not found:
        return default
    description = f"Weight metadata {key!r} on credential {candidate.credential_id!r}"
    return validate_weight(raw, description)


def validate_priority(value: object, description: str) -> int:
    """Validate a priority: an integer (bool is rejected). Lower values rank first."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{description} must be an integer, got {type(value).__name__}.")
    return value
