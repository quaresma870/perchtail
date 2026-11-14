"""Which encoding a file's content is redacted in. The detectors match
ASCII, so any ASCII-compatible encoding (UTF-8, Latin-1, CP1252) or binary
is redacted as bytes; UTF-16, common on Windows, has a NUL after (or
before) every ASCII character and is decoded first. Content that may be
UTF-16 without being clearly so is redacted as bytes and as both UTF-16
readings (the union, see engine.py). UTF-32 is refused: it is rare and its
NULs look like UTF-16's.

The decision is taken from the first HEAD_BYTES of the file, the same bytes
the viewer's Tail already identifies the file by, so /open, every Tail poll
and the agent decide the same way however far into the file they read."""

UTF16_LE = "utf-16-le"
UTF16_BE = "utf-16-be"
# Content that may be UTF-16 but isn't clearly so: it is served as bytes,
# and the detectors run on the bytes and on the UTF-16 LE and BE readings
# of them, redacting the union.
UTF16_UNSURE = "utf-16-unsure"

HEAD_BYTES = 64


class UnsupportedEncodingError(ValueError):
    """Content that can't be redacted: refused rather than served as is."""

    code = "unsupported_encoding"


def detect_encoding(head: bytes) -> str | None:
    """UTF16_LE or UTF16_BE for UTF-16 content, UTF16_UNSURE for content
    that looks like UTF-16 without being clearly so, None for content
    redacted as bytes; raises UnsupportedEncodingError for UTF-32.

    With a byte-order mark the mark decides. Without one, the first
    HEAD_BYTES are read as 2-byte (or 4-byte) units: UTF-32 when every unit
    is one non-NUL byte and three NULs; UTF-16 when at least three quarters
    of the 2-byte units are a non-NUL byte followed by a NUL (little
    endian) or a NUL followed by a non-NUL byte (big endian), and no unit
    has it the other way round. When that rule hesitates but at least a
    quarter of the units have their only NUL on the same side (a UTF-16
    file with a character such as U+4E00 or U+3000 near its start, or
    mostly non-Latin text), the content is UTF16_UNSURE."""
    head = head[:HEAD_BYTES]
    if head.startswith(b"\xff\xfe\x00\x00") or head.startswith(b"\x00\x00\xfe\xff"):
        raise UnsupportedEncodingError("UTF-32 content can't be redacted")
    if head.startswith(b"\xff\xfe"):
        return UTF16_LE
    if head.startswith(b"\xfe\xff"):
        return UTF16_BE
    quads = [head[i : i + 4] for i in range(0, len(head) - len(head) % 4, 4)]
    if len(quads) >= 2:
        if all(q[0] and not q[1] and not q[2] and not q[3] for q in quads):
            raise UnsupportedEncodingError("UTF-32 content can't be redacted")
        if all(not q[0] and not q[1] and not q[2] and q[3] for q in quads):
            raise UnsupportedEncodingError("UTF-32 content can't be redacted")
    pairs = [head[i : i + 2] for i in range(0, len(head) - len(head) % 2, 2)]
    if len(pairs) < 2:
        return None
    little = sum(1 for p in pairs if p[0] and not p[1])
    big = sum(1 for p in pairs if not p[0] and p[1])
    if little * 4 >= len(pairs) * 3 and big == 0:
        return UTF16_LE
    if big * 4 >= len(pairs) * 3 and little == 0:
        return UTF16_BE
    if little * 4 >= len(pairs) or big * 4 >= len(pairs):
        return UTF16_UNSURE
    return None
