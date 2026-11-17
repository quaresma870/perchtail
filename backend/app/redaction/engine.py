"""The redaction engine (#382, #249): replaces secrets, card numbers and
personal data in file content with `[redacted: <label>]` before the content
leaves the server. It never changes the source; callers run it on what they
fetched.

How it works:

- Content is redacted as bytes, a line at a time, in streaming: feed() takes
  blocks of any size and returns the redacted complete lines, keeping the
  unfinished last line for the next block. Every rule matches within one
  line (RE2's never_nl), so where the blocks are cut never changes the
  result, and memory stays O(block + MAX_LINE_BYTES).
- Rules come in the same shape as the agent's `redact` block: a built-in
  `{"name", "detector"}` from detectors.json, or a custom
  `{"name", "pattern", "secret_group", "max_length"}` RE2 pattern, where
  max_length is the longest match it can make, in bytes. Built-ins and custom
  patterns that are pure ASCII run in Latin-1, byte for byte, so invalid
  UTF-8 never stops a match; a custom pattern with non-ASCII literals runs
  in UTF-8.
- Matches that overlap are merged and replaced once; the first rule in the
  list gives the label. `secret_group` replaces only that group (the
  `password=` before the value stays).
- Private keys: `-----BEGIN ... PRIVATE KEY-----` up to the matching END
  becomes one `[redacted: private key]`, keeping the line breaks so line
  numbers don't move; a BEGIN without an END is redacted for at most
  pem_max_lines lines.
- A line longer than max_line_bytes keeps the start of its first
  max_line_bytes (redacted) and the rest becomes `[redacted: line over 1
  MiB]`. Nothing of a value cut by the limit may stay before the marker,
  so the kept start ends at least a guard before the limit: the largest of
  cut_guard_bytes (detectors.json, longer than any built-in value that can
  contain a blank) and the max_length of every custom rule; twice that for
  UTF16_UNSURE content, whose UTF-16 readings take two source bytes per
  character. Then it
  goes back to the last blank (space, tab, CR, VT, FF) before that point
  (nothing is kept when there is none) and back to the start of any
  replacement that the point would cut.
- An unfinished last line (no newline yet) is the start of a line still
  being written, where no detector can see the whole value: unless the
  read is final (a download, the end of a file), it is sent as
  `[redacted: incomplete line]`, and source_offset/trailing_partial tell a
  Tail where to read it again.
- UTF-16 content is decoded, redacted as UTF-8 and served as UTF-8 or
  encoded back (output_encoding="source"); offsets stay in source bytes.
- Content that may be UTF-16 without being clearly so (UTF16_UNSURE) is
  served as bytes; the detectors run on the bytes and on the UTF-16 LE and
  BE readings of them (units aligned on the file's even offsets, so
  `origin` matters), and the union of what they find is replaced.
- The `iban` detector searches with overlap: when a match fails its check,
  the search goes on from the byte after the start of the match, so a code
  like `AB12` just before an IBAN doesn't hide it.
- Content can change encoding part way (encodings.find_reading_change):
  every line is looked at in the reading it is read in. From the start of
  a line of bytes content that looks UTF-16, or of a line of clear UTF-16
  content with ASCII text in it, to the end of the read, the content is
  read as UTF16_UNSURE (served as bytes). From the start of a line that
  looks UTF-32, the rest is refused: `[redacted: unsupported encoding]`
  takes its place, the rest is read without being sent, and refused_at
  says where it started. A window that only the dropped part of a long
  line holds changes the reading from the next line. Each read looks again
  at its own lines: a Tail poll doesn't know what an earlier read found.

The agent's Go engine (agent/internal/redact) implements the same
behaviour; agent/internal/redact/testdata/vectors.json holds the shared
vectors both test suites run."""

import hashlib
import hmac
import json
import sys
from array import array
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import accumulate
from pathlib import Path
from typing import BinaryIO

import re2

from app.redaction import checks
from app.redaction.encodings import (
    CHANGE_CARRY_BYTES,
    UTF16_BE,
    UTF16_LE,
    UTF16_UNSURE,
    UTF32,
    UnsupportedEncodingError,
    find_reading_change,
    strongest_reading_change,
)

ENGINE_VERSION = 1
MAX_LINE_BYTES = 1024 * 1024
PEM_MAX_LINES = 200
CHUNK_BYTES = 1024 * 1024
HEAD_BYTES = 64
# The largest max_length a custom rule may declare, in bytes; the agent's
# engine refuses the same (MaxRuleLength).
MAX_RULE_LENGTH = 4096

INCOMPLETE_LINE_LABEL = "incomplete line"
UNSUPPORTED_ENCODING_LABEL = "unsupported encoding"

_DETECTORS = json.loads(Path(__file__).with_name("detectors.json").read_text(encoding="utf-8"))
DETECTOR_NAMES: tuple[str, ...] = tuple(d["name"] for d in _DETECTORS["detectors"])
_DETECTOR_BY_NAME = {d["name"]: d for d in _DETECTORS["detectors"]}
CUT_GUARD_BYTES: int = _DETECTORS["cut_guard_bytes"]

_NEWLINES = {None: b"\n", UTF16_LE: b"\n\x00", UTF16_BE: b"\x00\n", UTF16_UNSURE: b"\n"}
_BLANKS = b" \t\r\v\f"
_OUTPUT_ENCODINGS = ("utf-8", "source")


class RedactionRuleError(ValueError):
    """A rule the engine can't run (unknown detector, a pattern RE2 can't
    compile, a secret_group the pattern doesn't have)."""


def marker(label: str) -> bytes:
    return f"[redacted: {label}]".encode()


def _size_label(size: int) -> str:
    if size % (1024 * 1024) == 0:
        return f"{size // (1024 * 1024)} MiB"
    if size % 1024 == 0:
        return f"{size // 1024} KiB"
    return f"{size} bytes"


def long_line_label(max_line_bytes: int) -> str:
    return f"line over {_size_label(max_line_bytes)}"


def _options(latin1: bool) -> "re2.Options":
    options = re2.Options()
    options.log_errors = False
    # Every rule matches inside one line: what a block boundary or a Tail
    # poll cuts can never change a match.
    options.never_nl = True
    if latin1:
        options.encoding = re2.Options.Encoding.LATIN1
    return options


def compile_pattern(pattern: str):
    """Compiles a rule pattern the way the engine runs it: `^`/`$` at line
    boundaries, never across a newline, in Latin-1 when it's pure ASCII
    (the same spans as the agent's Go engine) and UTF-8 otherwise. Raises
    re2.error for a pattern RE2 can't compile."""
    text = "(?m)" + pattern
    if text.isascii():
        return re2.compile(text.encode("ascii"), _options(latin1=True))
    return re2.compile(text, _options(latin1=False))


@dataclass(frozen=True)
class _Rule:
    name: str
    regex: object
    group: int
    label: str
    check: str | None = None
    key_group: int | None = None


@dataclass(frozen=True)
class CompiledRules:
    rules: tuple[_Rule, ...]
    # (BEGIN regex, END regex, index of the private_key rule) when a
    # private_key rule is on.
    pem: tuple[object, object, int] | None = None
    # How far before max_line_bytes the kept start of a long line ends at
    # least (see Redactor._cut_point): the longest value any rule can
    # match with a blank in it, i.e. the largest of CUT_GUARD_BYTES (the
    # built-ins) and the max_length of every custom rule.
    cut_guard: int = CUT_GUARD_BYTES


def canonical_rules(rules: Sequence[Mapping]) -> str:
    return json.dumps(
        [dict(rule) for rule in rules], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def rules_digest(rules: Sequence[Mapping]) -> str:
    """Identifies a rule list: the SHA-256 of its canonical JSON."""
    return hashlib.sha256(canonical_rules(rules).encode("utf-8")).hexdigest()


def compile_rules(rules: Sequence[Mapping]) -> CompiledRules:
    """Compiles a rule list (cached by its canonical JSON, so the same rules
    compile once). Raises RedactionRuleError for a rule that can't run."""
    return _compile_canonical(canonical_rules(rules))


@lru_cache(maxsize=32)
def _compile_canonical(canonical: str) -> CompiledRules:
    compiled: list[_Rule] = []
    pem = None
    cut_guard = CUT_GUARD_BYTES
    for index, rule in enumerate(json.loads(canonical)):
        name = rule.get("name")
        if not isinstance(name, str) or not name:
            raise RedactionRuleError("every redaction rule needs a name")
        detector = rule.get("detector")
        if detector is not None:
            spec = _DETECTOR_BY_NAME.get(detector)
            if spec is None:
                raise RedactionRuleError(f"unknown detector {detector!r}")
            compiled.append(
                _Rule(
                    name=name,
                    regex=compile_pattern(spec["pattern"]),
                    group=spec["secret_group"],
                    label=spec["label"],
                    check=spec["check"],
                    key_group=spec.get("key_group"),
                )
            )
            if "pem_begin" in spec and pem is None:
                pem = (
                    compile_pattern(spec["pem_begin"]),
                    compile_pattern(spec["pem_end"]),
                    index,
                )
            continue
        pattern = rule.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise RedactionRuleError(f"redaction rule {name!r} needs a detector or a pattern")
        try:
            regex = compile_pattern(pattern)
        except re2.error as exc:
            raise RedactionRuleError(f"redaction rule {name!r}: {exc}") from exc
        group = rule.get("secret_group") or 0
        if not isinstance(group, int) or group < 0 or group > regex.groups:
            raise RedactionRuleError(
                f"redaction rule {name!r}: secret_group {group} isn't a group of the pattern"
            )
        max_length = rule.get("max_length")
        if max_length is not None:
            if (
                isinstance(max_length, bool)
                or not isinstance(max_length, int)
                or not 1 <= max_length <= MAX_RULE_LENGTH
            ):
                raise RedactionRuleError(
                    f"redaction rule {name!r}: max_length must be a whole number of bytes"
                    f" from 1 to {MAX_RULE_LENGTH}"
                )
            cut_guard = max(cut_guard, max_length)
        compiled.append(_Rule(name=name, regex=regex, group=group, label=name))
    return CompiledRules(rules=tuple(compiled), pem=pem, cut_guard=cut_guard)


# A replacement candidate: (start, end, priority, rule index, replacement,
# counted). Private-key spans have priority 0 and win any overlap; regex
# matches have priority 1, and the rule listed first wins among them.
_Span = tuple[int, int, int, int, bytes, bool]
# A merged replacement: (start, end, replacement, index of the rule to
# count, or None).
_Merged = tuple[int, int, bytes, int | None]


@dataclass
class _PemState:
    open: bool = False
    lines: int = 0


# UTF-16 readings of UTF16_UNSURE content: lone surrogates become U+FFFD,
# and a unit with a newline byte on either side is a newline, so a match
# found in a reading never crosses a newline byte of the content (which is
# what keeps the result independent of where blocks are cut).
_UTF16_TRANSLATE: dict[int, int] = {code: 0xFFFD for code in range(0xD800, 0xE000)}
for _byte in range(256):
    _UTF16_TRANSLATE[0x0A00 | _byte] = 0x0A
    _UTF16_TRANSLATE[(_byte << 8) | 0x0A] = 0x0A


class _Utf16View:
    """One UTF-16 reading (LE or BE) of a piece of bytes content, as UTF-8
    to run the rules on, with the way back to source offsets. `skip` is 1
    when the piece starts at an odd offset of the file. Surrogate pairs are
    not joined: every unit is one character, two source bytes (a last odd
    byte is one)."""

    def __init__(self, source: bytes, big: bool, skip: int) -> None:
        skip = min(skip, len(source))
        end = skip + (len(source) - skip) // 2 * 2
        units = array("H")
        units.frombytes(source[skip:end])
        if (sys.byteorder == "big") != big:
            units.byteswap()
        text = "".join(map(chr, units)).translate(_UTF16_TRANSLATE)
        if end < len(source):
            text += "\n" if source[end] == 10 else "�"
        self.work = text.encode("utf-8")
        self._skip = skip
        self._size = len(source)
        self._starts: list[int] | None = None
        if len(self.work) != len(text):
            sizes = (1 if char < "\x80" else 2 if char < "ࠀ" else 3 for char in text)
            self._starts = list(accumulate(sizes, initial=0))

    def _char(self, offset: int) -> int:
        if self._starts is None:
            return offset
        return bisect_right(self._starts, offset) - 1

    def source_range(self, start: int, end: int) -> tuple[int, int]:
        first = self._char(start)
        last = self._char(end - 1)
        return self._skip + 2 * first, min(self._skip + 2 * last + 2, self._size)


class _BytesView:
    """The bytes reading of a piece of content, for the private-key scan of
    the dropped part of a long line."""

    def __init__(self, source: bytes) -> None:
        self.work = source

    @staticmethod
    def source_range(start: int, end: int) -> tuple[int, int]:
        return start, end


# How much of the content before a dropped piece of a long line is read
# again with it, so a private-key BEGIN or END split between two pieces is
# still found.
_PEM_CARRY_BYTES = 512


class Redactor:
    """Redacts one stream of content. feed() blocks in order, then call
    finish() once; counts, source_size, source_offset and trailing_partial
    are set by then.

    - source_encoding: None (bytes), UTF16_LE, UTF16_BE or UTF16_UNSURE
      (see encodings.detect_encoding).
    - output_encoding: "utf-8" (what the viewer reads) or "source" (UTF-16
      encoded back, for downloads). Bytes and UTF16_UNSURE content is
      served as bytes either way.
    - origin: the file offset of the first byte fed (it matters for
      UTF16_UNSURE, whose UTF-16 readings are aligned on even offsets, and
      for refused_at and union_at).
    - refused_at: None, or the file offset from which the rest was refused
      (a line that looks UTF-32, see the module docstring).
    - union_at: None, or the file offset from which the content was read as
      UTF16_UNSURE although it didn't start so; from there on lines end at
      every newline byte.
    - With no rules the content passes through unchanged, byte for byte."""

    def __init__(
        self,
        rules: Sequence[Mapping] | CompiledRules,
        *,
        source_encoding: str | None = None,
        output_encoding: str = "utf-8",
        max_line_bytes: int = MAX_LINE_BYTES,
        pem_max_lines: int = PEM_MAX_LINES,
        origin: int = 0,
    ) -> None:
        self._rules = rules if isinstance(rules, CompiledRules) else compile_rules(rules)
        if source_encoding not in _NEWLINES:
            raise UnsupportedEncodingError(f"can't redact {source_encoding!r} content")
        if output_encoding not in _OUTPUT_ENCODINGS:
            raise ValueError(f"output_encoding must be one of {_OUTPUT_ENCODINGS}")
        self.passthrough = not self._rules.rules
        self._union = source_encoding == UTF16_UNSURE
        self._origin = origin
        if self._union:
            source_encoding = None
        self._encoding = source_encoding
        self._unit = 1 if source_encoding is None else 2
        self._newline = _NEWLINES[source_encoding]
        self._reencode = source_encoding is not None and output_encoding == "source"
        if max_line_bytes < self._unit or pem_max_lines < 1:
            raise ValueError("max_line_bytes and pem_max_lines must be positive")
        self._max_line_bytes = max_line_bytes
        self._max_line = max_line_bytes - max_line_bytes % self._unit
        self._long_line = marker(long_line_label(max_line_bytes))
        self._pem_max = pem_max_lines
        self._buffer = bytearray()
        self._consumed = 0  # source offset of _buffer[0]
        self._line_start = 0  # source offset of the line being read
        self._overflow = False  # the current line is over max_line_bytes
        self._line_output = 0  # bytes already sent for the current line
        self._line_counts: dict[str, int] = {}
        # Private-key state of the content, and of its UTF-16 LE and BE
        # readings for UTF16_UNSURE content.
        self._pem = (_PemState(), _PemState(), _PemState())
        # While a long line is being dropped: the content just before the
        # next piece, read again with it, and per reading the file offsets
        # where the last private-key BEGIN ends and the last END starts.
        self._pem_carry = b""
        self._pem_found: list[list[int | None]] = []
        # While a long line is being dropped: its last bytes read, looked at
        # again with the next piece, and how the windows in the dropped part
        # change the reading from the next line.
        self._rest_carry = b""
        self._rest_change: str | None = None
        self.counts: dict[str, int] = {}
        self.source_size = 0
        self.source_offset = 0
        self.trailing_partial = 0
        self.refused_at: int | None = None
        self.union_at: int | None = None

    # -- public API ---------------------------------------------------------

    def feed(self, data: bytes) -> bytes:
        self.source_size += len(data)
        if self.passthrough:
            return bytes(data)
        if self.refused_at is not None:
            return b""
        buffer = self._buffer
        buffer += data
        out: list[bytes] = []
        while self.refused_at is None:
            if self._overflow:
                index = self._find_newline(buffer, 0)
                at = self._origin + self._consumed
                if index < 0:
                    size = len(buffer) - len(buffer) % self._unit
                    self._dropped_feed(bytes(buffer[:size]), at)
                    self._drop(size)
                    break
                self._dropped_feed(bytes(buffer[:index]), at)
                self._pem_tail_end()
                self._drop(index + len(self._newline))
                out.append(self._encode_marker(b"\n"))
                self._overflow = False
                self._line_start = self._consumed
                self._line_output = 0
                self._line_counts = {}
                if self._rest_change is not None:
                    change, self._rest_change = self._rest_change, None
                    out.append(self._change_reading(change, self._origin + self._consumed))
                continue
            index = self._rfind_newline(buffer)
            if index >= 0:
                end = index + len(self._newline)
                at = self._origin + self._consumed
                text, used = self._lines(bytes(buffer[:end]), at, emit=True)
                out.append(text)
                if self.refused_at is not None:
                    break
                self._drop(used)
                self._line_start = self._consumed
                if used < end:
                    # The reading changed: the rest is split into lines again.
                    continue
            if len(buffer) > self._max_line:
                at = self._origin + self._consumed
                source = bytes(buffer[: self._max_line])
                change = self._reading_change(source, at)
                if change is not None:
                    out.append(self._change_reading(change[1], at))
                    continue
                self._line_counts = {}
                piece = self._segment(source, at, self._line_counts)
                piece += self._encode_marker(self._long_line)
                out.append(piece)
                self._line_output = len(piece)
                self._overflow = True
                size = len(buffer) - len(buffer) % self._unit
                self._pem_tail_start(source, at)
                self._rest_carry = source[-CHANGE_CARRY_BYTES:]
                self._rest_change = None
                self._dropped_feed(bytes(buffer[self._max_line : size]), at + self._max_line)
                self._drop(size)
                continue
            break
        if self.refused_at is not None:
            buffer.clear()
        return b"".join(out)

    def finish(self, final: bool = True) -> bytes:
        """The end of the content read. final=True when it is the end of
        the file or of what a download sends: the last line is redacted as
        it is. final=False for a read that the file may continue past (the
        viewer, a Tail poll): an unfinished last line is sent as
        `[redacted: incomplete line]`, source_offset is where it starts and
        trailing_partial is how many bytes of the output it took."""
        self.trailing_partial = 0
        if self.passthrough or self.refused_at is not None:
            # Refused: what was read is done with, sent or not.
            self.source_offset = self.source_size
            self._buffer.clear()
            return b""
        out = b""
        if self._overflow:
            if final:
                self.source_offset = self.source_size
            else:
                self.trailing_partial = self._line_output
                self.source_offset = self._line_start
                for name, count in self._line_counts.items():
                    self.counts[name] -= count
                    if not self.counts[name]:
                        del self.counts[name]
        elif self._buffer:
            if final:
                out = self._all_lines(bytes(self._buffer), self._origin + self._consumed)
                self.source_offset = self.source_size
            else:
                out = self._encode_marker(marker(INCOMPLETE_LINE_LABEL))
                self.trailing_partial = len(out)
                self.source_offset = self._line_start
        else:
            self.source_offset = self.source_size
        self._buffer.clear()
        return out

    def skip_context(self, data: bytes, at: int = 0) -> None:
        """Reads whole lines that come before the content to redact (`at`
        is the file offset of data[0]), only to know whether it starts
        inside a private-key block. Nothing is returned or counted."""
        if not self.passthrough and self._rules.pem is not None and data:
            self._lines(data, at, emit=False)

    # -- readings ------------------------------------------------------------

    def _reading(self) -> str | None:
        return UTF16_UNSURE if self._union else self._encoding

    def _reading_change(self, data: bytes, at: int) -> tuple[int, str] | None:
        return find_reading_change(data, at, self._reading())

    def _change_reading(self, reading: str, at: int) -> bytes:
        """Reads the content from file offset `at`, the start of a line, as
        `reading`: UTF16_UNSURE, or UTF32, which refuses the rest. Returns
        what to send in its place (the marker for a refusal)."""
        if reading == UTF32:
            self.refused_at = at
            self._buffer.clear()
            self._overflow = False
            return self._encode_marker(marker(UNSUPPORTED_ENCODING_LABEL))
        if self._encoding is not None:
            # Clear UTF-16: its private-key state goes on in the reading of
            # the same byte order.
            state = self._pem[0]
            big = self._encoding == UTF16_BE
            self._pem = (_PemState(), _PemState() if big else state, state if big else _PemState())
        self.union_at = at
        self._union = True
        self._encoding = None
        self._unit = 1
        self._newline = b"\n"
        self._reencode = False
        self._max_line = self._max_line_bytes
        return b""

    def _dropped_feed(self, source: bytes, at: int) -> None:
        """The next piece of the dropped part of a long line: read for
        private keys and for windows that change the reading."""
        self._pem_tail_feed(source, at)
        if self._rest_change == UTF32 or not source:
            return
        text = self._rest_carry + source
        change = strongest_reading_change(text, at - len(self._rest_carry), self._reading())
        if change is not None:
            self._rest_change = change
        self._rest_carry = text[-CHANGE_CARRY_BYTES:]

    # -- lines ---------------------------------------------------------------

    def _drop(self, size: int) -> None:
        del self._buffer[:size]
        self._consumed += size

    def _find_newline(self, data, start: int, end: int | None = None) -> int:
        end = len(data) if end is None else end
        index = data.find(self._newline, start, end)
        while index >= 0 and index % self._unit:
            index = data.find(self._newline, index + 1, end)
        return index

    def _rfind_newline(self, data, end: int | None = None) -> int:
        end = len(data) if end is None else end
        index = data.rfind(self._newline, 0, end)
        while index >= 0 and index % self._unit:
            index = data.rfind(self._newline, 0, index + 1)
        return index

    def _all_lines(self, block: bytes, at: int) -> bytes:
        """_lines on the whole block, through every change of reading."""
        out: list[bytes] = []
        while block and self.refused_at is None:
            text, used = self._lines(block, at, emit=True)
            out.append(text)
            block = block[used:]
            at += used
        return b"".join(out)

    def _lines(self, block: bytes, at: int, *, emit: bool) -> tuple[bytes, int]:
        """Redacts a block of whole lines (the last one may lack its
        newline only at the end of the content) that starts at file offset
        `at`, giving the lines over max_line_bytes the long-line marker.
        Returns the output and how much of the block it covers: less than
        all of it when the reading changed at the start of a line (the rest
        is to be split into lines again), all of it when the rest was
        refused."""
        newline = self._newline
        if len(block) <= self._max_line + len(newline) or (
            self._unit == 1 and max(map(len, block.split(b"\n"))) <= self._max_line
        ):
            return self._short_lines(block, at, emit)
        out: list[bytes] = []
        start = 0
        pending = 0
        while start < len(block):
            index = self._find_newline(block, start)
            end = len(block) if index < 0 else index
            after = len(block) if index < 0 else index + len(newline)
            if end - start > self._max_line:
                if pending < start:
                    text, used = self._short_lines(block[pending:start], at + pending, emit)
                    out.append(text)
                    if used < start - pending or self.refused_at is not None:
                        return b"".join(out), pending + used
                piece = block[start : start + self._max_line]
                if emit and (change := self._reading_change(piece, at + start)) is not None:
                    out.append(self._change_reading(change[1], at + start))
                    return b"".join(out), start
                out.append(self._segment(piece, at + start, self.counts, emit))
                self._pem_tail_start(piece, at + start)
                rest = block[start + self._max_line : end]
                self._pem_tail_feed(rest, at + start + self._max_line)
                self._pem_tail_end()
                rest_change = None
                if emit:
                    carry = piece[-CHANGE_CARRY_BYTES:]
                    rest_change = strongest_reading_change(
                        carry + rest, at + start + self._max_line - len(carry), self._reading()
                    )
                    out.append(self._encode_marker(self._long_line))
                    if index >= 0:
                        out.append(self._encode_marker(b"\n"))
                pending = after
                if rest_change is not None:
                    out.append(self._change_reading(rest_change, at + after))
                    return b"".join(out), after
            start = after
        if pending < len(block):
            text, used = self._short_lines(block[pending:], at + pending, emit)
            out.append(text)
            return b"".join(out), pending + used
        return b"".join(out), len(block)

    def _short_lines(self, block: bytes, at: int, emit: bool) -> tuple[bytes, int]:
        """_lines for a block with no line over max_line_bytes: up to the
        start of the first line whose windows change the reading."""
        if emit and (change := self._reading_change(block, at)) is not None:
            index, reading = change
            newline = self._rfind_newline(block, index)
            line = 0 if newline < 0 else newline + len(self._newline)
            out = self._redact_source(block[:line], at, self.counts, emit) if line else b""
            return out + self._change_reading(reading, at + line), line
        return self._redact_source(block, at, self.counts, emit), len(block)

    def _segment(self, piece: bytes, at: int, counts: dict[str, int], emit: bool = True) -> bytes:
        """The first max_line_bytes of a long line: only the part before
        the cut point (see _cut_point) is kept, redacted; counted in both
        the line's and the total counts."""
        if not emit:
            self._scan(piece, at, collect=False)
            return b""
        base, merged = self._scan(piece, at)
        cut = self._cut_point(base, merged, self._rules.cut_guard * (2 if self._union else 1))
        out = self._from_work(self._apply(base[:cut], [m for m in merged if m[1] <= cut], counts))
        if counts is not self.counts:
            for name, count in counts.items():
                self.counts[name] = self.counts.get(name, 0) + count
        return out

    @staticmethod
    def _cut_point(base: bytes, merged: list[_Merged], guard: int) -> int:
        """Where the kept start of a long line ends: at least `guard` bytes
        before the limit, then back to the last blank (or to the start of
        the line when there is none), then back to the start of a
        replacement the point would cut. A value cut by the limit has no
        blank in its last `guard` characters (no built-in value with a
        blank is longer than CUT_GUARD_BYTES, the `authorization` value is
        replaced from its key, and no custom value is longer than its
        rule's max_length), so nothing of it is kept.

        The guard counts characters of every reading: `base` is the
        work text (bytes, or UTF-8 for clear UTF-16), so it is
        CompiledRules.cut_guard, except for UTF16_UNSURE content, where `base` is
        the source bytes and a character of the UTF-16 readings takes two
        of them, so it is twice that. The blanks are looked for byte by
        byte, which finds those of every reading: `20 00` (LE) and `00 20`
        (BE) both hold the byte 0x20."""
        cut = len(base) - guard
        if cut <= 0:
            return 0
        cut = max(base.rfind(blank, 0, cut + 1) for blank in _BLANKS)
        if cut <= 0:
            return 0
        for start, end, _, _ in merged:
            if start < cut < end:
                return start
            if start >= cut:
                break
        return cut

    # -- private keys in the dropped part of a long line -----------------------

    def _readings(self, source: bytes, at: int) -> list:
        """The readings of a piece of content the private-key state is kept
        for, in the order of self._pem, with the way back to source
        offsets."""
        if self._union:
            return [_BytesView(source)] + [_Utf16View(source, big, at % 2) for big in (False, True)]
        if self._encoding is None:
            return [_BytesView(source)]
        return [_Utf16View(source, self._encoding == UTF16_BE, at % 2)]

    def _pem_tail_start(self, piece: bytes, at: int) -> None:
        """A line went over max_line_bytes after `piece` (its kept part,
        already read by the private-key state machine). The rest of the line
        is dropped without being shown, but a private-key BEGIN in it still
        has to hide the lines that follow, so it is read for BEGIN and END
        (see _pem_tail_end)."""
        if self._rules.pem is None:
            return
        self._pem_carry = piece[-_PEM_CARRY_BYTES:]
        self._pem_found = [[None, None] for _ in self._readings(b"", at)]

    def _pem_tail_feed(self, source: bytes, at: int) -> None:
        if self._rules.pem is None or not source:
            return
        begin_re, end_re, _ = self._rules.pem
        text = self._pem_carry + source
        text_at = at - len(self._pem_carry)
        for found, view in zip(self._pem_found, self._readings(text, text_at), strict=True):
            for match in begin_re.finditer(view.work):
                end = text_at + view.source_range(match.start(), match.end())[1]
                found[0] = end if found[0] is None else max(found[0], end)
            for match in end_re.finditer(view.work):
                start = text_at + view.source_range(match.start(), match.end())[0]
                found[1] = start if found[1] is None else max(found[1], start)
        self._pem_carry = text[-_PEM_CARRY_BYTES:]

    def _pem_tail_end(self) -> None:
        """The long line ended: a BEGIN in it with no END after it opens a
        private-key block over the next lines, as on a short line; an END
        after the last BEGIN closes one."""
        if self._rules.pem is None:
            return
        for state, (last_begin, last_end) in zip(self._pem, self._pem_found, strict=False):
            if last_begin is None and last_end is None:
                continue
            if last_begin is not None and (last_end is None or last_end < last_begin):
                state.open = self._pem_max > 1
                state.lines = 1
            else:
                state.open = False
        self._pem_found = []
        self._pem_carry = b""

    # -- encodings -------------------------------------------------------------

    def _to_work(self, source: bytes) -> bytes:
        if self._encoding is None:
            return source
        return source.decode(self._encoding, "replace").encode("utf-8")

    def _from_work(self, work: bytes) -> bytes:
        if not self._reencode:
            return work
        return work.decode("utf-8", "replace").encode(self._encoding)

    def _encode_marker(self, text: bytes) -> bytes:
        return text.decode("utf-8").encode(self._encoding) if self._reencode else text

    def _redact_source(self, source: bytes, at: int, counts: dict[str, int], emit: bool) -> bytes:
        if not emit:
            self._scan(source, at, collect=False)
            return b""
        base, merged = self._scan(source, at)
        return self._from_work(self._apply(base, merged, counts))

    # -- matching --------------------------------------------------------------

    def _scan(self, source: bytes, at: int, *, collect: bool = True) -> tuple[bytes, list[_Merged]]:
        """Runs the rules on whole lines of source content that starts at
        file offset `at`: the text the output is built from (the content,
        or its UTF-8 for UTF-16) and the merged replacements in it. For
        UTF16_UNSURE content the replacements found in its UTF-16 LE and BE
        readings are added, in source offsets. With collect=False only the
        private-key state moves on."""
        work = self._to_work(source)
        views = (
            [(_Utf16View(source, big, at % 2), self._pem[1 + big]) for big in (False, True)]
            if self._union
            else []
        )
        if not collect:
            self._pem_spans(work, None, self._pem[0])
            for view, state in views:
                self._pem_spans(view.work, None, state)
            return work, []
        spans = self._collect(work, self._pem[0])
        for view, state in views:
            for span in self._collect(view.work, state):
                spans.append((*view.source_range(span[0], span[1]), *span[2:]))
        return work, self._merge(spans)

    def _collect(self, work: bytes, pem: _PemState) -> list[_Span]:
        spans: list[_Span] = []
        self._pem_spans(work, spans, pem)
        for index, rule in enumerate(self._rules.rules):
            if rule.check == "iban":
                # With overlap: a failed match is searched again from the
                # byte after its start, so a code that looks like the start
                # of an IBAN (`AB12 PT50 ...`) doesn't hide the IBAN.
                position = 0
                while (match := rule.regex.search(work, position)) is not None:
                    start, end = match.span(rule.group)
                    parts = self._check(rule, match, work, start, end)
                    for part_start, part_end, label in parts:
                        spans.append(
                            (start + part_start, start + part_end, 1, index, marker(label), True)
                        )
                    position = start + parts[-1][1] if parts else match.start() + 1
                continue
            for match in rule.regex.finditer(work):
                start, end = match.span(rule.group)
                if start < 0 or start == end:
                    continue
                if rule.check is None:
                    spans.append((start, end, 1, index, marker(rule.label), True))
                    continue
                for part_start, part_end, label in self._check(rule, match, work, start, end):
                    spans.append(
                        (start + part_start, start + part_end, 1, index, marker(label), True)
                    )
        return spans

    @staticmethod
    def _merge(spans: list[_Span]) -> list[_Merged]:
        """Overlapping spans become one replacement: a private key wins,
        then the rule listed first, then the span that starts first."""
        spans.sort(key=lambda span: (span[0], span[1]))
        merged: list[_Merged] = []
        index = 0
        while index < len(spans):
            best = spans[index]
            start, end = best[0], best[1]
            index += 1
            while index < len(spans) and spans[index][0] < end:
                other = spans[index]
                end = max(end, other[1])
                if (other[2], other[3], other[0]) < (best[2], best[3], best[0]):
                    best = other
                index += 1
            merged.append((start, end, best[4], best[3] if best[5] else None))
        return merged

    def _apply(self, base: bytes, merged: list[_Merged], counts: dict[str, int]) -> bytes:
        if not merged:
            return base
        out = bytearray()
        position = 0
        for start, end, replacement, rule_index in merged:
            out += base[position:start]
            out += replacement
            position = end
            if rule_index is not None:
                name = self._rules.rules[rule_index].name
                counts[name] = counts.get(name, 0) + 1
        out += base[position:]
        return bytes(out)

    def _check(
        self, rule: _Rule, match, work: bytes, start: int, end: int
    ) -> list[tuple[int, int, str]]:
        """Runs a detector's verification on work[start:end]: the parts to
        replace (relative to start) with their labels, none to leave the
        match alone."""
        value = work[start:end]
        if rule.check == "luhn_iin":
            return [
                (card_start, card_end, rule.label.replace("{last4}", last4))
                for card_start, card_end, last4 in checks.card_spans(
                    value, _DETECTORS["card_brands"]
                )
            ]
        if rule.check == "iban":
            length = checks.iban_length(value, _DETECTORS["iban_lengths"])
            return [] if length is None else [(0, length, rule.label)]
        if rule.check == "ipv6":
            length = checks.ipv6_length(value, work[end : end + 1])
            return [] if length is None else [(0, length, rule.label)]
        if rule.check == "kv_value":
            if not checks.kv_value_ok(value, _DETECTORS["kv_ignore"]):
                return []
            label = checks.key_label(
                match.group(rule.key_group), _DETECTORS["key_labels"], rule.label
            )
            return [(0, len(value), label)]
        raise RedactionRuleError(f"unknown check {rule.check!r}")

    def _pem_spans(self, work: bytes, spans: list[_Span] | None, state: _PemState) -> None:
        """The private-key state machine over whole lines of `work`: adds
        the spans to replace (when `spans` isn't None) and carries the
        open/closed state to the next block."""
        if self._rules.pem is None:
            return
        if not state.open and b"-----BEGIN" not in work:
            return
        position = 0
        size = len(work)
        while position < size:
            if not state.open:
                begin = work.find(b"-----BEGIN", position)
                if begin < 0:
                    return
                position = work.rfind(b"\n", position, begin) + 1 or position
            end = work.find(b"\n", position)
            end = size if end < 0 else end
            self._pem_line(work, position, end, spans, state)
            position = end + 1

    def _pem_line(
        self, work: bytes, start: int, end: int, spans: list[_Span] | None, state: _PemState
    ) -> None:
        begin_re, end_re, rule_index = self._rules.pem
        content_end = end - 1 if end > start and work[end - 1] == 13 else end

        def add(span_start: int, span_end: int, replacement: bytes, counted: bool) -> None:
            if spans is not None and span_start < span_end:
                spans.append((span_start, span_end, 0, rule_index, replacement, counted))

        cursor = start
        if state.open:
            found = end_re.search(work, start, content_end)
            if found is None:
                add(start, content_end, b"", False)
                state.lines += 1
                if state.lines >= self._pem_max:
                    state.open = False
                return
            add(start, found.end(), b"", False)
            state.open = False
            cursor = found.end()
        last_begin = None
        for found in begin_re.finditer(work, cursor, content_end):
            last_begin = found
        if last_begin is None or end_re.search(work, last_begin.end(), content_end):
            return
        add(last_begin.start(), content_end, marker(self._rules.rules[rule_index].label), True)
        state.open = True
        state.lines = 1
        if state.lines >= self._pem_max:
            state.open = False


@dataclass
class RedactionResult:
    counts: dict[str, int] = field(default_factory=dict)
    source_size: int = 0
    source_offset: int = 0
    trailing_partial: int = 0
    output_size: int = 0
    # File offsets (see Redactor): where the rest was refused, and where the
    # content started to be read as UTF16_UNSURE.
    refused_at: int | None = None
    union_at: int | None = None


def redact_bytes(
    data: bytes, rules: Sequence[Mapping] | CompiledRules, *, final: bool = True, **options
) -> tuple[bytes, RedactionResult]:
    """Redacts content held in memory in one go."""
    redactor = Redactor(rules, **options)
    out = redactor.feed(data) + redactor.finish(final)
    return out, _result(redactor, len(out))


def redact_stream(
    source: BinaryIO,
    destination: BinaryIO,
    rules: Sequence[Mapping] | CompiledRules,
    *,
    final: bool = True,
    chunk_size: int = CHUNK_BYTES,
    **options,
) -> RedactionResult:
    """Redacts `source` into `destination` a block at a time."""
    redactor = Redactor(rules, **options)
    written = 0
    while block := source.read(chunk_size):
        out = redactor.feed(block)
        destination.write(out)
        written += len(out)
    out = redactor.finish(final)
    destination.write(out)
    written += len(out)
    return _result(redactor, written)


def _result(redactor: Redactor, output_size: int) -> RedactionResult:
    return RedactionResult(
        counts=dict(redactor.counts),
        source_size=redactor.source_size,
        source_offset=redactor.source_offset,
        trailing_partial=redactor.trailing_partial,
        output_size=output_size,
        refused_at=redactor.refused_at,
        union_at=redactor.union_at,
    )


@dataclass
class WindowResult:
    """A redacted read from the middle of a file. Offsets are indexes into
    the data passed in: the output is the redaction of data[begin:end] (plus
    the incomplete-line marker when trailing_partial > 0), and the next
    read should start at end. refused_at: None, or where the rest was
    refused (the output then ends with the refusal marker and end is the
    end of data); union_at: None, or where the content started to be read
    as UTF16_UNSURE, its lines ending at every newline byte from there."""

    data: bytes
    counts: dict[str, int]
    begin: int
    end: int
    aligned: bool
    trailing_partial: int
    refused_at: int | None = None
    union_at: int | None = None


def redact_window(
    data: bytes,
    start: int,
    rules: Sequence[Mapping] | CompiledRules,
    *,
    base_offset: int = 0,
    final: bool = False,
    **options,
) -> WindowResult:
    """Redacts data[start:] of a read that began base_offset bytes into the
    file, for a Tail poll or "open only the end". data[:start] is context
    read before the offset: it is never returned, only read to know whether
    `start` falls inside a private-key block.

    When `start` isn't the start of a line (it follows no newline in data,
    or nothing is known before it), the output starts at the next line
    instead and `aligned` is True, so a value cut by the offset is never
    shown in part."""
    redactor = Redactor(rules, **options)
    if redactor.passthrough:
        redactor.feed(data[start:])
        return WindowResult(bytes(data[start:]), {}, start, len(data), False, 0)
    unit = redactor._unit
    newline = redactor._newline

    def find(begin: int, end: int) -> int:
        index = data.find(newline, begin, end)
        while index >= 0 and (base_offset + index) % unit:
            index = data.find(newline, index + 1, end)
        return index

    begin = start
    at_line_start = base_offset + start == 0 or (
        start >= len(newline)
        and (base_offset + start) % unit == 0
        and data[start - len(newline) : start] == newline
    )
    if not at_line_start:
        index = find(start, len(data))
        begin = len(data) if index < 0 else index + len(newline)
    if base_offset == 0:
        context = 0
    else:
        index = find(0, begin)
        context = begin if index < 0 else index + len(newline)
    redactor._origin = base_offset + begin
    redactor.skip_context(bytes(data[context:begin]), base_offset + context)
    out = redactor.feed(data[begin:]) + redactor.finish(final)
    return WindowResult(
        data=out,
        counts=dict(redactor.counts),
        begin=begin,
        end=begin + redactor.source_offset,
        aligned=begin != start,
        trailing_partial=redactor.trailing_partial,
        refused_at=None if redactor.refused_at is None else redactor.refused_at - base_offset,
        union_at=None if redactor.union_at is None else redactor.union_at - base_offset,
    )


def head_digest(key: bytes, start: bytes, offset: int) -> str:
    """Identifies a file by its first min(HEAD_BYTES, offset) bytes for the
    Tail and the alerts (whether it was replaced or rotated): HMAC-SHA256
    with a per-source key (app.crypto.follow_head_key), truncated to 128
    bits, so a short, guessable start of a file (`password=1234`) can't be
    confirmed offline from the digest."""
    return hmac.new(key, start[: min(HEAD_BYTES, offset)], hashlib.sha256).hexdigest()[:32]
