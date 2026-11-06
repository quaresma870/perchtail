"""Small checks for values read from untrusted JSON (agent messages, audit
metadata), shared so they behave the same everywhere."""


def as_int(value) -> int | None:
    """`value` if it's an int (not a bool, which is an int subclass), else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None
