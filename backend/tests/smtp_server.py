"""A small SMTP server for tests of app/email_delivery.py: listens on
127.0.0.1 only, speaks just enough SMTP (EHLO, STARTTLS, AUTH PLAIN, MAIL,
RCPT, DATA, QUIT) and records what it received. Also builds a throwaway CA
and server certificates, so TLS verification is tested for real."""

import base64
import datetime
import socket
import socketserver
import ssl
import threading
from dataclasses import dataclass, field
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

SERVER_NAME = "mail.example.test"


@dataclass
class Received:
    mail_from: str
    rcpt_to: list[str]
    data: bytes
    tls: bool
    auth: tuple[str, str] | None


@dataclass
class Certificates:
    ca_pem: str
    certfile: Path
    keyfile: Path


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def make_ca() -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name("PerchTail test CA"))
        .issuer_name(_name("PerchTail test CA"))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def make_server_certificates(directory: Path, dns_name: str = SERVER_NAME) -> Certificates:
    """A new CA and a server certificate for `dns_name` signed by it."""
    ca_key, ca_cert = make_ca()
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(dns_name))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(dns_name)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    directory.mkdir(parents=True, exist_ok=True)
    certfile = directory / f"{dns_name}.crt"
    keyfile = directory / f"{dns_name}.key"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    ca_pem = ca_cert.public_bytes(serialization.Encoding.PEM).decode()
    return Certificates(ca_pem=ca_pem, certfile=certfile, keyfile=keyfile)


@dataclass
class ServerOptions:
    certificates: Certificates
    implicit_tls: bool = False
    offer_starttls: bool = True
    offer_auth: bool = True
    password: str = "relay-password"
    refuse_recipient: bool = False
    received: list[Received] = field(default_factory=list)
    connections: int = 0


class _Handler(socketserver.BaseRequestHandler):
    def _send(self, line: str) -> None:
        self.conn.sendall(line.encode() + b"\r\n")

    def _readline(self) -> str | None:
        line = self.reader.readline()
        if not line:
            return None
        return line.decode("utf-8", "replace").rstrip("\r\n")

    def _wrap(self) -> bool:
        try:
            self.conn = self.server.tls_context.wrap_socket(self.conn, server_side=True)
        except (ssl.SSLError, OSError):
            return False
        self.reader = self.conn.makefile("rb")
        self.tls = True
        return True

    def handle(self) -> None:
        options: ServerOptions = self.server.options
        options.connections += 1
        self.conn = self.request
        self.tls = False
        if options.implicit_tls and not self._wrap():
            return
        if not self.tls:
            self.reader = self.conn.makefile("rb")
        self._send(f"220 {SERVER_NAME} ESMTP test")
        auth = None
        mail_from = ""
        rcpt_to: list[str] = []
        while True:
            line = self._readline()
            if line is None:
                return
            verb = line.split(" ", 1)[0].upper()
            if verb in ("EHLO", "HELO"):
                lines = [SERVER_NAME]
                if options.offer_starttls and not self.tls:
                    lines.append("STARTTLS")
                if options.offer_auth:
                    lines.append("AUTH PLAIN")
                lines.append("8BITMIME")
                for index, text in enumerate(lines):
                    sep = " " if index == len(lines) - 1 else "-"
                    self._send(f"250{sep}{text}")
            elif verb == "STARTTLS":
                self._send("220 Ready to start TLS")
                if not self._wrap():
                    return
            elif verb == "AUTH":
                parts = line.split(" ")
                decoded = base64.b64decode(parts[2]).split(b"\0")
                user, password = decoded[1].decode(), decoded[2].decode()
                if password == options.password:
                    auth = (user, password)
                    self._send("235 Authentication successful")
                else:
                    self._send("535 Authentication failed")
            elif verb == "MAIL":
                mail_from = line.split(":", 1)[1].strip()
                rcpt_to = []
                self._send("250 OK")
            elif verb == "RCPT":
                if options.refuse_recipient:
                    self._send("550 No such user")
                else:
                    rcpt_to.append(line.split(":", 1)[1].strip())
                    self._send("250 OK")
            elif verb == "DATA":
                self._send("354 End data with <CR><LF>.<CR><LF>")
                chunks = []
                while True:
                    raw = self.reader.readline()
                    if not raw or raw == b".\r\n":
                        break
                    chunks.append(raw)
                options.received.append(
                    Received(
                        mail_from=mail_from,
                        rcpt_to=rcpt_to,
                        data=b"".join(chunks),
                        tls=self.tls,
                        auth=auth,
                    )
                )
                self._send("250 OK queued")
            elif verb == "QUIT":
                self._send("221 Bye")
                return
            else:
                self._send("250 OK")


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass


class SmtpTestServer:
    def __init__(self, options: ServerOptions):
        self.options = options
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.options = options
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(options.certificates.certfile, options.certificates.keyfile)
        self._server.tls_context = context
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "SmtpTestServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()


# socket.getaddrinfo as it is at import time, before conftest's DNS stub
# replaces it for each test: the client needs it to reach 127.0.0.1 here.
REAL_GETADDRINFO = socket.getaddrinfo
