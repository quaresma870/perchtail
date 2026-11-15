"""The vectors shared with the agent's Go engine
(agent/internal/redact/testdata/vectors.json): both engines must give the
same output, counts and offsets, whatever block sizes the input comes in."""

import base64
import json
import re
from pathlib import Path

import pytest
from app.redaction import (
    UTF16_UNSURE,
    RedactionRuleError,
    Redactor,
    UnsupportedEncodingError,
    compile_rules,
    detect_encoding,
    head_digest,
    long_line_label,
    marker,
    redact_window,
    rules_digest,
)

REPO = Path(__file__).resolve().parents[2]
VECTORS_PATH = REPO / "agent" / "internal" / "redact" / "testdata" / "vectors.json"
VECTORS = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
DETECTORS_PATH = REPO / "backend" / "app" / "redaction" / "detectors.json"
AGENT_DETECTORS_PATH = REPO / "agent" / "internal" / "redact" / "detectors.json"


def _bytes(vector: dict, key: str) -> bytes:
    if f"{key}_b64" in vector:
        return base64.b64decode(vector[f"{key}_b64"])
    return vector[key].encode(vector.get(f"{key}_encoding", "utf-8"))


def _options(vector: dict, data: bytes) -> dict:
    options = {}
    encoding = vector.get("source_encoding")
    if encoding == "auto":
        encoding = detect_encoding(data[:64])
    if encoding is not None:
        options["source_encoding"] = encoding
    for key in ("output_encoding", "max_line_bytes", "pem_max_lines"):
        if key in vector:
            options[key] = vector[key]
    return options


def _run(vector: dict, blocks: list[bytes]) -> tuple[bytes, Redactor]:
    data = b"".join(blocks)
    redactor = Redactor(vector["rules"], **_options(vector, data))
    out = b"".join(redactor.feed(block) for block in blocks)
    out += redactor.finish(vector.get("final", True))
    return out, redactor


def _splits(data: bytes):
    yield [data]
    for cut in range(len(data) + 1):
        yield [data[:cut], data[cut:]]
    for size in (1, 7, 4096):
        yield [data[i : i + size] for i in range(0, len(data), size)] or [b""]


STREAM_VECTORS = [v for v in VECTORS["vectors"] if "window" not in v]
WINDOW_VECTORS = [v for v in VECTORS["vectors"] if "window" in v]


@pytest.mark.parametrize("vector", STREAM_VECTORS, ids=[v["name"] for v in STREAM_VECTORS])
def test_vector(vector):
    data = _bytes(vector, "input")
    if "error" in vector:
        with pytest.raises(UnsupportedEncodingError) as raised:
            _run(vector, [data])
        assert raised.value.code == vector["error"]
        return
    expected = _bytes(vector, "expected")
    for blocks in _splits(data):
        out, redactor = _run(vector, blocks)
        assert out == expected, [len(block) for block in blocks]
        assert redactor.counts == vector["counts"]
        assert redactor.source_size == len(data)
        if "source_offset" in vector:
            assert redactor.source_offset == vector["source_offset"]
        if "trailing_partial" in vector:
            assert redactor.trailing_partial == vector["trailing_partial"]


@pytest.mark.parametrize("vector", WINDOW_VECTORS, ids=[v["name"] for v in WINDOW_VECTORS])
def test_window_vector(vector):
    data = _bytes(vector, "input")
    window = vector["window"]
    result = redact_window(
        data,
        window["start"],
        vector["rules"],
        base_offset=window["base_offset"],
        final=vector.get("final", False),
        **_options(vector, data),
    )
    assert result.data == _bytes(vector, "expected")
    assert result.counts == vector["counts"]
    assert result.begin == vector["begin"]
    assert result.end == vector["end"]
    assert result.aligned == vector["aligned"]
    assert result.trailing_partial == vector["trailing_partial"]


@pytest.mark.parametrize(
    "vector", VECTORS["long_line_cuts"], ids=[v["name"] for v in VECTORS["long_line_cuts"]]
)
def test_long_line_cut_vector(vector):
    """A value cut by the long-line limit at every position: nothing of it
    is kept before the marker."""
    if vector.get("source_encoding") == "utf-16-unsure":
        _utf16_unsure_long_line_cuts(vector)
        return
    max_line = vector["max_line_bytes"]
    kept = vector["kept"].encode()
    value = vector["value"].encode()
    guard = json.loads(DETECTORS_PATH.read_text(encoding="utf-8"))["cut_guard_bytes"]
    for k in range(len(value) + 1):
        filler = max_line - len(kept) - 2 - k
        assert filler >= 0
        line = kept + b" " + b"~" * filler + b" " + value + b" end\n"
        assert len(kept) + 2 + filler + k == max_line
        expected = vector["expected_kept"].encode()
        if max_line - k - 1 <= max_line - guard:
            expected += b" " + b"~" * filler
        expected += marker(long_line_label(max_line)) + b"\n"
        for blocks in ([line], [line[i : i + 1] for i in range(len(line))]):
            redactor = Redactor(vector["rules"], max_line_bytes=max_line)
            out = b"".join(redactor.feed(block) for block in blocks) + redactor.finish()
            assert out == expected, k
            assert redactor.counts == vector["counts"]


def _encode_kept(text: str, encoding: str) -> bytes:
    """Kept text as a UTF16_UNSURE redaction gives it: the source's own
    UTF-16 bytes, with the markers in ASCII."""
    parts = re.split(r"(\[redacted: [^\]]*\])", text)
    return b"".join(
        part.encode() if index % 2 else part.encode(encoding) for index, part in enumerate(parts)
    )


def _utf16_unsure_long_line_cuts(vector: dict) -> None:
    """The UTF16_UNSURE form of the long_line_cuts vectors: the guard is
    twice cut_guard_bytes in source bytes, and k counts characters."""
    encoding = vector["input_encoding"]
    big = encoding == "utf-16-be"
    max_line = vector["max_line_bytes"]
    kept, value = vector["kept"], vector["value"]
    guard = 2 * json.loads(DETECTORS_PATH.read_text(encoding="utf-8"))["cut_guard_bytes"]
    for k in range(len(value) + 1):
        filler = max_line // 2 - len(kept) - 2 - k
        assert filler >= 0
        line = f"{kept} {'~' * filler} {value} end\n".encode(encoding)
        assert len(f"{kept} {'~' * filler} ".encode(encoding)) + 2 * k == max_line
        blank = 2 * (len(kept) + 1 + filler) + (1 if big else 0)
        expected = _encode_kept(vector["expected_kept"], encoding)
        if blank <= max_line - guard:
            expected += f" {'~' * filler}".encode(encoding)
        if big:
            expected += b"\x00"
        expected += marker(long_line_label(max_line)) + b"\n"
        if not big:
            expected += b"\x00"
        for blocks in ([line], [line[i : i + 1] for i in range(len(line))]):
            redactor = Redactor(
                vector["rules"], max_line_bytes=max_line, source_encoding=UTF16_UNSURE
            )
            out = b"".join(redactor.feed(block) for block in blocks) + redactor.finish()
            assert out == expected, k
            assert redactor.counts == vector["counts"]


@pytest.mark.parametrize("vector", VECTORS["head_digests"])
def test_head_digest_vector(vector):
    key = base64.b64decode(vector["key_b64"])
    assert head_digest(key, vector["data"].encode(), vector["offset"]) == vector["expected"]


@pytest.mark.parametrize("vector", VECTORS["detect"])
def test_detect_vector(vector):
    head = base64.b64decode(vector["head_b64"])
    if vector["detected"] == "error":
        with pytest.raises(UnsupportedEncodingError):
            detect_encoding(head)
    else:
        assert detect_encoding(head) == vector["detected"]


@pytest.mark.parametrize("vector", VECTORS["rule_digests"])
def test_rule_digest_vector(vector):
    """The agent caches compiled rules by the same digest."""
    assert rules_digest(vector["rules"]) == vector["expected"]


@pytest.mark.parametrize(
    "vector", VECTORS["rule_errors"], ids=[v["name"] for v in VECTORS["rule_errors"]]
)
def test_rule_error_vector(vector):
    """Rule lists both engines refuse to compile (or, marked accepted, a
    limit both accept)."""
    if vector.get("accepted"):
        compile_rules(vector["rules"])
        return
    with pytest.raises(RedactionRuleError) as caught:
        compile_rules(vector["rules"])
    if "message" in vector:
        assert str(caught.value).endswith(vector["message"])


def test_every_detector_has_a_vector():
    used = {
        rule["detector"]
        for vector in VECTORS["vectors"]
        for rule in vector["rules"]
        if "detector" in rule
    }
    detectors = json.loads(DETECTORS_PATH.read_text(encoding="utf-8"))
    assert used == {d["name"] for d in detectors["detectors"]}
    assert VECTORS["engine"] == detectors["engine"]


def test_the_agent_embeds_the_same_detectors():
    """The agent's Go engine embeds its own copy of detectors.json (it can't
    read the backend's, and the backend image doesn't carry agent/): a
    detector changed on one side only must fail here."""
    assert (
        AGENT_DETECTORS_PATH.read_bytes() == DETECTORS_PATH.read_bytes()
    ), "agent/internal/redact/detectors.json differs from backend/app/redaction/detectors.json"
