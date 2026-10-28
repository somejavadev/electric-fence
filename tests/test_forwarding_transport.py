"""Tests for the syslog transport, against local servers only."""

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
import ipaddress
from pathlib import Path
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import pytest

from custom_components.electric_fence.forwarding.transport import (
    SyslogTransport,
    build_ssl_context,
    is_local_host,
)

pytestmark = pytest.mark.enable_socket


@pytest.fixture(autouse=True)
def allow_local(socket_enabled: None) -> None:
    """Let the local test servers use sockets."""


async def _wait(predicate: Callable[[], bool]) -> None:
    """Wait."""
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_tcp_newline_framing() -> None:
    """Tcp lines end with LF and carry no length prefix (non-transparent framing)."""
    data = bytearray()

    async def handle(reader: asyncio.StreamReader, _: asyncio.StreamWriter) -> None:
        while chunk := await reader.read(1024):
            data.extend(chunk)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    transport = SyslogTransport("127.0.0.1", port, "tcp", None)
    try:
        await transport.connect()
        assert transport.connected
        await transport.send(b"<132>one")
        await transport.send(b"<132>two!")
        await _wait(lambda: len(data) >= 19)
        assert bytes(data) == b"<132>one\n<132>two!\n"
    finally:
        await transport.close()
        server.close()
        await server.wait_closed()
    assert not transport.connected


async def test_udp_datagram() -> None:
    """Udp datagram."""
    got: list[bytes] = []

    class Server(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: object) -> None:
            got.append(data)

    loop = asyncio.get_running_loop()
    server, _ = await loop.create_datagram_endpoint(Server, local_addr=("127.0.0.1", 0))
    port = server.get_extra_info("sockname")[1]
    transport = SyslogTransport("127.0.0.1", port, "udp", None)
    try:
        await transport.connect()
        await transport.send(b"<132>hello")
        await transport.send(b"x" * 20000)
        await _wait(lambda: len(got) == 2)
        assert got[0] == b"<132>hello"
        assert len(got[1]) == 8192
    finally:
        await transport.close()
        server.close()


async def test_send_without_connection_raises() -> None:
    """Send without connection raises."""
    transport = SyslogTransport("127.0.0.1", 9, "tcp", None)
    with pytest.raises(OSError):
        await transport.send(b"x")


async def test_connect_refused_raises() -> None:
    """Connect refused raises."""
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    transport = SyslogTransport("127.0.0.1", port, "tcp", None)
    with pytest.raises(OSError):
        await transport.connect()
    assert not transport.connected


async def _send_until_failure(transport: SyslogTransport) -> None:
    """Send until the closed peer makes a send fail."""
    async with asyncio.timeout(5):
        while True:
            await transport.send(b"x" * 65536)
            await asyncio.sleep(0.01)


async def test_send_after_peer_closed_raises() -> None:
    """Send after peer closed raises."""

    async def handle(_: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    transport = SyslogTransport("127.0.0.1", port, "tcp", None)
    try:
        await transport.connect()
        with pytest.raises(OSError):
            await _send_until_failure(transport)
        assert not transport.connected
    finally:
        await transport.close()
        server.close()
        await server.wait_closed()


async def test_peer_close_after_first_line_is_noticed() -> None:
    """A peer that closes after one line makes the next send fail, not vanish."""
    got: list[bytes] = []

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        got.append(await reader.readline())
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    transport = SyslogTransport("127.0.0.1", port, "tcp", None)
    try:
        await transport.connect()
        await transport.send(b"<132>one")
        await _wait(lambda: got == [b"<132>one\n"])
        await asyncio.sleep(0.05)
        with pytest.raises(OSError):
            await transport.send(b"<132>two")
        assert not transport.connected
        await transport.connect()
        assert transport.connected
    finally:
        await transport.close()
        server.close()
        await server.wait_closed()


@pytest.fixture
def cert(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[Path, Path, str]:
    """Self-signed certificate for 127.0.0.1: (cert file, key file, cert PEM).

    A CA certificate by default. Parametrize with "server" for a plain server
    certificate, as most self-signed collectors have, or "named" for one that
    names only collector.example.
    """
    kind = getattr(request, "param", "ca")
    is_ca = kind == "ca"
    alt_name: x509.GeneralName = (
        x509.DNSName("collector.example")
        if kind == "named"
        else x509.IPAddress(ipaddress.ip_address("127.0.0.1"))
    )
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([alt_name]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=is_ca, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    pem = certificate.public_bytes(serialization.Encoding.PEM)
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_bytes(pem)
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_file, key_file, pem.decode()


@pytest.fixture
async def tls_server(
    cert: tuple[Path, Path, str],
) -> AsyncIterator[tuple[int, bytearray]]:
    """Tls server."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert[0], cert[1])
    data = bytearray()

    async def handle(reader: asyncio.StreamReader, _: asyncio.StreamWriter) -> None:
        while chunk := await reader.read(1024):
            data.extend(chunk)

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    # A rejected handshake is logged by the server side as an error.
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, _context: None)
    yield server.sockets[0].getsockname()[1], data
    loop.set_exception_handler(previous)
    server.close()
    await server.wait_closed()


async def test_tls_verify_and_custom_ca(
    tls_server: tuple[int, bytearray], cert: tuple[Path, Path, str]
) -> None:
    """Tls verify and custom ca."""
    port, data = tls_server
    strict = build_ssl_context("")
    failing = SyslogTransport("127.0.0.1", port, "tls", strict)
    with pytest.raises(OSError):
        await failing.connect()
    assert not failing.connected

    trusted = SyslogTransport("127.0.0.1", port, "tls", build_ssl_context(cert[2]))
    try:
        await trusted.connect()
        await trusted.send(b"<132>tls")
        await _wait(lambda: bytes(data) == b"<132>tls\n")
    finally:
        await trusted.close()


def test_tls_context_always_verifies(cert: tuple[Path, Path, str]) -> None:
    """The certificate and host name are checked, with or without a CA."""
    for context in (build_ssl_context(""), build_ssl_context(cert[2])):
        assert context.verify_mode is ssl.CERT_REQUIRED
        assert context.check_hostname
        assert context.minimum_version is ssl.TLSVersion.TLSv1_2


@pytest.mark.parametrize(
    ("host", "local"),
    [
        ("127.0.0.1", True),
        ("10.0.0.5", True),
        ("192.168.1.20", True),
        ("fd00::1", True),
        ("[fe80::1]", True),
        ("wazuh", True),
        ("wazuh.local", True),
        ("siem.lan", True),
        ("siem.home.arpa", True),
        ("SIEM.Internal.", True),
        ("8.8.8.8", False),
        ("2001:4860:4860::8888", False),
        ("siem.example.com", False),
    ],
)
def test_is_local_host(host: str, local: bool) -> None:
    """Private addresses and home-network names count as local."""
    assert is_local_host(host) is local


@pytest.mark.parametrize("cert", ["server"], indirect=True)
async def test_tls_trusts_a_pasted_self_signed_certificate(
    tls_server: tuple[int, bytearray], cert: tuple[Path, Path, str]
) -> None:
    """A self-signed server certificate pasted as the CA is trusted."""
    port, data = tls_server
    transport = SyslogTransport("127.0.0.1", port, "tls", build_ssl_context(cert[2]))
    try:
        await transport.connect()
        await transport.send(b"<132>self-signed")
        await _wait(lambda: bytes(data) == b"<132>self-signed\n")
    finally:
        await transport.close()


@pytest.mark.parametrize("cert", ["named"], indirect=True)
async def test_tls_rejects_an_address_the_certificate_is_not_for(
    tls_server: tuple[int, bytearray], cert: tuple[Path, Path, str]
) -> None:
    """A trusted certificate for a host name is refused when connecting by IP."""
    port, _ = tls_server
    transport = SyslogTransport("127.0.0.1", port, "tls", build_ssl_context(cert[2]))
    with pytest.raises(ssl.SSLCertVerificationError, match="127.0.0.1"):
        await transport.connect()
    assert not transport.connected
