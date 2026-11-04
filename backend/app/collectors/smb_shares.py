"""Lists the disk shares on an SMB server (the share picker for an SMB
source's Base path, #159) with a minimal DCE/RPC client for SRVSVC
NetrShareEnum (MS-SRVS 3.1.4.8, opnum 15, level 1) over the `\\srvsvc`
named pipe -- smbprotocol has no share enumeration of its own, and pulling
in impacket for one call isn't worth it.

Only plain disk shares are returned: IPC$, printers and the hidden admin
shares (C$, ADMIN$ -- the STYPE_SPECIAL bit) are left out, matching
docs/source-setup.md's advice to share exactly the log folder instead."""

import struct
import uuid
from dataclasses import dataclass

from app.collectors.base import SourceUnreachableError

_SRVSVC_UUID = uuid.UUID("4b324fc8-1670-01d3-1278-5a47bf6ee188")
_SRVSVC_VERSION = 3
_NDR_UUID = uuid.UUID("8a885d04-1ceb-11c9-9fe8-08002b104860")
_NDR_VERSION = 2

_PTYPE_REQUEST = 0
_PTYPE_RESPONSE = 2
_PTYPE_FAULT = 3
_PTYPE_BIND = 11
_PTYPE_BIND_ACK = 12
_PFC_FIRST = 0x01
_PFC_LAST = 0x02
_MAX_FRAG = 4280

_STYPE_DISKTREE = 0
_STYPE_SPECIAL = 0x80000000


class ShareEnumError(SourceUnreachableError):
    public_message = "Couldn't list the shares on this server"


@dataclass(frozen=True)
class Share:
    name: str
    remark: str


def _header(ptype: int, body_len: int, call_id: int) -> bytes:
    # rpc_vers 5.0, flags first|last, little-endian/ASCII/IEEE data rep.
    return struct.pack(
        "<BBBB4sHHI",
        5,
        0,
        ptype,
        _PFC_FIRST | _PFC_LAST,
        b"\x10\x00\x00\x00",
        16 + body_len,
        0,
        call_id,
    )


def bind_pdu(call_id: int = 1) -> bytes:
    body = struct.pack("<HHI", _MAX_FRAG, _MAX_FRAG, 0)
    body += struct.pack("<BBH", 1, 0, 0)  # one presentation context
    body += struct.pack("<HBB", 0, 1, 0)  # context id 0, one transfer syntax
    body += _SRVSVC_UUID.bytes_le + struct.pack("<HH", _SRVSVC_VERSION, 0)
    body += _NDR_UUID.bytes_le + struct.pack("<I", _NDR_VERSION)
    return _header(_PTYPE_BIND, len(body), call_id) + body


def _ndr_wstring(value: str) -> bytes:
    chars = value + "\x00"
    encoded = chars.encode("utf-16-le")
    data = struct.pack("<III", len(chars), 0, len(chars)) + encoded
    return data + b"\x00" * (-len(data) % 4)


def share_enum_request_pdu(server: str, call_id: int = 2) -> bytes:
    stub = struct.pack("<I", 0x00020000) + _ndr_wstring(f"\\\\{server}")  # ServerName
    stub += struct.pack("<II", 1, 1)  # InfoStruct.Level = 1, union switch = 1
    stub += struct.pack("<I", 0x00020004)  # pointer to SHARE_INFO_1_CONTAINER
    stub += struct.pack("<II", 0, 0)  # EntriesRead = 0, Buffer = NULL
    stub += struct.pack("<I", 0xFFFFFFFF)  # PreferedMaximumLength
    stub += struct.pack("<I", 0)  # ResumeHandle = NULL
    body = struct.pack("<IHH", len(stub), 0, 15) + stub  # alloc_hint, ctx 0, opnum 15
    return _header(_PTYPE_REQUEST, len(body), call_id) + body


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def u32(self) -> int:
        if self.pos + 4 > len(self.data):
            raise ShareEnumError("truncated NetrShareEnum response")
        (value,) = struct.unpack_from("<I", self.data, self.pos)
        self.pos += 4
        return value

    def wstring(self) -> str:
        _max, _offset, actual = self.u32(), self.u32(), self.u32()
        end = self.pos + actual * 2
        if end > len(self.data):
            raise ShareEnumError("truncated string in NetrShareEnum response")
        text = self.data[self.pos : end].decode("utf-16-le").rstrip("\x00")
        self.pos = end + (-(actual * 2) % 4)
        return text


def parse_share_enum_stub(stub: bytes) -> list[tuple[str, int, str]]:
    """(name, type, remark) for every share in a level-1 response stub."""
    r = _Reader(stub)
    level, _switch, container_ptr = r.u32(), r.u32(), r.u32()
    if level != 1 or container_ptr == 0:
        raise ShareEnumError("unexpected NetrShareEnum response")
    count, buffer_ptr = r.u32(), r.u32()
    if buffer_ptr == 0 or count == 0:
        return []
    if r.u32() != count:  # conformant array max_count
        raise ShareEnumError("inconsistent share count")
    entries = [(r.u32(), r.u32(), r.u32()) for _ in range(count)]
    shares = []
    for name_ptr, share_type, remark_ptr in entries:
        name = r.wstring() if name_ptr else ""
        remark = r.wstring() if remark_ptr else ""
        shares.append((name, share_type, remark))
    return shares


def split_response_pdus(data: bytes) -> bytes:
    """Concatenates the stub data of one or more response fragments."""
    stub = b""
    pos = 0
    while pos < len(data):
        if len(data) - pos < 24:
            raise ShareEnumError("truncated DCE/RPC response")
        ptype, flags = data[pos + 2], data[pos + 3]
        (frag_len,) = struct.unpack_from("<H", data, pos + 8)
        if ptype == _PTYPE_FAULT:
            (status,) = struct.unpack_from("<I", data, pos + 24)
            raise ShareEnumError(f"server refused share enumeration (fault 0x{status:08x})")
        if ptype != _PTYPE_RESPONSE:
            raise ShareEnumError(f"unexpected DCE/RPC packet type {ptype}")
        stub += data[pos + 24 : pos + frag_len]
        pos += frag_len
        if flags & _PFC_LAST:
            break
    return stub


def disk_shares(entries: list[tuple[str, int, str]]) -> list[Share]:
    return sorted(
        (
            Share(name=name, remark=remark)
            for name, share_type, remark in entries
            if share_type & 0xFFFF == _STYPE_DISKTREE and not share_type & _STYPE_SPECIAL
        ),
        key=lambda s: s.name.lower(),
    )


def list_shares(host: str, connect_kwargs: dict) -> list[Share]:
    """Runs NetrShareEnum against `host` over IPC$. `connect_kwargs` are the
    same username/password/port/auth_protocol the SMB collector uses."""
    import smbclient

    smbclient.register_session(host, **connect_kwargs)
    pipe_path = f"\\\\{host}\\IPC$\\srvsvc"
    with smbclient.open_file(
        pipe_path, mode="w+b", buffering=0, file_type="pipe", **connect_kwargs
    ) as pipe:
        pipe.write(bind_pdu())
        ack = pipe.read(_MAX_FRAG)
        if len(ack) < 3 or ack[2] != _PTYPE_BIND_ACK:
            raise ShareEnumError("the server didn't accept the SRVSVC bind")
        pipe.write(share_enum_request_pdu(host))
        response = b""
        while True:
            chunk = pipe.read(65536)
            if not chunk:
                break
            response += chunk
            # Stop once the last fragment is complete.
            pos = 0
            done = False
            while pos + 10 <= len(response):
                (frag_len,) = struct.unpack_from("<H", response, pos + 8)
                if pos + frag_len > len(response):
                    break
                if response[pos + 3] & _PFC_LAST:
                    done = True
                    break
                pos += frag_len
            if done:
                break
    return disk_shares(parse_share_enum_stub(split_response_pdus(response)))
