"""Which encoding a file's content is redacted in. The detectors match
ASCII, so any ASCII-compatible encoding (UTF-8, Latin-1, CP1252) or binary
is redacted as bytes; UTF-16, common on Windows, has a NUL after (or
before) every ASCII character and is decoded first. Content that may be
UTF-16 without being clearly so is redacted as bytes and as both UTF-16
readings (the union, see engine.py). UTF-32 is refused: it is rare and its
NULs look like UTF-16's.

The decision is taken from the first HEAD_BYTES of the file, the same bytes
the viewer's Tail already identifies the file by, so /open, every Tail poll
and the agent decide the same way however far into the file they read.

Content can change encoding part way, so the engine also looks at every
line it reads (find_reading_change): a line of content read as bytes that
looks UTF-16, or a line of clear UTF-16 content with ASCII text in it, is
read as UTF16_UNSURE from its start to the end of the read; a line that
looks UTF-32 ends the read with a refusal."""

import re

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


# What find_reading_change gives for a line that looks UTF-32: the rest of
# the read is refused.
UTF32 = "utf-32"

# The windows find_reading_change looks for. None holds a newline byte, so
# each lies within one line of bytes, whatever the reading; in clear UTF-16
# LE content, none starts on the NUL of a newline (`0A 00` on an even
# offset), which ends the line before. UTF-32: 16 bytes
# in which each group of 4 has one byte that isn't NUL, at the same place in
# every group (in any phase). UTF-16: 4 units of 2 bytes, each with exactly
# one NUL, on the same side (on even file offsets). ASCII: 4 printable ASCII
# bytes in a row.
_UTF32_WINDOW = re.compile(
    rb"(?:[^\x00\n]\x00\x00\x00){4}|(?:\x00[^\x00\n]\x00\x00){4}"
    rb"|(?:\x00\x00[^\x00\n]\x00){4}|(?:\x00\x00\x00[^\x00\n]){4}"
)
_UTF16_WINDOW = re.compile(rb"(?:[^\x00\n]\x00){4}|(?:\x00[^\x00\n]){4}")
_ASCII_WINDOW = re.compile(rb"[\x20-\x7e]{4}")
_UTF32_BYTES = 16
_UTF16_BYTES = 8
_ASCII_BYTES = 4
# How much of what came before a piece of content is looked at again with
# it, so a window across the two is still found.
CHANGE_CARRY_BYTES = _UTF32_BYTES - 1


def _first_utf32(data: bytes, at: int, reading: str | None) -> int | None:
    # Every UTF-32 window holds three NULs in a row.
    if b"\x00\x00\x00" not in data:
        return None
    position = 0
    while (found := _UTF32_WINDOW.search(data, position)) is not None:
        index = found.start()
        if not (
            reading == UTF16_LE
            and index > 0
            and data[index - 1] == 0x0A
            and data[index] == 0
            and (at + index) % 2
        ):
            return index
        position = index + 1
    return None


def _first_aligned_utf16(data: bytes, at: int, endpos: int) -> int | None:
    if b"\x00" not in data:
        return None
    position = 0
    while (found := _UTF16_WINDOW.search(data, position, endpos)) is not None:
        if (at + found.start()) % 2 == 0:
            return found.start()
        position = found.start() + 1
    return None


def find_reading_change(data: bytes, at: int, reading: str | None) -> tuple[int, str] | None:
    """Where in `data` (content that starts at file offset `at`, read as
    `reading`: None for bytes, UTF16_LE, UTF16_BE or UTF16_UNSURE) the first
    window that changes how it is read starts, and what it changes to:
    UTF32 for a UTF-32 window (in any reading), UTF16_UNSURE for a UTF-16
    window in content read as bytes or an ASCII window in clear UTF-16
    content. When two start at the same byte, UTF32 wins. None when there
    is none."""
    first = _first_utf32(data, at, reading)
    best = None if first is None else (first, UTF32)
    if reading is None:
        # Only a window that starts before the UTF-32 one counts.
        endpos = len(data) if best is None else min(len(data), best[0] + _UTF16_BYTES - 1)
        index = _first_aligned_utf16(data, at, endpos)
        if index is not None:
            best = (index, UTF16_UNSURE)
    elif reading in (UTF16_LE, UTF16_BE):
        endpos = len(data) if best is None else min(len(data), best[0] + _ASCII_BYTES - 1)
        found = _ASCII_WINDOW.search(data, 0, endpos)
        if found is not None:
            best = (found.start(), UTF16_UNSURE)
    return best


def strongest_reading_change(data: bytes, at: int, reading: str | None) -> str | None:
    """What the windows anywhere in `data` change the reading to: UTF32 when
    there is a UTF-32 window, otherwise UTF16_UNSURE when there is a window
    that changes it to that (see find_reading_change), otherwise None."""
    if _first_utf32(data, at, reading) is not None:
        return UTF32
    change = find_reading_change(data, at, reading)
    return None if change is None else change[1]
