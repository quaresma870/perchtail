"""Turns each protocol library's exceptions into the SourceError types in
collectors/base.py, so a permission problem, a missing file, a locked file
or an unreachable host is answered with its own status and a message
anyone can act on -- instead of a bare 500 (#170). Applied once, in the
connector registry, around every list/fetch call."""

import errno
import socket
import ssl
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from types import ModuleType

from app import source_roots
from app.collectors.base import (
    FetchTooLargeError,
    PathOutsideBaseError,
    RangeNotSupportedError,
    SourceAuthError,
    SourceError,
    SourceHostKeyError,
    SourceLockedError,
    SourceNotFoundError,
    SourcePermissionError,
    SourceTLSError,
    SourceUnreachableError,
)

Translator = Callable[[BaseException], SourceError | None]

# NTSTATUS codes worth telling apart (MS-ERREF 2.3.1).
_NT_OBJECT_NAME_NOT_FOUND = 0xC0000034
_NT_OBJECT_PATH_NOT_FOUND = 0xC000003A
_NT_BAD_NETWORK_NAME = 0xC00000CC
_NT_ACCESS_DENIED = 0xC0000022
_NT_SHARING_VIOLATION = 0xC0000043
_NT_LOGON_FAILURE = 0xC000006D


# OpenSSL's X509_V_ERR_* codes, as ssl.SSLCertVerificationError.verify_code.
_X509_EXPIRED = 10
_X509_ISSUER_CODES = frozenset({2, 18, 19, 20, 21})
_X509_NAME_MISMATCH = frozenset({62, 64})  # hostname, IP address


def _cert_verify_code(exc: BaseException | None) -> int | None:
    """The OpenSSL verify code behind a TLS failure, however deeply
    requests/urllib3 wrapped it (SSLError -> MaxRetryError -> SSLError ->
    SSLCertVerificationError)."""
    seen: set[int] = set()
    stack = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return current.verify_code
        stack.extend([current.__cause__, current.__context__, getattr(current, "reason", None)])
        stack.extend(arg for arg in getattr(current, "args", ()) if isinstance(arg, BaseException))
    return None


def _tls_error(exc: BaseException) -> SourceTLSError:
    """Says which TLS check failed (F-N, #203): a certificate for another
    name (host or IP) needs a different fix (the host field, or reissuing
    it) than one from an unknown CA (the CA certificate field) or an
    expired one. Decided by OpenSSL's verify code when there is one, by the
    message otherwise."""
    text = str(exc)
    lowered = text.lower()
    code = _cert_verify_code(exc)
    if code in _X509_NAME_MISMATCH or (
        code is None
        and any(m in lowered for m in ("hostname mismatch", "ip address mismatch", "doesn't match"))
    ):
        return SourceTLSError(
            text,
            public_message="The source's TLS certificate is for a different name: enter the "
            "host as it appears on the certificate, or reissue the certificate for this host",
        )
    if code == _X509_EXPIRED or (code is None and "certificate has expired" in lowered):
        return SourceTLSError(text, public_message="The source's TLS certificate has expired")
    if code is None or code in _X509_ISSUER_CODES:
        return SourceTLSError(
            text,
            public_message="The source's TLS certificate isn't trusted: its issuer is unknown, "
            "so add the issuing CA certificate to the source",
        )
    return SourceTLSError(
        text, public_message=f"The source's TLS certificate was refused (verify error {code})"
    )


def _common(exc: BaseException) -> SourceError | None:
    text = str(exc)
    if isinstance(exc, ssl.SSLError):
        return _tls_error(exc)
    if isinstance(exc, PermissionError):
        return SourcePermissionError(text)
    if isinstance(exc, FileNotFoundError | NotADirectoryError):
        return SourceNotFoundError(text)
    if isinstance(exc, OSError) and exc.errno in (errno.ENOENT, errno.ENOTDIR):
        return SourceNotFoundError(text)
    if isinstance(exc, OSError) and exc.errno == errno.EACCES:
        return SourcePermissionError(text)
    if isinstance(exc, TimeoutError | socket.timeout | ConnectionError | socket.gaierror):
        return SourceUnreachableError(text)
    return None


def translate_ssh(exc: BaseException) -> SourceError | None:
    import paramiko

    text = str(exc)
    if isinstance(exc, paramiko.BadHostKeyException):
        return SourceHostKeyError(text)
    if isinstance(exc, paramiko.AuthenticationException):
        return SourceAuthError(text)
    if isinstance(exc, paramiko.ssh_exception.NoValidConnectionsError):
        return SourceUnreachableError(text)
    if isinstance(exc, paramiko.SSHException) and "known_hosts" in text:
        return SourceHostKeyError(
            text,
            public_message="The server's SSH host key isn't pinned and the policy is strict",
        )
    common = _common(exc)
    if common is not None:
        return common
    if isinstance(exc, paramiko.SSHException):
        return SourceUnreachableError(text)
    return None


def translate_smb(exc: BaseException) -> SourceError | None:
    from smbprotocol import exceptions as smb_exc

    text = str(exc)
    if isinstance(exc, smb_exc.LogonFailure | smb_exc.SMBAuthenticationError):
        return SourceAuthError(text)
    status = getattr(exc, "ntstatus", None)
    if status in (_NT_OBJECT_NAME_NOT_FOUND, _NT_OBJECT_PATH_NOT_FOUND):
        return SourceNotFoundError(text)
    if status == _NT_BAD_NETWORK_NAME:
        return SourceNotFoundError(
            text,
            public_message="That share doesn't exist on the server. For SMB, Base path is a "
            "share name (e.g. AppLogs), not a drive path.",
        )
    if status == _NT_ACCESS_DENIED:
        return SourcePermissionError(text)
    if status == _NT_SHARING_VIOLATION:
        return SourceLockedError(text)
    if status == _NT_LOGON_FAILURE:
        return SourceAuthError(text)
    if isinstance(exc, smb_exc.SMBConnectionClosed):
        return SourceUnreachableError(text)
    if isinstance(exc, ValueError) and "connect" in text.lower():
        return SourceUnreachableError(text)
    return _common(exc)


def _from_powershell_text(text: str) -> SourceError | None:
    lowered = text.lower()
    if "being used by another process" in lowered:
        return SourceLockedError(text)
    if "access to the path" in lowered and "denied" in lowered:
        return SourcePermissionError(text)
    if "cannot find path" in lowered or "does not exist" in lowered:
        return SourceNotFoundError(text)
    return None


def translate_winrm(exc: BaseException) -> SourceError | None:
    import requests
    from winrm import exceptions as winrm_exc

    text = str(exc)
    if isinstance(exc, winrm_exc.InvalidCredentialsError | winrm_exc.AuthenticationError):
        return SourceAuthError(text)
    if isinstance(exc, requests.exceptions.SSLError):
        return _tls_error(exc)
    if isinstance(exc, requests.exceptions.ConnectionError | requests.exceptions.Timeout):
        return SourceUnreachableError(text)
    if isinstance(exc, winrm_exc.WSManFaultError) and "access is denied" in text.lower():
        return SourcePermissionError(
            text,
            public_message="The WinRM account isn't allowed to run commands on the server "
            "(see docs/source-setup.md, WinRM)",
        )
    if isinstance(exc, winrm_exc.WinRMError):
        return SourceUnreachableError(text)
    if isinstance(exc, RuntimeError):
        return _from_powershell_text(text)
    return _common(exc)


def translate_agent(exc: BaseException) -> SourceError | PathOutsideBaseError | None:
    from app.agent_registry import (
        AgentCommandError,
        AgentNotConnectedError,
        AgentTimeoutError,
    )

    text = str(exc)
    if isinstance(exc, AgentNotConnectedError):
        return SourceUnreachableError(
            text, public_message="The agent for this source isn't connected"
        )
    if isinstance(exc, AgentTimeoutError):
        return SourceUnreachableError(text, public_message="The agent didn't answer in time")
    if isinstance(exc, AgentCommandError):
        lowered = text.lower()
        # Go's os.Root refuses a path through a symlink that leaves the base
        # ("openat x: path escapes from parent"), and the agent refuses
        # non-canonical paths itself: answered as 404, the same as a link
        # leaving the base path over SSH, rather than 502 (F-L).
        if "path escapes from parent" in lowered or lowered.startswith("unsafe path"):
            return PathOutsideBaseError(text)
        if "not a directory" in lowered:  # a list on a file (#206)
            return SourceNotFoundError(text)
        if "not exist" in lowered or "no such file" in lowered or "not found" in lowered:
            return SourceNotFoundError(text)
        if "permission" in lowered or "denied" in lowered or "outside" in lowered:
            return SourcePermissionError(text)
        return SourceError(text)
    return _common(exc)


def translate_local(exc: BaseException) -> SourceError | None:
    return _common(exc)


_PASS_THROUGH = (SourceError, FetchTooLargeError, PathOutsideBaseError, RangeNotSupportedError)


@contextmanager
def tracked(source) -> Iterator[None]:
    """Records the source's reachability from a real operation (#276): ok
    when it worked, failing for an error about reaching the source; an
    error about one file changes nothing. Re-raises whatever it sees."""
    from app import source_status

    source_id = getattr(source, "id", None)
    try:
        yield
    except Exception as exc:
        if source_status.is_connectivity_error(exc):
            source_status.record(source_id, False, source_status.failure_message(exc))
        raise
    source_status.record(source_id, True)


@contextmanager
def translated(translate: Translator) -> Iterator[None]:
    try:
        yield
    except _PASS_THROUGH:
        raise
    except Exception as exc:
        mapped = translate(exc)
        if mapped is None:
            raise
        raise mapped from exc


class TranslatingConnector:
    """Wraps a collector module so every list/fetch raises SourceError types.
    Anything else (helpers like local.resolve_path) is forwarded as is."""

    def __init__(self, module: ModuleType, translate: Translator):
        self._module = module
        self._translate = translate

    def __getattr__(self, name: str):
        return getattr(self._module, name)

    # A source with several roots (#161) is mapped here, once, so every
    # caller (browse, open, download, ZIP, search, alerts) sees the same
    # virtual `label/...` tree.

    def list_directory(self, source, rules, relative_path: str = ""):
        with tracked(source), translated(self._translate):
            if source_roots.is_multi_root(source):
                return source_roots.list_directory(self._module, source, rules, relative_path)
            return self._module.list_directory(source, rules, relative_path)

    def fetch_file(self, source, relative_path: str, destination):
        with tracked(source), translated(self._translate):
            if source_roots.is_multi_root(source):
                source, relative_path = source_roots.split(source, relative_path)
            return self._module.fetch_file(source, relative_path, destination)

    def read_range(self, source, relative_path: str, offset: int, length: int, head: int = 0):
        """Incremental follow (#254): a RangeRead (collectors/base.py)."""
        with tracked(source), translated(self._translate):
            if source_roots.is_multi_root(source):
                source, relative_path = source_roots.split(source, relative_path)
            return self._module.read_range(source, relative_path, offset, length, head)

    @contextmanager
    def local_copy(self, source, relative_path: str):
        # Only the fetch is translated; exceptions from the caller's `with`
        # body propagate unchanged.
        with ExitStack() as stack:
            with tracked(source), translated(self._translate):
                if source_roots.is_multi_root(source):
                    source, relative_path = source_roots.split(source, relative_path)
                local = stack.enter_context(self._module.local_copy(source, relative_path))
            yield local
