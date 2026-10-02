"""AgentCore Platform v1.0"""

# FIN-C2-196 - caller-input validation primitives.
#
# Every value a caller can influence passes through one of the helpers in this
# module before any node acts on it. Three rules are enforced here so that no
# individual node has to remember them:
#
#   1. Numbers must be FINITE and in range. `float("nan")` and `float("inf")`
#      parse successfully, and every comparison against NaN is False — so a NaN
#      threshold silently disables the filter it was supposed to tighten, which
#      is a fail-OPEN on exactly the decision this template exists to make. Both
#      values also arrive through ordinary JSON request bodies, because Python's
#      json module accepts the bare literals `NaN`, `Infinity` and `-Infinity`.
#      Booleans are rejected too: `True` is an int in Python and would otherwise
#      silently mean 1.
#
#   2. Strings that are rendered back into the report are locked to an inert
#      identifier alphabet. Free text supplied by a caller and echoed into the
#      output is caller-controlled output injection.
#
#   3. Rejection is FAIL-CLOSED and names the FIELD, never the value — an error
#      message that quotes the rejected input is a second echo channel.

import math
import re
from typing import Any, Dict, Optional, Tuple

# Inert identifier alphabet for caller strings that reach the rendered report.
_INERT_IDENTIFIER_RE = re.compile(r"^[a-z0-9_]{1,32}$")


class CallerInputError(ValueError):
    """A caller-supplied field failed validation. Carries the field name only."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"caller field '{field}' rejected: {reason}")


def finite_in_range(
    value: Any,
    *,
    field: str,
    minimum: float,
    maximum: float,
    integer: bool = False,
) -> float:
    """Parse a caller number, rejecting anything non-finite or out of range.

    Rejects, in order: booleans (an int in disguise), non-numeric values,
    NaN / +Infinity / -Infinity, and magnitudes outside [minimum, maximum].
    Raises CallerInputError naming the field; never returns a sentinel, so a
    caller cannot get a silently substituted value.
    """
    if isinstance(value, bool):
        raise CallerInputError(field, "boolean is not a number")
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise CallerInputError(field, "empty string is not a number")
        try:
            parsed = float(stripped)
        except (TypeError, ValueError):
            raise CallerInputError(field, "not a number") from None
    elif isinstance(value, (int, float)):
        parsed = float(value)
    else:
        raise CallerInputError(field, "not a number")

    if not math.isfinite(parsed):
        raise CallerInputError(field, "must be a finite number")
    if parsed < minimum or parsed > maximum:
        raise CallerInputError(field, "outside the permitted range")
    if integer:
        if parsed != int(parsed):
            raise CallerInputError(field, "must be a whole number")
        return float(int(parsed))
    return parsed


def inert_identifier(value: Any, *, field: str) -> str:
    """Validate a caller string that will be rendered into the report.

    Only lowercase letters, digits and underscore, 1-32 characters. Anything
    else is rejected rather than escaped: an identifier slot is not a place to
    negotiate with free text.
    """
    if not isinstance(value, str):
        raise CallerInputError(field, "must be a string")
    candidate = value.strip()
    if not _INERT_IDENTIFIER_RE.match(candidate):
        raise CallerInputError(field, "must be 1-32 characters of [a-z0-9_]")
    return candidate


def one_of(value: Any, allowed: Tuple[str, ...], *, field: str) -> str:
    """Validate a caller string against a closed set of permitted values."""
    if not isinstance(value, str):
        raise CallerInputError(field, "must be a string")
    candidate = value.strip().lower()
    if candidate not in allowed:
        raise CallerInputError(field, "not one of the permitted values")
    return candidate


def mask_field_name(name: Any) -> str:
    """Render an unrecognised caller field name safely for an error message.

    Unknown field names are caller-controlled strings. They are reported as a
    masked shape (length and whether the name is inert) rather than echoed, so
    a hostile field name cannot ride out through the error path.
    """
    if not isinstance(name, str):
        return f"<non-string field name: {type(name).__name__}>"
    if _INERT_IDENTIFIER_RE.match(name.strip()):
        return name.strip()
    return f"<masked field name, {len(name)} chars>"


def optional(mapping: Dict[str, Any], key: str) -> Optional[Any]:
    """Return mapping[key] when present and not None, else None."""
    if key not in mapping:
        return None
    value = mapping[key]
    return None if value is None else value
