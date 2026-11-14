"""Address identities and socket authorities; no implicit DNS during validation."""
from __future__ import annotations

import ipaddress
import re
import socket
import queue
import threading

FAMILIES = {"ipv4": socket.AF_INET, "ipv6": socket.AF_INET6}
DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def literal(value: object) -> str:
    if not isinstance(value, str) or "%" in value:
        raise ValueError("expected a literal IP without zone ID")
    ip = ipaddress.ip_address(value)
    if ip.is_multicast or getattr(ip, "ipv4_mapped", None) is not None:
        raise ValueError("multicast and IPv4-mapped IPv6 are unsupported")
    return str(ip)


def host(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("expected an IP or DNS host")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if any(c in value for c in ":/%[]@?#\\"):
            raise ValueError("host must not contain URL syntax, port or zone ID")
        ascii_host = value.rstrip(".").encode("idna").decode("ascii").lower()
        if len(ascii_host) > 253 or not all(DNS_LABEL.fullmatch(label) for label in ascii_host.split(".")):
            raise ValueError("invalid DNS host")
        return ascii_host
    result = literal(value)
    if ipaddress.ip_address(result).is_unspecified:
        raise ValueError("public host cannot be a wildcard")
    return result


def family(value: str) -> str:
    return "ipv4" if ipaddress.ip_address(value).version == 4 else "ipv6"


def authority(value: str, port: int) -> str:
    return f"[{value}]:{port}" if ":" in value else f"{value}:{port}"


def loopback(value: str) -> str:
    return {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(value, value)


def overlaps(left: str, right: str) -> bool:
    a, b = ipaddress.ip_address(left), ipaddress.ip_address(right)
    # Telemt's IPv6 listen sockets explicitly set IPV6_V6ONLY.
    return a.version == b.version and (a == b or a.is_unspecified or b.is_unspecified)


def listen_ips(data: dict) -> list[str]:
    server = data["server"]
    return server["listen_ips"] if data.get("schema_version") == 2 else [server["listen_ip"]]


def endpoints(data: dict) -> list[dict]:
    links = data["links"]
    if data.get("schema_version") == 2:
        return links["endpoints"]
    value = links["public_host"]
    try:
        families = [family(value)]
    except ValueError:
        families = ["ipv4", "ipv6"]
    return [dict(id="default", host=value, port=links["public_port"], families=families)]


def primary(data: dict) -> dict:
    items = endpoints(data)
    if data.get("schema_version") != 2:
        return items[0]
    return next(item for item in items if item["id"] == data["links"]["primary"])


def resolve(value: str, port: int, selected_family: str, limit: int = 16) -> list[str]:
    rows = socket.getaddrinfo(value, port, FAMILIES[selected_family], socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(str(ipaddress.ip_address(row[4][0])) for row in rows))
    if not addresses:
        raise ValueError("DNS_MISSING")
    if len(addresses) > limit:
        raise ValueError("DNS_ADDRESS_LIMIT: incomplete check")
    return addresses


def resolve_bounded(value: str, port: int, selected_family: str, timeout: float) -> list[str]:
    """Bound OS resolver latency without a non-daemon executor blocking shutdown."""
    results = queue.Queue(maxsize=1)
    def worker():
        try:
            results.put((True, resolve(value, port, selected_family)))
        except Exception as exc:
            results.put((False, exc))
    threading.Thread(target=worker, daemon=True).start()
    try:
        ok, result = results.get(timeout=timeout)
    except queue.Empty as exc:
        raise TimeoutError("DNS resolution timed out") from exc
    if not ok:
        raise result
    return result
