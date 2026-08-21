"""SOQL/SOSL input handling.

The Salesforce REST API has no bind parameters, so every query is assembled as a string.
This module is the only place escaping and validation happen; nothing else may build
query fragments from caller-supplied values.
"""

from __future__ import annotations

import re

__all__ = [
    "MAX_LITERAL_LENGTH",
    "MAX_SEARCH_TERM_LENGTH",
    "MIN_SEARCH_TERM_LENGTH",
    "UnsafeQueryInput",
    "record_id",
    "soql_literal",
    "sosl_term",
]

MIN_SEARCH_TERM_LENGTH = 2
MAX_SEARCH_TERM_LENGTH = 200
MAX_LITERAL_LENGTH = 4000

# 15-character case-sensitive or 18-character case-safe Salesforce ID.
_RECORD_ID = re.compile(r"\A[a-zA-Z0-9]{15}(?:[a-zA-Z0-9]{3})?\Z")

# C0 controls with no legitimate place in a transcript, minus the ones we escape below.
_DISALLOWED_CONTROL = re.compile(r"[\x00-\x07\x0b\x0e-\x1f\x7f]")

_WHITESPACE_RUN = re.compile(r"\s+")

# Backslash must map first; dict order is preserved but lookup is per-character so it is safe.
_SOQL_ESCAPES = {
    "\\": "\\\\",
    "'": "\\'",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}

_SOSL_RESERVED = frozenset("?&|!{}[]()^~*:\\\"'+-")


class UnsafeQueryInput(ValueError):
    """Raised when a value is rejected before it can reach a query."""


def record_id(value: object, *, field: str = "id") -> str:
    """Validate a Salesforce record ID.

    Interpolating the result is safe by construction: the pattern admits only
    alphanumerics, so no quote, comment, or operator can survive validation.
    """
    if not isinstance(value, str):
        raise UnsafeQueryInput(f"{field} must be a string, got {type(value).__name__}")
    if not _RECORD_ID.match(value):
        raise UnsafeQueryInput(f"{field} is not a valid Salesforce record ID")
    return value


def soql_literal(value: object, *, field: str = "value") -> str:
    """Return a quoted, escaped SOQL string literal, surrounding quotes included."""
    text = _clean_text(value, field=field, max_length=MAX_LITERAL_LENGTH)
    escaped = "".join(_SOQL_ESCAPES.get(char, char) for char in text)
    return f"'{escaped}'"


def sosl_term(value: object, *, field: str = "search term") -> str:
    """Return an escaped SOSL search term for use inside ``FIND {...}``."""
    text = _clean_text(value, field=field, max_length=MAX_SEARCH_TERM_LENGTH)
    collapsed = _WHITESPACE_RUN.sub(" ", text).strip()
    if len(collapsed) < MIN_SEARCH_TERM_LENGTH:
        raise UnsafeQueryInput(
            f"{field} must be at least {MIN_SEARCH_TERM_LENGTH} characters after trimming"
        )
    return "".join(f"\\{char}" if char in _SOSL_RESERVED else char for char in collapsed)


def _clean_text(value: object, *, field: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise UnsafeQueryInput(f"{field} must be a string, got {type(value).__name__}")
    if _DISALLOWED_CONTROL.search(value):
        raise UnsafeQueryInput(f"{field} contains control characters")
    if len(value) > max_length:
        raise UnsafeQueryInput(f"{field} exceeds {max_length} characters")
    return value
