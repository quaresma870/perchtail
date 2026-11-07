"""#169: WinRM sources behind a JEA endpoint, over PSRP (pypsrp mocked)."""

import base64
import json

import app.collectors.winrm as winrm_module
import app.collectors.winrm_jea as jea
import pytest
from app.collectors.base import (
    PathOutsideBaseError,
    SourceAuthError,
    SourcePermissionError,
    SourceUnreachableError,
)
from app.collectors.errors import translate_winrm
from app.models import PatternKind, Protocol, Rule, RuleType, Source

SHOW_ALL = [
    Rule(source_id=1, order=0, type=RuleType.include, pattern="**", pattern_kind=PatternKind.glob)
]
FILE = b"line one\nline two\n"


def _source(**overrides) -> Source:
    defaults = dict(
        name="win01",
        protocol=Protocol.winrm,
        host="win01.example.com",
        base_path="D:\\Logs",
        credential_ref="encrypted",
        winrm_scheme="https",
        winrm_configuration="PerchTail",
    )
    defaults.update(overrides)
    return Source(**defaults)


class FakeEndpoint:
    """Stands in for pypsrp: records every pool and every command, and
    answers like the reference role would."""

    def __init__(self):
        self.pools: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.errors: dict[str, str] = {}

    def answer(self, command: str, params: dict) -> str:
        if command in self.errors:
            raise RuntimeError(self.errors[command])
        if command == "Get-PerchTailListing":
            listing = [
                {"Name": "app.log", "PSIsContainer": False, "Length": 18, "MTime": 1_000},
                {"Name": "old", "PSIsContainer": True, "Length": 0, "MTime": 2_000},
            ]
            return base64.b64encode(json.dumps(listing).encode()).decode()
        if command == "Get-PerchTailSize":
            return str(len(FILE))
        if command == "Read-PerchTailBlock":
            o, n = params["Offset"], params["Length"]
            return base64.b64encode(FILE[o : o + n]).decode()
        if command == "Read-PerchTailRange":
            o, n, h = params["Offset"], params["Length"], params["HeadLength"]
            head = base64.b64encode(FILE[:h]).decode()
            return f"{len(FILE)}|{head}|{base64.b64encode(FILE[o:o + n]).decode()}"
        if command == "Get-PerchTailRoots":
            return base64.b64encode(json.dumps(["D:\\Logs", "C:\\inetpub\\logs"]).encode()).decode()
        raise AssertionError(command)

    def install(self, monkeypatch):
        endpoint = self

        class Pool:
            def __init__(self, wsman, configuration_name=None):
                endpoint.pools.append({"wsman": wsman, "configuration_name": configuration_name})

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class Error:
            def __init__(self, text):
                self.text = text

            def __str__(self):
                return self.text

        class PowerShell:
            def __init__(self, pool):
                self.command = None
                self.params = {}
                self.had_errors = False
                self.streams = type("Streams", (), {"error": []})()

            def add_cmdlet(self, name):
                self.command = name
                return self

            def add_parameter(self, name, value):
                self.params[name] = value
                return self

            def add_script(self, *args, **kwargs):  # must never be used
                raise AssertionError("no script text may be sent to a JEA endpoint")

            def invoke(self):
                endpoint.calls.append((self.command, dict(self.params)))
                try:
                    return [endpoint.answer(self.command, self.params)]
                except RuntimeError as exc:
                    self.had_errors = True
                    self.streams.error = [Error(str(exc))]
                    return []

        monkeypatch.setattr(jea, "RunspacePool", Pool)
        monkeypatch.setattr(jea, "PowerShell", PowerShell)
        monkeypatch.setattr(jea, "WSMan", lambda host, **kwargs: {"host": host, **kwargs})
        monkeypatch.setattr(
            jea, "decrypt_credential", lambda ref: {"username": "svc", "password": "s3cret"}
        )


@pytest.fixture()
def endpoint(monkeypatch):
    fake = FakeEndpoint()
    fake.install(monkeypatch)
    # The WinRS path must never be used for a JEA source.
    monkeypatch.setattr(
        winrm_module, "_run_ps", lambda *a, **k: pytest.fail("WinRS used for a JEA source")
    )
    return fake


def test_listing_calls_the_role_function_on_the_named_endpoint(endpoint):
    entries = winrm_module.list_directory(_source(), SHOW_ALL, "app")
    assert [(e.name, e.is_dir, e.size, e.mtime) for e in entries] == [
        ("app.log", False, 18, 1.0),
        ("old", True, 0, 2.0),
    ]
    assert endpoint.calls == [("Get-PerchTailListing", {"Path": "D:\\Logs\\app"})]
    assert endpoint.pools[0]["configuration_name"] == "PerchTail"


def test_the_endpoint_uses_the_sources_tls_settings(endpoint, monkeypatch, tmp_path):
    monkeypatch.setattr(winrm_module, "_ca_bundle_path", lambda pem: "/data/winrm_ca/x.pem")
    winrm_module.list_directory(_source(port=5986, tls_ca_pem="PEM"), SHOW_ALL)
    wsman = endpoint.pools[0]["wsman"]
    assert wsman["host"] == "win01.example.com"
    assert (wsman["port"], wsman["ssl"], wsman["cert_validation"]) == (
        5986,
        True,
        "/data/winrm_ca/x.pem",
    )
    assert (wsman["username"], wsman["auth"]) == ("svc", "ntlm")

    winrm_module.list_directory(_source(winrm_scheme="http", port=None), SHOW_ALL)
    wsman = endpoint.pools[1]["wsman"]
    assert (wsman["port"], wsman["ssl"]) == (5985, False)


def test_fetch_reads_blocks_through_one_runspace(endpoint, tmp_path, monkeypatch):
    monkeypatch.setattr(jea, "_CHUNK_BYTES", 5)
    destination = tmp_path / "out"
    winrm_module.fetch_file(_source(), "app.log", destination)
    assert destination.read_bytes() == FILE
    assert len(endpoint.pools) == 1
    assert [c for c, _ in endpoint.calls][:2] == ["Get-PerchTailSize", "Read-PerchTailBlock"]
    assert [p["Offset"] for c, p in endpoint.calls if c == "Read-PerchTailBlock"] == [0, 5, 10, 15]


def test_fetch_refuses_a_file_over_the_limit(endpoint, tmp_path, monkeypatch):
    monkeypatch.setattr("app.collectors.base.max_fetch_bytes", lambda: 8)
    with pytest.raises(Exception, match="MAX_FETCH_MB"):
        winrm_module.fetch_file(_source(), "app.log", tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_read_range_caps_what_it_asks_for(endpoint):
    result = winrm_module.read_range(_source(), "app.log", 9, 10**9, 10**9)
    assert result == (18, FILE, b"line two\n")
    ((_, params),) = endpoint.calls
    assert (params["Length"], params["HeadLength"]) == (1 << 20, 4096)


def test_drives_are_the_roles_roots(endpoint):
    assert winrm_module.list_drives(_source()) == ["C:\\inetpub\\logs", "D:\\Logs"]


@pytest.mark.parametrize(
    "message, error",
    [
        ("PERCHTAIL_OUTSIDE_BASE", PathOutsideBaseError),
        ("PERCHTAIL_NOT_A_DIRECTORY", NotADirectoryError),
        ("PERCHTAIL_NOT_FOUND", FileNotFoundError),
        ("something else", RuntimeError),
    ],
)
def test_the_roles_refusals_become_errors(endpoint, message, error):
    endpoint.errors["Get-PerchTailListing"] = message
    with pytest.raises(error):
        winrm_module.list_directory(_source(), SHOW_ALL, "x")


def test_a_source_without_an_endpoint_keeps_the_winrs_shell(monkeypatch):
    seen = []
    monkeypatch.setattr(winrm_module, "_run_ps", lambda source, script: seen.append(script) or "")
    monkeypatch.setattr(
        jea, "RunspacePool", lambda *a, **k: pytest.fail("PSRP used without an endpoint")
    )
    assert winrm_module.list_directory(_source(winrm_configuration=None), SHOW_ALL) == []
    assert seen and "Get-ChildItem" in seen[0]


def test_psrp_errors_are_translated():
    from pypsrp import exceptions as psrp_exc

    assert isinstance(translate_winrm(psrp_exc.AuthenticationError("bad")), SourceAuthError)
    denied = psrp_exc.WinRMTransportError("http", 403, "Access is denied")
    mapped = translate_winrm(denied)
    assert isinstance(mapped, SourcePermissionError)
    assert "JEA endpoint" in mapped.public_message


def test_a_missing_endpoint_is_named():
    """What pt-win answers for an endpoint name that isn't registered (lab, #169)."""
    from pypsrp import exceptions as psrp_exc

    missing = psrp_exc.WSManFaultError(
        2150858811,
        "192.168.77.10",
        "The resource URI (http://schemas.microsoft.com/powershell/NaoExiste) "
        "was not found in the WS-Management catalog.",
        None,
        None,
        None,
    )
    mapped = translate_winrm(missing)
    assert not isinstance(mapped, SourceUnreachableError)
    assert mapped.public_message == "The JEA endpoint wasn't found on the server"
