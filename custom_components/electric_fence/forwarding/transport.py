"""Syslog transport over UDP, TCP and TLS.

TCP and TLS use non-transparent framing (RFC 6587 §3.4.2): each line ends
with LF, no length prefix, which is what Wazuh's remoted and Splunk's raw
TCP input read. `syslog_line` never contains a raw LF. UDP sends one line
per datagram.
"""

import asyncio
import contextlib
import ipaddress
import ssl
from typing import Literal

CONNECT_TIMEOUT = 10.0
UDP_MAX = 8192
# Names that only resolve on a home network.
_LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localdomain", ".arpa")


def is_local_host(host: str) -> bool:
    """Return whether the host is on the home network.

    Private, loopback and link-local addresses count, and so do names without
    a dot or ending in a home-network suffix. Names are not resolved.
    """
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        name = host.strip().rstrip(".").lower()
        return "." not in name or name.endswith(_LOCAL_SUFFIXES)
    return address.is_private or address.is_loopback or address.is_link_local


def build_ssl_context(ca_pem: str) -> ssl.SSLContext:
    """Return the TLS context. Loads files, so it runs in the executor.

    The certificate and host name are always checked: events go out unmasked,
    so an unchecked connection would hand them to whoever answers. TLS 1.0
    and 1.1 are refused even where the system's OpenSSL still allows them.
    """
    if ca_pem.strip():
        context = ssl.create_default_context(cadata=ca_pem)
    else:
        context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class _Datagram(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.lost = False

    def error_received(self, exc: Exception) -> None:
        self.lost = True

    def connection_lost(self, exc: Exception | None) -> None:
        self.lost = True


class SyslogTransport:
    """One connection to a syslog receiver."""

    def __init__(
        self,
        host: str,
        port: int,
        protocol: Literal["tcp", "udp", "tls"],
        ssl_context: ssl.SSLContext | None,
    ) -> None:
        """Set up a transport; nothing connects until `connect`."""
        self._host = host
        self._port = port
        self._protocol = protocol
        self._ssl = ssl_context
        self._writer: asyncio.StreamWriter | None = None
        # Never read from: kept so a peer's close (EOF) is noticed.
        self._reader: asyncio.StreamReader | None = None
        self._datagram: asyncio.DatagramTransport | None = None
        self._udp: _Datagram | None = None

    @property
    def connected(self) -> bool:
        """Return True while a connection is open."""
        if self._writer is not None:
            return not self._writer.is_closing()
        if self._datagram is not None:
            return not self._datagram.is_closing()
        return False

    async def connect(self) -> None:
        """Open the connection; raise OSError on failure."""
        await self.close()
        if self._protocol == "udp":
            loop = asyncio.get_running_loop()
            async with asyncio.timeout(CONNECT_TIMEOUT):
                transport, protocol = await loop.create_datagram_endpoint(
                    _Datagram, remote_addr=(self._host, self._port)
                )
            self._datagram = transport
            self._udp = protocol
            return
        tls = self._protocol == "tls"
        async with asyncio.timeout(CONNECT_TIMEOUT):
            self._reader, self._writer = await asyncio.open_connection(
                self._host,
                self._port,
                ssl=self._ssl if tls else None,
                server_hostname=self._host if tls else None,
            )

    async def send(self, line: bytes) -> None:
        """Send one line; raise OSError when it can't be delivered."""
        try:
            if self._datagram is not None:
                if self._udp is not None and self._udp.lost:
                    raise ConnectionError("datagram endpoint lost")
                self._datagram.sendto(line[:UDP_MAX])
                return
            if self._writer is None or self._writer.is_closing():
                raise ConnectionError("not connected")
            if self._reader is not None and self._reader.at_eof():
                # A write to a half-closed socket succeeds and is lost.
                raise ConnectionError("closed by the receiver")
            self._writer.write(line + b"\n")
            await self._writer.drain()
        except OSError:
            await self.close()
            raise

    async def close(self) -> None:
        """Close the connection; never raises."""
        writer, self._writer = self._writer, None
        self._reader = None
        datagram, self._datagram = self._datagram, None
        self._udp = None
        if datagram is not None:
            datagram.close()
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
