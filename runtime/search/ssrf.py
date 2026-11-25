"""SSRF Protection and Destination URL Validator
Ensures outgoing page fetches strictly target public Internet addresses and blocks
access to private subnets, localhost, cloud metadata endpoints, loopback, and link-local ranges.
Provides socket-level validation to defeat DNS rebinding attacks.
"""

import socket
import ipaddress
import logging
import httpx
import httpcore
from urllib.parse import urlparse
from typing import Tuple

logger = logging.getLogger("presence.search.ssrf")

DISALLOWED_SCHEMES = frozenset({"file", "ftp", "gopher", "data", "blob", "javascript", "mailto", "dict"})
BLOCKED_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "broadcasthost", "ip6-localhost", "ip6-loopback"})


def is_ip_disallowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Checks if an IP address belongs to private, loopback, link-local, or reserved blocks."""
    # RFC 6052 Well-Known Prefix for IPv4/IPv6 translation (64:ff9b::/96)
    # Python stdlib marks 64:ff9b::/96 as is_reserved=True, but it is global NAT64 translation used by ISPs/DNS64.
    if isinstance(ip, ipaddress.IPv6Address) and ip in ipaddress.ip_network("64:ff9b::/96"):
        embedded_v4 = ipaddress.IPv4Address(ip.packed[12:16])
        return is_ip_disallowed(embedded_v4)

    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return True

    # Carrier-grade NAT (100.64.0.0/10)
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"):
        return True

    # IPv4-mapped IPv6 address check (::ffff:127.0.0.1)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return is_ip_disallowed(ip.ipv4_mapped)

    # 6to4 relay (2002::/16, RFC 7526 deprecated, can encapsulate private IPv4 in bytes 2..6)
    if isinstance(ip, ipaddress.IPv6Address) and ip in ipaddress.ip_network("2002::/16"):
        embedded_v4 = ipaddress.IPv4Address(ip.packed[2:6])
        if is_ip_disallowed(embedded_v4):
            return True

    # Cloud metadata service endpoint (169.254.169.254)
    if str(ip) == "169.254.169.254":
        return True

    return False


def validate_destination_url(url: str) -> Tuple[bool, str]:
    """Strictly validates a URL for public network safety before establishing connection.

    Returns:
        Tuple of (is_safe: bool, reason: str)
    """
    if not url or not isinstance(url, str):
        return False, "URL must be a non-empty string"

    url_str = url.strip()
    try:
        parsed = urlparse(url_str)
    except Exception as e:
        return False, f"Malformed URL syntax: {e}"

    # 1. Scheme validation
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, f"Disallowed URL scheme '{scheme}'. Only HTTP and HTTPS are permitted."

    # 2. Userinfo rejection (credentials in URL are disallowed to prevent parser confusion)
    if parsed.username or parsed.password:
        return False, "Userinfo (credentials) in URL is disallowed (security policy)."

    # 3. Hostname validation
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False, "URL must contain a valid host name"

    if host in BLOCKED_HOSTNAMES or host.endswith(".local") or host.endswith(".internal"):
        return False, f"Access to local / internal host '{host}' is strictly blocked (SSRF security policy)."

    # 4. Direct IP literal check
    try:
        ip = ipaddress.ip_address(host)
        if is_ip_disallowed(ip):
            return False, f"Direct access to private or restricted IP '{ip}' is blocked (SSRF security policy)."
        return True, "Valid public IP literal"
    except ValueError:
        pass  # Host is not standard IP literal, check encoded forms

    # 4b. Encoded IP representations check (integer, hex, octal)
    if host.isdigit():
        val = int(host)
        if 0 <= val <= 0xFFFFFFFF:
            ip = ipaddress.IPv4Address(val)
            if is_ip_disallowed(ip):
                return False, f"Direct access to integer IP literal '{ip}' is blocked (SSRF security policy)."
    elif (host.startswith("0x") or host.startswith("0X")) and len(host) > 2:
        try:
            val = int(host, 16)
            if 0 <= val <= 0xFFFFFFFF:
                ip = ipaddress.IPv4Address(val)
                if is_ip_disallowed(ip):
                    return False, f"Direct access to hex IP literal '{ip}' is blocked (SSRF security policy)."
        except ValueError:
            pass

    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        try:
            octets = [int(p, 8) if p.startswith("0") and len(p) > 1 else int(p) for p in parts]
            if all(0 <= o <= 255 for o in octets):
                ip = ipaddress.IPv4Address(bytes(octets))
                if is_ip_disallowed(ip):
                    return False, f"Direct access to encoded IP literal '{ip}' is blocked (SSRF security policy)."
        except Exception:
            pass

    # 5. Resolve DNS and verify all returned addresses
    try:
        addr_info = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        return False, f"Could not resolve host '{host}': {e}"
    except Exception as e:
        return False, f"DNS resolution error for '{host}': {e}"

    if not addr_info:
        return False, f"DNS resolution returned no addresses for host '{host}'"

    for entry in addr_info:
        sockaddr = entry[4]
        ip_str = sockaddr[0]
        try:
            resolved_ip = ipaddress.ip_address(ip_str)
            if is_ip_disallowed(resolved_ip):
                logger.warning(
                    f"SSRF BLOCKED: Host '{host}' resolved to restricted address '{resolved_ip}'"
                )
                return False, f"Host '{host}' resolves to restricted address '{resolved_ip}' (SSRF security policy)."
        except ValueError:
            return False, f"Invalid resolved IP format '{ip_str}'"

    return True, "Valid public destination"


class SSRFSafeBackend(httpcore.SyncBackend):
    """Network backend that verifies resolved and connected socket IP addresses against SSRF policies.
    Guarantees immunity against DNS rebinding (TOCTOU) attacks.
    """

    def connect_tcp(self, host: str, port: int, timeout=None, local_address=None, socket_options=None):
        # 1. Pre-connect check: Resolve host and verify every candidate IP
        try:
            addr_info = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
            for entry in addr_info:
                ip_str = entry[4][0]
                if is_ip_disallowed(ipaddress.ip_address(ip_str)):
                    raise httpcore.ConnectError(
                        f"SSRF blocked: host '{host}' resolves to restricted IP '{ip_str}'"
                    )
        except socket.gaierror as gai:
            raise httpcore.ConnectError(f"DNS resolution failure for '{host}': {gai}") from gai

        # 2. Establish connection
        stream = super().connect_tcp(
            host, port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )

        # 3. Post-connect check: Inspect the actual connected socket peer IP (defeats DNS rebinding)
        sock = getattr(stream, "_sock", None) or getattr(stream, "sock", None)
        if sock:
            peer_ip_str = sock.getpeername()[0]
            peer_ip = ipaddress.ip_address(peer_ip_str)
            if is_ip_disallowed(peer_ip):
                sock.close()
                logger.error(
                    f"DNS REBINDING DETECTED: Socket connected to restricted IP '{peer_ip}'. Connection closed."
                )
                raise httpcore.ConnectError(
                    f"DNS rebinding attack blocked: socket connected to restricted IP '{peer_ip}'"
                )

        return stream


class SSRFSafeTransport(httpx.HTTPTransport):
    """Custom HTTPX transport backed by SSRFSafeBackend for socket-level destination verification."""

    def __init__(self, *args, **kwargs):
        # Explicitly enforce trust_env=False unless overridden
        if "trust_env" not in kwargs:
            kwargs["trust_env"] = False
        super().__init__(*args, **kwargs)
        self._pool._network_backend = SSRFSafeBackend()
