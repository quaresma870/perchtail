"""The verifications a built-in detector runs on each regex match before it
is redacted: a match that fails its check is left as it is. They take the
tables from detectors.json, which the agent's Go engine embeds too, so both
engines decide the same way (the shared vectors in
agent/internal/redact/testdata/vectors.json pin every edge case)."""

import ipaddress
from collections.abc import Mapping, Sequence

_DIGITS = frozenset(b"0123456789")


def luhn_ok(digits: bytes) -> bool:
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = char - 48
        if position % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


_BRAND_TABLES: dict[int, tuple[Sequence[Mapping], dict[str, tuple[int, ...]]]] = {}


def _brand_table(brands: Sequence[Mapping]) -> dict[str, tuple[int, ...]]:
    """The card lengths a number may have, by its first four digits: every
    brand prefix in the table is at most four digits long."""
    cached = _BRAND_TABLES.get(id(brands))
    if cached is not None and cached[0] is brands:
        return cached[1]
    table: dict[str, tuple[int, ...]] = {}
    for number in range(10000):
        prefix = f"{number:04d}"
        lengths: set[int] = set()
        for brand in brands:
            for low, high in brand["ranges"]:
                if int(low) <= int(prefix[: len(low)]) <= int(high):
                    lengths.update(brand["lengths"])
        if lengths:
            table[prefix] = tuple(sorted(length for length in lengths if 12 <= length <= 19))
    _BRAND_TABLES[id(brands)] = (brands, table)
    return table


def card_spans(value: bytes, brands: Sequence[Mapping]) -> list[tuple[int, int, str]]:
    """The card numbers inside a `card` match, as (start, end, last four
    digits) relative to the match.

    The match is a whole sequence of digits with single spaces or hyphens
    between them, however long. Every window of 12 to 19 digits in it that
    starts and ends at a separator or at an end of the sequence is tried
    (a window never splits a run of digits written together, so a 15-digit
    IMEI is not read as the 13-digit card at its start): a window is a card
    when its separators are all of one kind (only spaces or only hyphens),
    its check digit is valid (Luhn) and it has the prefix and length of a
    known brand (so IMEIs and 13-digit millisecond timestamps are left
    alone).
    Windows that overlap are replaced together, labelled with the last four
    digits of the replaced stretch. Digits written next to a card
    ("12 4111 1111 1111 1111 2", or a long row of counters before it) never
    hide it. Linear in the length of the sequence: at most 8 windows per
    digit, each checked in constant time."""
    positions = [index for index, char in enumerate(value) if char in _DIGITS]
    count = len(positions)
    if count < 12:
        return []
    digits = bytes(value[index] for index in positions)
    # spaces[k] / hyphens[k]: separators of each kind between digits 0..k.
    spaces = [0] * count
    hyphens = [0] * count
    for k in range(1, count):
        gap = positions[k] - positions[k - 1]
        separator = value[positions[k] - 1] if gap == 2 else None
        spaces[k] = spaces[k - 1] + (separator == 32)
        hyphens[k] = hyphens[k - 1] + (separator == 45)
    # Luhn sums by prefix: plain[q][k] adds digits at even/odd indexes as
    # they are, doubled[q][k] adds them doubled (minus 9 over 9).
    plain = [[0] * (count + 1), [0] * (count + 1)]
    doubled = [[0] * (count + 1), [0] * (count + 1)]
    for k in range(count):
        digit = digits[k] - 48
        twice = digit * 2 - 9 if digit > 4 else digit * 2
        for q in (0, 1):
            plain[q][k + 1] = plain[q][k] + (digit if k % 2 == q else 0)
            doubled[q][k + 1] = doubled[q][k] + (twice if k % 2 == q else 0)
    # A run boundary before digit k (k starts a run) / after it (k ends one).
    starts_run = [k == 0 or positions[k] - positions[k - 1] > 1 for k in range(count)]
    ends_run = [k == count - 1 or starts_run[k + 1] for k in range(count)]
    table = _brand_table(brands)
    windows: list[tuple[int, int]] = []
    for first in range(count - 11):
        if not starts_run[first]:
            continue
        lengths = table.get(digits[first : first + 4].decode("ascii"))
        if not lengths:
            continue
        for length in lengths:
            end = first + length  # one past the last digit
            if end > count:
                break
            last = end - 1
            if not ends_run[last]:
                continue
            if spaces[last] - spaces[first] and hyphens[last] - hyphens[first]:
                continue
            # The check digit (index last) is not doubled, the one before is.
            q = last % 2
            total = plain[q][end] - plain[q][first] + doubled[1 - q][end] - doubled[1 - q][first]
            if total % 10 == 0:
                windows.append((first, last))
    found: list[tuple[int, int, str]] = []
    for first, last in windows:
        if found and positions[first] < found[-1][1]:
            start, end, _ = found[-1]
            if positions[last] + 1 > end:
                found[-1] = (start, positions[last] + 1, digits[last - 3 : last + 1].decode())
            continue
        found.append((positions[first], positions[last] + 1, digits[last - 3 : last + 1].decode()))
    return found


def iban_length(value: bytes, lengths: Mapping[str, int]) -> int | None:
    """How many bytes of an `iban` match are the IBAN (the match can run on
    into a word that follows it), or None when it isn't one: a known
    country, that country's length and the mod-97 check."""
    country = value[:2].decode("ascii")
    expected = lengths.get(country)
    if expected is None:
        return None
    compact = bytearray()
    end = 0
    for index, char in enumerate(value):
        if char == 32:
            continue
        compact.append(char)
        end = index + 1
        if len(compact) == expected:
            break
    if len(compact) != expected:
        return None
    if end < len(value) and value[end] != 32:
        return None
    rearranged = bytes(compact[4:] + compact[:4]).decode("ascii")
    number = "".join(str(int(char, 36)) for char in rearranged)
    if int(number) % 97 != 1:
        return None
    return end


def _ipv6_ok(value: bytes) -> bool:
    try:
        ipaddress.IPv6Address(value.decode("ascii"))
    except ValueError:
        return False
    return True


def ipv6_length(value: bytes, following: bytes) -> int | None:
    """How many bytes of an `ipv6` match are the address, or None when it
    isn't one. The match is the longest run of hex groups and colons; a
    single colon at its end is punctuation (`fe80::1: error`). An address
    has to be valid (so times like 12:30:45 and MACs aren't) and must not
    run on into a word (`::Class`). `following` is the byte after the
    match, empty at the end of the line."""
    if not _ipv6_ok(value):
        if not value.endswith(b":") or value.endswith(b"::"):
            return None
        following = b":"
        value = value[:-1]
        if not _ipv6_ok(value):
            return None
    if following and (following.isalnum() or following == b"_"):
        return None
    return len(value)


def kv_value_ok(value: bytes, ignore: Sequence[str]) -> bool:
    """A `secret_kv` value worth redacting: not a placeholder (`null`,
    `none`, only asterisks) and not already redacted."""
    lowered = value.lower()
    if lowered.decode("latin-1") in ignore:
        return False
    if lowered.startswith(b"[redacted"):
        return False
    return value.strip(b"*") != b""


def key_label(key: bytes, labels: Sequence[Mapping], default: str) -> str:
    """The label of a `secret_kv` replacement, from its key: `db_password`
    gives `password`, `X-Api-Key` gives `api key`."""
    lowered = key.lower().decode("latin-1")
    for entry in labels:
        if any(fragment in lowered for fragment in entry["contains"]):
            return entry["label"]
    return default
