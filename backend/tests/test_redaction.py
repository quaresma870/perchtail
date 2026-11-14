import io
import time

import pytest
from app.config import get_settings
from app.crypto import follow_head_key, follow_head_root_key
from app.redaction import (
    DETECTOR_NAMES,
    MAX_LINE_BYTES,
    UTF16_BE,
    UTF16_LE,
    UTF16_UNSURE,
    RedactionRuleError,
    Redactor,
    UnsupportedEncodingError,
    compile_rules,
    detect_encoding,
    head_digest,
    long_line_label,
    redact_bytes,
    redact_stream,
    redact_window,
    rules_digest,
)

ALL = [{"id": i, "name": name, "detector": name} for i, name in enumerate(DETECTOR_NAMES)]


def rule(name: str) -> list[dict]:
    return [{"id": 1, "name": name, "detector": name}]


def redact(text: str, rules=None, **options) -> str:
    out, _ = redact_bytes(text.encode(), ALL if rules is None else rules, **options)
    return out.decode()


SAMPLE = (
    "2024-05-01 10:20:30 login user=alice@example.com from 10.1.2.3 and 2001:db8::7\n"
    "charge card 4111 1111 1111 1111 ok; iban PT50 0002 0123 1234 5678 9015 4\n"
    "GET /x Authorization: Bearer abcdefgh12345678\n"
    "dsn=postgres://app:Sup3rS3cret@db:5432/x password=hunter2\n"
    "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
    "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U\n"
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
)
SECRETS = [
    "alice@example.com",
    "10.1.2.3",
    "2001:db8::7",
    "4111 1111 1111 1111",
    "PT50 0002 0123 1234 5678 9015 4",
    "abcdefgh12345678",
    "Sup3rS3cret",
    "hunter2",
    "eyJhbGciOiJIUzI1NiJ9",
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW",
]


def test_no_secret_survives_with_every_detector():
    out = redact(SAMPLE)
    for secret in SECRETS:
        assert secret not in out
    assert out.count("\n") == SAMPLE.count("\n")
    assert "1111" in out  # the last four digits of the card


@pytest.mark.parametrize(
    "detector, text",
    [
        ("card", "imei 490154203237518"),
        ("card", "ts 1700000000000"),
        ("card", "order 1234567890123456"),
        ("ipv6", "time 10:20:30"),
        ("ipv6", "mac de:ad:be:ef:00:01"),
        ("ipv6", "std::string and ::Class"),
        ("ipv4", "1.2.3.400"),
        ("iban", "GB82 WEST 1234 5698 7654 33"),
        ("iban", "XX82 WEST 1234 5698 7654 32"),
        ("jwt", "eyJhbGciOiJIUzI1NiJ9"),
        ("email", "user@localhost"),
        ("private_key", "-----BEGIN PUBLIC KEY-----"),
        ("secret_kv", "password=none"),
        ("secret_kv", "token=[redacted: token]"),
        ("bearer", "bearer abc"),
    ],
)
def test_negative_examples(detector, text):
    assert redact(text, rule(detector)) == text


@pytest.mark.parametrize(
    "detector, text, expected",
    [
        ("card", "x 6011111111111117 y", "x [redacted: card ****1117] y"),
        ("card", "x 3530111333300000 y", "x [redacted: card ****0000] y"),
        ("card", "x 30569309025904 y", "x [redacted: card ****5904] y"),
        ("card", "x 2223003122003222 y", "x [redacted: card ****3222] y"),
        ("ipv6", "fe80::1: error", "[redacted: ip]: error"),
        ("ipv4", "255.255.255.0", "[redacted: ip]"),
        ("email", "a.b+c@sub.example.org.", "[redacted: email]."),
        ("secret_kv", "PRIVATE_KEY='abc'", "PRIVATE_KEY='[redacted: private key]'"),
        ("secret_kv", "accessKey: AKIA1", "accessKey: [redacted: access key]"),
        ("authorization", 'authorization="Basic x"', 'authorization="[redacted: authorization]"'),
    ],
)
def test_positive_examples(detector, text, expected):
    assert redact(text, rule(detector)) == expected


def test_secret_group_keeps_the_rest_of_the_match():
    rules = [{"id": 1, "name": "pin", "pattern": r"pin=(\d+)", "secret_group": 1}]
    assert redact("a pin=1234 b", rules) == "a pin=[redacted: pin] b"


def test_custom_rule_anchors_are_per_line():
    rules = [{"id": 1, "name": "head", "pattern": r"^SECRET \w+$"}]
    assert redact("SECRET a\nx SECRET b\nSECRET c", rules) == (
        "[redacted: head]\nx SECRET b\n[redacted: head]"
    )


def test_empty_matches_are_ignored():
    rules = [{"id": 1, "name": "empty", "pattern": "x*"}]
    assert redact("abc\n", rules) == "abc\n"


@pytest.mark.parametrize(
    "rules",
    [
        [{"id": 1, "name": "x", "detector": "nope"}],
        [{"id": 1, "name": "x", "pattern": "(a"}],
        [{"id": 1, "name": "x", "pattern": "(a)\\1"}],
        [{"id": 1, "name": "x", "pattern": "a", "secret_group": 1}],
        [{"id": 1, "detector": "card"}],
        [{"id": 1, "name": "x"}],
    ],
)
def test_rules_the_engine_cant_run_are_refused(rules):
    with pytest.raises(RedactionRuleError):
        compile_rules(rules)


def test_compiled_rules_are_cached():
    assert compile_rules(ALL) is compile_rules([dict(r) for r in ALL])


def test_rules_digest():
    assert rules_digest(ALL) == rules_digest([dict(r) for r in ALL])
    assert rules_digest(ALL) != rules_digest(ALL[:1])


def test_no_rules_is_byte_for_byte():
    data = bytes(range(256)) * 10 + b"\npassword=x\npartial"
    out, result = redact_bytes(data, [], final=False)
    assert out == data
    assert result.source_offset == len(data)
    assert result.trailing_partial == 0


def test_default_long_line_marker():
    assert long_line_label(MAX_LINE_BYTES) == "line over 1 MiB"
    line = b"a" * 1000 + b" " + b"b" * MAX_LINE_BYTES + b" password=x\nnext password=y\n"
    out, result = redact_bytes(line, rule("secret_kv"))
    assert out == (
        b"a" * 1000 + b"[redacted: line over 1 MiB]\nnext password=[redacted: password]\n"
    )
    assert result.counts == {"secret_kv": 1}


def test_a_value_at_the_long_line_limit_is_not_kept_in_part():
    """The cases from the review: a card or an email cut by the limit used
    to keep the digits or the local part before the cut."""
    card = b"x" * 52 + b" 4111 1111 1111 1111\n"
    email = b"x" * 50 + b" alice.secret@example.com\n"
    for line in (card, email):
        out, _ = redact_bytes(line, ALL, max_line_bytes=64)
        assert out == b"[redacted: line over 64 bytes]\n"


def test_unsure_utf16_window_matches_the_whole_file():
    """UTF16_UNSURE readings are aligned on the file's even offsets, so a
    window that starts on an odd offset reads the same units."""
    data = "一 a\npassword=hunter2\nmail bob@example.com\n".encode("utf-16-le")
    assert detect_encoding(data[:64]) == UTF16_UNSURE
    whole, _ = redact_bytes(data, ALL, source_encoding=UTF16_UNSURE)
    assert b"h\x00u\x00n\x00t\x00e\x00r\x00" not in whole
    assert b"b\x00o\x00b\x00@\x00" not in whole
    for start in [i + 1 for i, byte in enumerate(data) if byte == 10]:
        head, _ = redact_bytes(data[:start], ALL, source_encoding=UTF16_UNSURE)
        window = redact_window(data, start, ALL, final=True, source_encoding=UTF16_UNSURE)
        assert not window.aligned
        assert head + window.data == whole, start


def test_memory_stays_bounded():
    redactor = Redactor(ALL, max_line_bytes=64 * 1024)
    line = b"2024 password=secret value " + b"x" * 100 + b"\n"
    block = line * 2000
    for _ in range(20):
        redactor.feed(block)
        assert len(redactor._buffer) < len(line)
    for _ in range(200):
        redactor.feed(b"y" * 32 * 1024)
        assert len(redactor._buffer) <= 64 * 1024
    redactor.finish()


def test_redact_stream():
    source = io.BytesIO(SAMPLE.encode() * 50)
    destination = io.BytesIO()
    result = redact_stream(source, destination, ALL, chunk_size=1000)
    out, expected = redact_bytes(SAMPLE.encode() * 50, ALL)
    assert destination.getvalue() == out
    assert result.counts == expected.counts
    assert result.output_size == len(out)
    assert result.source_size == len(SAMPLE.encode()) * 50


def test_incomplete_line_counts_are_not_kept():
    redactor = Redactor(rule("secret_kv"), max_line_bytes=32)
    out = redactor.feed(b"ok\ntoken=abc " + b"z" * 40)
    out += redactor.finish(final=False)
    assert redactor.counts == {}
    assert out[: len(out) - redactor.trailing_partial] == b"ok\n"
    assert redactor.source_offset == 3


def test_window_after_the_start_of_the_file_without_context_aligns():
    result = redact_window(b"value password=x\nnext\n", 0, rule("secret_kv"), base_offset=10)
    assert result.aligned
    assert result.begin == 17
    assert result.data == b"next\n"


def test_window_with_no_newline_returns_nothing():
    result = redact_window(b"password=abcdef", 3, rule("secret_kv"), base_offset=10)
    assert result.data == b""
    assert result.begin == result.end == 15


def test_utf16_detection():
    assert detect_encoding("password=x\n".encode("utf-16-le")) == UTF16_LE
    assert detect_encoding("password=x\n".encode("utf-16-be")) == UTF16_BE
    assert detect_encoding("﻿é".encode("utf-16-le")) == UTF16_LE
    assert detect_encoding(b"plain ascii") is None
    assert detect_encoding(b"") is None
    assert detect_encoding(b"a\x00") is None
    with pytest.raises(UnsupportedEncodingError):
        detect_encoding("ab".encode("utf-32"))


def test_utf16_without_matches_round_trips():
    text = "﻿line one ção €\r\nline two 𝄞\r\n".encode("utf-16-le")
    out, _ = redact_bytes(text, ALL, source_encoding=UTF16_LE, output_encoding="source")
    assert out == text
    out, _ = redact_bytes(text, ALL, source_encoding=UTF16_LE)
    assert out == text.decode("utf-16-le").encode("utf-8")


def test_utf16_odd_cut_and_lone_surrogate():
    text = "a password=x\n".encode("utf-16-le") + b"\x00\xd8" + "\n".encode("utf-16-le") + b"z"
    redactor = Redactor(rule("secret_kv"), source_encoding=UTF16_LE)
    out = b"".join(redactor.feed(text[i : i + 3]) for i in range(0, len(text), 3))
    out += redactor.finish()
    assert out == "a password=[redacted: password]\n�\n�".encode()


def test_unsupported_source_encoding():
    with pytest.raises(UnsupportedEncodingError):
        Redactor(ALL, source_encoding="utf-32")


def test_head_digest_changes_with_the_key():
    assert head_digest(b"a" * 32, b"start", 5) != head_digest(b"b" * 32, b"start", 5)
    assert head_digest(b"a" * 32, b"start", 5) == head_digest(b"a" * 32, b"start+more", 5)
    assert len(head_digest(b"a" * 32, b"start", 5)) == 32


@pytest.fixture
def credential_key(monkeypatch, tmp_path):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "test-key-for-redaction")
    monkeypatch.setenv("CREDENTIAL_SALT_PATH", str(tmp_path / "salt" / "credential_salt"))
    get_settings.cache_clear()
    follow_head_root_key.cache_clear()
    yield
    get_settings.cache_clear()
    follow_head_root_key.cache_clear()


def test_follow_head_key_is_per_source_and_stable(credential_key):
    first = follow_head_key(1)
    assert len(first) == 32
    assert follow_head_key(1) == first
    assert follow_head_key(2) != first
    follow_head_root_key.cache_clear()
    assert follow_head_key(1) == first


def test_follow_head_key_depends_on_the_credential_key(credential_key, monkeypatch):
    first = follow_head_key(1)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "another-key")
    get_settings.cache_clear()
    follow_head_root_key.cache_clear()
    assert follow_head_key(1) != first


def test_throughput_regression():
    """A wide bound, to catch a 10x regression, not to measure: the real
    numbers belong to the load tests (#277)."""
    data = SAMPLE.encode() * (5 * 1024 * 1024 // len(SAMPLE))
    started = time.monotonic()
    redact_stream(io.BytesIO(data), io.BytesIO(), ALL)
    assert time.monotonic() - started < 30
