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

from app.collectors.base import (
    FetchTooLargeError,
    PathOutsideBaseError,
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


def _common(exc: BaseException) -> SourceError | None:
    text = str(exc)
    if isinstance(exc, ssl.SSLError):
        return SourceTLSError(text)
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
        return SourceTLSError(text)
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


def translate_agent(exc: BaseException) -> SourceError | None:
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
        if "not exist" in lowered or "no such file" in lowered or "not found" in lowered:
            return SourceNotFoundError(text)
        if "permission" in lowered or "denied" in lowered or "outside" in lowered:
            return SourcePermissionError(text)
        return SourceError(text)
    return _common(exc)


def translate_local(exc: BaseException) -> SourceError | None:
    return _common(exc)


_PASS_THROUGH = (SourceError, FetchTooLargeError, PathOutsideBaseError)


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

    def list_directory(self, *args, **kwargs):
        with translated(self._translate):
            return self._module.list_directory(*args, **kwargs)

    def fetch_file(self, *args, **kwargs):
        with translated(self._translate):
            return self._module.fetch_file(*args, **kwargs)

    @contextmanager
    def local_copy(self, *args, **kwargs):
        # Only the fetch is translated; exceptions from the caller's `with`
        # body propagate unchanged.
        with ExitStack() as stack:
            with translated(self._translate):
                local = stack.enter_context(self._module.local_copy(*args, **kwargs))
            yield local
