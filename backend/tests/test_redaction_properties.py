"""Properties of the redaction engine over generated content: where blocks
are cut never changes the output, no planted secret survives (fixed ones
and values generated for every detector, in noise and across long-line
cuts), a Tail window gives the same lines as redacting the whole file, and
no rules means no change."""

import ipaddress
import json
import re
import string
from pathlib import Path

from app.redaction import (
    DETECTOR_NAMES,
    UTF16_UNSURE,
    Redactor,
    redact_bytes,
    redact_window,
)
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

DETECTORS = json.loads(
    (Path(__file__).resolve().parents[1] / "app" / "redaction" / "detectors.json").read_text(
        encoding="utf-8"
    )
)

ALL = [{"id": i, "name": name, "detector": name} for i, name in enumerate(DETECTOR_NAMES)]

SECRETS = [
    "4111 1111 1111 1111",
    "5555-5555-5555-4444",
    "378282246310005",
    "alice@example.com",
    "10.20.30.40",
    "2001:db8::42",
    "GB82 WEST 1234 5698 7654 32",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJl",
]
# (text written to the log, the part that must not survive)
KEYED = [
    ("password=hunter22", "hunter22"),
    ("Authorization: Basic Zm9vOmJhcg==", "Zm9vOmJhcg=="),
    ("Bearer abcdefgh12345678", "abcdefgh12345678"),
    ("https://u:pa55word@host", "pa55word"),
]
PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzj4p4WGe\n"
    "-----END RSA PRIVATE KEY-----"
)
NOISE = st.sampled_from(
    ["", " ", "  ", "\n", "\r\n", "ok", "x=1", "12:30:45", "v1.2", "id 42", "é", "\t", "\xff"]
)
PIECES = st.one_of(
    NOISE,
    st.sampled_from(SECRETS),
    st.sampled_from([text for text, _ in KEYED]),
    st.just(PEM),
    st.text(alphabet="abc 0123456789:.-@\n", max_size=12),
)


def _join(pieces: list[str]) -> bytes:
    # Secrets are separated from what's around them, as in a log line.
    return " ".join(pieces).encode("utf-8", "surrogateescape")


SETTINGS = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@SETTINGS
@given(st.lists(PIECES, max_size=30), st.lists(st.integers(min_value=0), max_size=6))
def test_block_cuts_never_change_the_output(pieces, cuts):
    data = _join(pieces)
    whole, result = redact_bytes(data, ALL, max_line_bytes=64)
    points = sorted({cut % (len(data) + 1) for cut in cuts})
    blocks = [data[a:b] for a, b in zip([0, *points], [*points, len(data)], strict=True)]
    redactor = Redactor(ALL, max_line_bytes=64)
    out = b"".join(redactor.feed(block) for block in blocks) + redactor.finish()
    assert out == whole
    assert redactor.counts == result.counts


@SETTINGS
@given(st.lists(PIECES, max_size=30))
def test_no_planted_secret_survives(pieces):
    data = _join(pieces)
    out, _ = redact_bytes(data, ALL)
    for secret in SECRETS:
        assert secret.encode() not in out
    for _, value in KEYED:
        assert value.encode() not in out
    assert b"MIIBOgIBAAJBAKj34" not in out
    assert out.count(b"\n") == data.count(b"\n")


@SETTINGS
@given(st.lists(PIECES, max_size=30), st.integers(min_value=0))
def test_a_window_matches_the_whole_file(pieces, pick):
    data = _join(pieces)
    starts = [0] + [i + 1 for i, byte in enumerate(data) if byte == 10]
    start = starts[pick % len(starts)]
    whole, _ = redact_bytes(data, ALL)
    head, _ = redact_bytes(data[:start], ALL)
    window = redact_window(data, start, ALL, final=True)
    assert not window.aligned
    assert head + window.data == whole


@SETTINGS
@given(st.binary(max_size=300), st.booleans())
def test_no_rules_change_nothing(data, final):
    out, result = redact_bytes(data, [], final=final)
    assert out == data
    assert result.source_offset == len(data)


def _luhn_digit(payload: str) -> str:
    total = 0
    for position, char in enumerate(reversed(payload)):
        value = int(char)
        if position % 2 == 0:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return str((10 - total % 10) % 10)


@st.composite
def _cards(draw) -> str:
    brand = draw(st.sampled_from(DETECTORS["card_brands"]))
    low, high = draw(st.sampled_from(brand["ranges"]))
    prefix = str(draw(st.integers(int(low), int(high)))).zfill(len(low))
    length = draw(st.sampled_from(brand["lengths"]))
    body = draw(st.text(string.digits, min_size=length - len(prefix) - 1, max_size=length))
    payload = (prefix + body)[: length - 1]
    number = payload + _luhn_digit(payload)
    separator = draw(st.sampled_from(["", " ", "-"]))
    group = draw(st.sampled_from([4, 1]))
    return separator.join(number[i : i + group] for i in range(0, len(number), group))


@st.composite
def _ibans(draw) -> str:
    country, length = draw(st.sampled_from(sorted(DETECTORS["iban_lengths"].items())))
    size = length - 4
    bban = draw(st.text(string.digits + string.ascii_uppercase, min_size=size, max_size=size))
    number = int("".join(str(int(char, 36)) for char in bban + country + "00"))
    iban = f"{country}{98 - number % 97:02d}{bban}"
    if draw(st.booleans()):
        return " ".join(iban[i : i + 4] for i in range(0, len(iban), 4))
    return iban


_ALNUM = string.ascii_letters + string.digits
_UPPER = string.ascii_uppercase + string.digits
_B64URL = _ALNUM + "_-"

# Each strategy gives (text written to the log, what must not be seen in
# clear in the output, with enough context that noise can't produce it by
# chance, the hidden part alone, without the clear words around it like
# `Bearer` or `password=`).
GENERATED = {
    "card": _cards().map(lambda value: (value,) * 3),
    "iban": _ibans().map(lambda value: (value,) * 3),
    "email": st.tuples(
        st.text(_ALNUM, min_size=1, max_size=12),
        st.text(string.ascii_lowercase + string.digits, min_size=1, max_size=10),
        st.text(string.ascii_lowercase, min_size=2, max_size=6),
    ).map(lambda p: (f"{p[0]}@{p[1]}.{p[2]}",) * 3),
    "jwt": st.tuples(
        st.text(_B64URL, min_size=5, max_size=20),
        st.text(_B64URL, min_size=5, max_size=20),
        st.text(_B64URL, max_size=20),
    ).map(lambda p: (f"eyJ{p[0]}.eyJ{p[1]}.{p[2]}",) * 3),
    "ipv4": st.lists(st.integers(0, 255), min_size=4, max_size=4).map(
        lambda octets: (".".join(map(str, octets)),) * 3
    ),
    "ipv6": st.integers(1, 2**128 - 1).map(
        lambda number: (ipaddress.IPv6Address(number).compressed,) * 3
    ),
    "bearer": st.text(_UPPER, min_size=8, max_size=30).map(
        lambda token: (f"Bearer {token}", f"Bearer {token}", token)
    ),
    "authorization": st.text(_UPPER, min_size=4, max_size=30).map(
        lambda value: (f"Authorization: Basic {value}", f"Basic {value}", value)
    ),
    "secret_kv": st.tuples(
        st.sampled_from(["password", "db_password", "api_key", "token", "client_secret"]),
        st.text(_UPPER, min_size=1, max_size=20).filter(
            lambda value: value.lower() not in DETECTORS["kv_ignore"]
        ),
    ).map(lambda p: (f"{p[0]}={p[1]}", f"={p[1]}", p[1])),
    "url_password": st.text(_UPPER, min_size=1, max_size=16).map(
        lambda secret: (f"https://u:{secret}@host", f":{secret}@", secret)
    ),
    "private_key": st.text(_ALNUM + "+/", min_size=8, max_size=60).map(
        lambda body: (
            f"-----BEGIN PRIVATE KEY-----\nMII{body}\n-----END PRIVATE KEY-----",
            f"MII{body}",
            f"MII{body}",
        )
    ),
}
assert set(GENERATED) == set(DETECTOR_NAMES)

GENERATED_VALUES = st.one_of(*GENERATED.values())
SEPARATORS = st.sampled_from([" ", "  ", "\t", "\n", "\r\n", ", ", "; ", " | ", " - "])
DIGIT_NOISE = st.text(string.digits, min_size=1, max_size=8)
GENERATED_NOISE = st.one_of(
    NOISE.filter(lambda piece: piece.strip()),
    DIGIT_NOISE,
    st.lists(DIGIT_NOISE, min_size=1, max_size=40).map(" ".join),
    st.sampled_from(["AB12", "DE12", "-", "x" * 70, "ok\n"]),
)


@st.composite
def _planted(draw) -> tuple[bytes, list[str]]:
    items = draw(st.lists(st.one_of(GENERATED_VALUES, GENERATED_NOISE), max_size=25))
    secrets = [item[1] for item in items if isinstance(item, tuple)]
    parts: list[str] = []
    for item in items:
        if parts:
            parts.append(draw(SEPARATORS))
        parts.append(item[0] if isinstance(item, tuple) else item)
    return "".join(parts).encode(), secrets


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    _planted(),
    st.integers(min_value=64, max_value=600),
    st.lists(st.integers(min_value=0), max_size=5),
)
def test_no_generated_value_survives(planted, max_line, cuts):
    """Values generated for every detector, in noise of separators, digits
    and line breaks, with long lines cut at a random limit and the content
    fed in random blocks: none of them is ever seen in clear."""
    data, secrets = planted
    points = sorted({cut % (len(data) + 1) for cut in cuts})
    blocks = [data[a:b] for a, b in zip([0, *points], [*points, len(data)], strict=True)]
    redactor = Redactor(ALL, max_line_bytes=max_line)
    out = b"".join(redactor.feed(block) for block in blocks) + redactor.finish()
    for secret in secrets:
        assert secret.encode() not in out, secret


_MARKER = re.compile(rb"\[redacted: [^\]]*\]")


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.sampled_from(sorted(set(GENERATED) - {"private_key"})).flatmap(GENERATED.get),
    st.text("~#/| ", min_size=0, max_size=300),
    st.text("~#/| ", min_size=0, max_size=40),
    st.integers(min_value=64, max_value=400),
)
def test_no_piece_of_a_value_survives_a_long_line_cut(item, before, after, max_line):
    """A value anywhere around the long-line limit, in filler with no
    letters or digits: once the markers are taken out, no four characters
    of what must be hidden are left (the clear parts, like `password=`,
    share none with it)."""
    text, _, secret = item
    line = f"{before} {text} {after}\n".encode()
    out, _ = redact_bytes(line, ALL, max_line_bytes=max_line)
    rest = _MARKER.sub(b"", out)
    hidden = secret.encode()
    size = min(4, len(hidden))
    pieces = {hidden[i : i + size] for i in range(len(hidden) - size + 1)}
    pieces = {piece for piece in pieces if any(chr(c).isalnum() for c in piece)}
    for piece in pieces:
        assert piece not in rest, (piece, out)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.sampled_from(sorted(set(GENERATED) - {"private_key"})).flatmap(GENERATED.get),
    st.integers(min_value=0),
    st.text("~#/| ", min_size=0, max_size=40),
    st.integers(min_value=128, max_value=800),
    st.sampled_from(["utf-16-le", "utf-16-be"]),
    st.booleans(),
)
@example(("MT84 MALT 0110 0001 2345 MTLC AST0 01S",) * 3, 36, "", 256, "utf-16-le", False)
@example(("MT84 MALT 0110 0001 2345 MTLC AST0 01S",) * 3, 36, "", 256, "utf-16-be", True)
@example(("4 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 0",) * 3, 35, "", 300, "utf-16-be", False)
@example(("4 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 0",) * 3, 35, "", 300, "utf-16-le", True)
def test_no_piece_of_a_value_survives_a_long_line_cut_in_unsure_utf16(
    item, into, after, max_line, encoding, odd
):
    """The same in UTF-16 content served as bytes (UTF16_UNSURE), where a
    character takes two source bytes, starting at an even or an odd file
    offset, with the limit inside the value: no four characters of what
    must be hidden are left."""
    text, _, secret = item
    before = "~|" * max_line
    before = before[: max(0, max_line // 2 - 1 - into % (len(text) + 1))]
    line = f"{before} {text} {after}\n".encode(encoding)
    origin = 1 if odd else 0
    window = redact_window(
        b"\n" * origin + line,
        origin,
        ALL,
        final=True,
        max_line_bytes=max_line,
        source_encoding=UTF16_UNSURE,
    )
    rest = _MARKER.sub(b"", window.data)
    size = min(4, len(secret))
    pieces = {secret[i : i + size] for i in range(len(secret) - size + 1)}
    pieces = {piece for piece in pieces if any(char.isalnum() for char in piece)}
    for piece in pieces:
        assert piece.encode(encoding) not in rest, (piece, window.data)


@SETTINGS
@given(st.binary(max_size=300), st.lists(st.integers(min_value=0), max_size=4))
def test_arbitrary_bytes_never_break_streaming(data, cuts):
    whole, _ = redact_bytes(data, ALL, final=False, max_line_bytes=50)
    points = sorted({cut % (len(data) + 1) for cut in cuts})
    blocks = [data[a:b] for a, b in zip([0, *points], [*points, len(data)], strict=True)]
    redactor = Redactor(ALL, max_line_bytes=50)
    out = b"".join(redactor.feed(block) for block in blocks) + redactor.finish(final=False)
    assert out == whole
