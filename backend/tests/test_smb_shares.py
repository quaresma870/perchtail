"""SRVSVC NetrShareEnum encoding/decoding (#159). There is no SMB server in
CI, so the response side is checked against a hand-built level-1 stub laid
out as MS-SRVS/NDR specify; the request side against the spec's fixed
fields."""

import struct

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
