"""SRVSVC NetrShareEnum encoding/decoding (#159). There is no SMB server in
CI, so the response side is checked against a hand-built level-1 stub laid
out as MS-SRVS/NDR specify; the request side against the spec's fixed
fields."""

import multiprocessing
import struct
import sys
import types

import pytest
from app.collectors import smb_shares


def _wstring(value: str) -> bytes:
    chars = value + "\x00"
    data = struct.pack("<III", len(chars), 0, len(chars)) + chars.encode("utf-16-le")
    return data + b"\x00" * (-len(data) % 4)


def _response_stub(shares):
    stub = struct.pack("<III", 1, 1, 0x20000)
    stub += struct.pack("<II", len(shares), 0x20004)
    stub += struct.pack("<I", len(shares))
    for i, (_name, share_type, remark) in enumerate(shares):
        stub += struct.pack("<III", 0x20008 + i * 8, share_type, 0x2000C + i * 8 if remark else 0)
    for name, _type, remark in shares:
        stub += _wstring(name)
        if remark:
            stub += _wstring(remark)
    return stub + struct.pack("<III", len(shares), 0, 0)


def _response_pdu(stub: bytes, flags=0x03) -> bytes:
    body = struct.pack("<IHBB", len(stub), 0, 0, 0) + stub
    return struct.pack("<BBBB4sHHI", 5, 0, 2, flags, b"\x10\0\0\0", 16 + len(body), 0, 2) + body


SHARES = [
    ("ADMIN$", 0x80000000, "Remote Admin"),
    ("C$", 0x80000000, "Default share"),
    ("IPC$", 0x80000003, "Remote IPC"),
    ("PTLogs", 0, "PerchTail lab logs"),
    ("AppLogs", 0, ""),
    ("Printer", 1, "printer"),
]


def test_parses_a_level_1_response_and_keeps_only_plain_disk_shares():
    entries = smb_shares.parse_share_enum_stub(_response_stub(SHARES))
    assert [e[0] for e in entries] == [s[0] for s in SHARES]
    shares = smb_shares.disk_shares(entries)
    assert [s.name for s in shares] == ["AppLogs", "PTLogs"]
    assert shares[1].remark == "PerchTail lab logs"


def test_reassembles_fragmented_responses():
    stub = _response_stub(SHARES)
    first, second = stub[:40], stub[40:]
    data = _response_pdu(first, flags=0x01) + _response_pdu(second, flags=0x02)
    assert smb_shares.split_response_pdus(data) == stub


def test_fault_pdu_raises():
    fault = struct.pack("<BBBB4sHHI", 5, 0, 3, 3, b"\x10\0\0\0", 32, 0, 2)
    fault += struct.pack("<IHBBI", 0, 0, 0, 0, 0x5)
    with pytest.raises(smb_shares.ShareEnumError, match="0x00000005"):
        smb_shares.split_response_pdus(fault)


def test_bind_and_request_pdus_have_the_spec_fields():
    bind = smb_shares.bind_pdu()
    assert bind[0] == 5 and bind[2] == 11
    assert struct.unpack_from("<H", bind, 8)[0] == len(bind)
    assert smb_shares._SRVSVC_UUID.bytes_le in bind

    request = smb_shares.share_enum_request_pdu("fileserver")
    assert request[2] == 0  # request
    assert struct.unpack_from("<H", request, 22)[0] == 15  # opnum NetrShareEnum
    assert "\\\\fileserver".encode("utf-16-le") in request


def test_truncated_stub_raises():
    with pytest.raises(smb_shares.ShareEnumError):
        smb_shares.parse_share_enum_stub(_response_stub(SHARES)[:30])


# --- a server that answers badly ---------------------------------------------


class _Pipe:
    """The `\\srvsvc` pipe of a server that accepts the bind and then sends
    `answer()` on every read, without end."""

    def __init__(self, answer):
        self._answer = answer
        self._bound = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write(self, data):
        pass

    def read(self, size):
        if not self._bound:
            self._bound = True
            return struct.pack("<BBBB4sHHI", 5, 0, 12, 3, b"\x10\0\0\0", 16, 0, 1)
        return self._answer()


def _fake_smbclient_module(answer) -> types.ModuleType:
    fake = types.ModuleType("smbclient")
    fake.register_session = lambda host, **kwargs: None
    fake.open_file = lambda path, **kwargs: _Pipe(answer)
    return fake


def _fragment(frag_len: int, flags: int) -> bytes:
    return struct.pack("<BBBB4sHHI", 5, 0, 2, flags, b"\x10\0\0\0", frag_len, 0, 2) + b"\0" * 8


# What the bad server sends on every read, by name, so the child process can
# pick it (a lambda doesn't cross the process boundary).
_BAD_ANSWERS = {
    "zero_fragment_length": lambda: _fragment(0, 0x01),
    "never_last_fragment": lambda: _fragment(24, 0x00) * 1000,
}

# The child process's limits: a loop that never ends is killed after the
# timeout, and one that keeps allocating hits the memory cap first.
_CHILD_TIMEOUT_SECONDS = 10
_CHILD_MEMORY_BYTES = 1024 * 1024 * 1024


def _run_list_shares(answer_name: str, conn) -> None:
    """The child process: list_shares against the named bad server; sends
    back the exception's type and message, or None when it returned."""
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (_CHILD_MEMORY_BYTES, _CHILD_MEMORY_BYTES))
    except (ImportError, ValueError, OSError):
        pass
    sys.modules["smbclient"] = _fake_smbclient_module(_BAD_ANSWERS[answer_name])
    try:
        smb_shares.list_shares("fileserver", {})
    except Exception as exc:
        conn.send((type(exc).__name__, str(exc)))
    else:
        conn.send(None)
    finally:
        conn.close()


def _list_shares_against(answer_name: str):
    """list_shares against a bad fake server, in a separate process: a loop
    that never ends fails the test and is killed, never left running in the
    test process. Returns (exception type name, message), or None if
    list_shares returned."""
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_run_list_shares, args=(answer_name, child), daemon=True)
    process.start()
    child.close()
    try:
        if not parent.poll(_CHILD_TIMEOUT_SECONDS):
            pytest.fail("list_shares never returned")
        return parent.recv()
    finally:
        parent.close()
        process.join(timeout=2)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        if process.is_alive():
            process.kill()
            process.join()


def test_a_zero_fragment_length_ends_with_an_error():
    outcome = _list_shares_against("zero_fragment_length")
    assert outcome is not None and outcome[0] == "ShareEnumError"


def test_a_response_that_never_ends_is_cut_off():
    outcome = _list_shares_against("never_last_fragment")
    assert outcome is not None and outcome[0] == "ShareEnumError"
    assert "too large" in outcome[1]


def test_split_refuses_a_fragment_shorter_than_its_headers():
    with pytest.raises(smb_shares.ShareEnumError, match="fragment length"):
        smb_shares.split_response_pdus(_fragment(0, 0x01) + _fragment(24, 0x02))


def test_list_shares_reads_a_well_formed_answer(monkeypatch):
    answers = iter([_response_pdu(_response_stub(SHARES))])
    monkeypatch.setitem(
        sys.modules, "smbclient", _fake_smbclient_module(lambda: next(answers, b""))
    )
    shares = smb_shares.list_shares("fileserver", {})
    assert [s.name for s in shares] == ["AppLogs", "PTLogs"]
