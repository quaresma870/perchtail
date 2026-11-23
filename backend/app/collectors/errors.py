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
from app.archives import SKIP_ENCRYPTED
from app.collectors.base import (
    FetchTooLargeError,
    LinkTargetHiddenError,
    PathOutsideBaseError,
    SourceArchiveUnreadableError,
    SourceAuthError,
    SourceError,
    SourceHostKeyError,
    SourceLockedError,
    SourceMemberUnreadableError,
    SourceNotAFileError,
    SourceNotFoundError,
    SourcePermissionError,
    SourceTLSError,
    SourceUnreachableError,
    TargetCheck,
)
from app.collectors.ranged import RemoteFileChangedError
from app.rules import is_dir_visible, is_visible, rules_case_insensitive
from app.scratch import ScratchFullError

Translator = Callable[[BaseException], SourceError | None]

# NTSTATUS codes worth telling apart (MS-ERREF 2.3.1).
_NT_OBJECT_NAME_NOT_FOUND = 0xC0000034
_NT_OBJECT_PATH_NOT_FOUND = 0xC000003A
_NT_BAD_NETWORK_NAME = 0xC00000CC
_NT_ACCESS_DENIED = 0xC0000022
_NT_SHARING_VIOLATION = 0xC0000043
_NT_LOGON_FAILURE = 0xC000006D
_NT_FILE_IS_A_DIRECTORY = 0xC00000BA


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
    # A folder opened as a file: the connectors check first, so a link to a
    # folder has its rules checked on the folder (base.refuse_directory);
    # this only catches one that turned into a folder since.
    if isinstance(exc, IsADirectoryError) or (
        isinstance(exc, OSError) and exc.errno == errno.EISDIR
    ):
        return SourceNotAFileError(text)
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

    from app.collectors.ssh import PrivateKeyError

    text = str(exc)
    if isinstance(exc, PrivateKeyError):
        # Saving refuses an encrypted key; one saved before that, or a key
        # paramiko can't parse, still gets the neutral credential error.
        return SourceAuthError(text)
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
    if status == _NT_FILE_IS_A_DIRECTORY:
        return SourceNotAFileError(text)
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
    from pypsrp import exceptions as psrp_exc
    from winrm import exceptions as winrm_exc

    text = str(exc)
    # PSRP, for a source behind a JEA endpoint (#169).
    if isinstance(exc, psrp_exc.AuthenticationError):
        return SourceAuthError(text)
    if isinstance(exc, psrp_exc.WinRMTransportError) and exc.code == 401:
        return SourceAuthError(text)
    if isinstance(exc, psrp_exc.WSManFaultError | psrp_exc.WinRMTransportError):
        lowered = text.lower()
        if "access is denied" in lowered or getattr(exc, "code", None) == 403:
            return SourcePermissionError(
                text,
                public_message="The WinRM account isn't allowed to use the JEA endpoint "
                "(see docs/examples/jea)",
            )
        # Windows names a missing endpoint by its resource URI (fault 2150858811),
        # not as a missing "configuration".
        if "ws-management catalog" in lowered or (
            "configuration" in lowered and ("not found" in lowered or "cannot be found" in lowered)
        ):
            return SourceError(text, public_message="The JEA endpoint wasn't found on the server")
        return SourceUnreachableError(text)
    if isinstance(exc, psrp_exc.WinRMError):
        return SourceUnreachableError(text)
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


# What an agent before the is_directory code (0.7.0-rc.3 and earlier) ends
# its error with when the path to read is a folder.
_OLD_AGENT_IS_A_DIRECTORY = ": is a directory"


def is_directory_answer(exc) -> bool:
    """Whether an agent's AgentCommandError says the path to read is a
    folder: the code is_directory, or, from an older agent that sends no
    code, a message ending in ": is a directory"."""
    if exc.code == "is_directory":
        return True
    return exc.code is None and str(exc).endswith(_OLD_AGENT_IS_A_DIRECTORY)


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
        if exc.code == "link_target":
            # A link out of the base path (FEATURE_LINKS): 404, audited as
            # link_target_hidden, like a link the rules hide.
            return LinkTargetHiddenError(text)
        if exc.code in ("archive_unreadable", "gzip_unreadable"):
            # The archive, or the .gz file to decompress, can't be read
            # (damaged, or not that kind of file): said by its code since
            # the 0.7.0 agent, never guessed from the text.
            return SourceArchiveUnreadableError(text, gzip=exc.code == "gzip_unreadable")
        if exc.code == "member_encrypted":
            # A zip member stored encrypted, which the agent can't read.
            return SourceMemberUnreadableError(text, reason=SKIP_ENCRYPTED)
        if is_directory_answer(exc):
            # A folder read as a file. Content commands go through
            # agent._following_links, which checks the rules on a link's
            # target as a folder first.
            return SourceNotAFileError(text)
        lowered = text.lower()
        # Go's os.Root refuses a path through a symlink that leaves the base
        # ("openat x: path escapes from parent"), and the agent refuses
        # non-canonical paths itself: answered as 404, the same as a link
        # leaving the base path over SSH, rather than 502 (F-L).
        if "path escapes from parent" in lowered or lowered.startswith("unsafe path"):
            return PathOutsideBaseError(text)
        # A list on a file (#206): "not a directory", or on Windows "The
        # directory name is invalid."
        if "not a directory" in lowered or "directory name is invalid" in lowered:
            return SourceNotFoundError(text)
        # Windows agents say "The system cannot find the file (path)
        # specified." (#272).
        if any(
            phrase in lowered
            for phrase in ("not exist", "no such file", "not found", "cannot find the")
        ):
            return SourceNotFoundError(text)
        if "permission" in lowered or "denied" in lowered or "outside" in lowered:
            return SourcePermissionError(text)
        return SourceError(text)
    return _common(exc)


def translate_local(exc: BaseException) -> SourceError | None:
    return _common(exc)


# ScratchFullError (#345): refused before anything reached the source.
# RemoteFileChangedError: a ranged read found the file changed under it
# (collectors/ranged.py), answered by the archive routes.
_PASS_THROUGH = (
    SourceError,
    FetchTooLargeError,
    PathOutsideBaseError,
    ScratchFullError,
    RemoteFileChangedError,
)


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


def target_check_for(source, rules, label: str = "") -> TargetCheck:
    """The target_check (collectors.base.require_target) for a read on
    `source` with its visibility `rules`: where a link leads must be a file
    the rules show, on its virtual path (`label/...` inside a root of a
    source with several), or with `is_dir`, a folder they show (the read is
    then refused as not a file, collectors.base.refuse_directory). None, so
    no link is followed, without rules."""
    if rules is None:
        return None
    case_insensitive = rules_case_insensitive(source)
    prefix = f"{label}/" if label else ""

    def check(real: str, is_dir: bool = False) -> bool:
        if is_dir:
            virtual = f"{label}/{real}" if label and real else (label or real)
            return is_dir_visible(virtual, rules, case_insensitive=case_insensitive)
        return is_visible(prefix + real, rules, case_insensitive=case_insensitive)

    return check


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

    def _link_kwargs(self, source, rules, label: str) -> dict:
        """For a connector that follows links (FOLLOWS_LINKS): the
        target_check its reads get, built from the source's `rules` on the
        virtual path (`label/...` for a root of a source with several). With
        no rules, no link is followed (fail closed). SMB and WinRM refuse
        every link themselves and take no target_check."""
        if not getattr(self._module, "FOLLOWS_LINKS", False):
            return {}
        return {"target_check": target_check_for(source, rules, label)}

    def _split(self, source, relative_path: str):
        """(the source or its root, the path inside it, the root's label)."""
        if source_roots.is_multi_root(source):
            rooted, rest = source_roots.split(source, relative_path)
            return rooted, rest, relative_path.partition("/")[0]
        return source, relative_path, ""

    # `rules` are the source's visibility rules: a read through a link is
    # allowed only when they show where the link leads too
    # (collectors.base.require_target). The caller has already checked the
    # requested path itself.

    def fetch_file(self, source, relative_path: str, destination, *, rules=None):
        with tracked(source), translated(self._translate):
            target, path, label = self._split(source, relative_path)
            link = self._link_kwargs(source, rules, label)
            return self._module.fetch_file(target, path, destination, **link)

    def read_range(
        self, source, relative_path: str, offset: int, length: int, head: int = 0, *, rules=None
    ):
        """Incremental follow (#254): a RangeRead (collectors/base.py)."""
        with tracked(source), translated(self._translate):
            target, path, label = self._split(source, relative_path)
            link = self._link_kwargs(source, rules, label)
            return self._module.read_range(target, path, offset, length, head, **link)

    def call(self, name: str, source, *args, rules=None, **kwargs):
        """One of the collector's own functions that take the source first
        (the agent's redacted commands, #382, #249), with its errors
        translated and the source's reachability tracked like the calls
        above. Single-root sources only (push agents). `rules`, for one that
        reads content, are the source's visibility rules: through a link, it
        reads only when they show where the link leads."""
        link = self._link_kwargs(source, rules, "") if rules is not None else {}
        with tracked(source), translated(self._translate):
            return getattr(self._module, name)(source, *args, **kwargs, **link)

    @contextmanager
    def open_ranged(self, source, relative_path: str, *, rules=None):
        """For a connector with RANGED_READS: the file opened for ranged
        reads (collectors/ranged.py), checked as fetch_file checks it, for
        the length of the `with` block. Its reads are translated like the
        open; what the caller's own code raises propagates unchanged."""
        with ExitStack() as stack:
            with tracked(source), translated(self._translate):
                target, path, label = self._split(source, relative_path)
                link = self._link_kwargs(source, rules, label)
                remote = stack.enter_context(self._module.open_ranged(target, path, **link))
            yield _TranslatedRemote(remote, self._translate)

    @contextmanager
    def local_copy(self, source, relative_path: str, *, rules=None):
        # Only the fetch is translated; exceptions from the caller's `with`
        # body propagate unchanged.
        with ExitStack() as stack:
            with tracked(source), translated(self._translate):
                target, path, label = self._split(source, relative_path)
                link = self._link_kwargs(source, rules, label)
                local = stack.enter_context(self._module.local_copy(target, path, **link))
            yield local


class _TranslatedRemote:
    """A connector's RangedRemote whose reads raise SourceError types."""

    def __init__(self, remote, translate: Translator):
        self._remote = remote
        self._translate = translate
        self.size = remote.size

    def read_at(self, offset: int, length: int) -> bytes:
        with translated(self._translate):
            return self._remote.read_at(offset, length)

    def copy_range(self, offset: int, length: int, dst) -> int:
        with translated(self._translate):
            return self._remote.copy_range(offset, length, dst)
