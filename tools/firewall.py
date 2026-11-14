"""Family-specific owned UFW rules. Never adopt pre-existing allowances."""
from __future__ import annotations

import ipaddress
import re
from pathlib import Path
import addressing


def parse(tokens):
    """Normalize only supported simple allow rules; unknown syntax stays opaque."""
    if tokens[:2] != ["ufw", "allow"]:
        return None
    rest = tokens[2:]
    comment = None
    if "comment" in rest:
        pos = rest.index("comment")
        if pos != len(rest)-2:
            return None
        comment = rest[-1]; rest = rest[:pos]
    source, destination, protocol, port = "any", "any", None, None
    if len(rest) == 1 and re.fullmatch(r"[0-9]+/tcp", rest[0]):
        port = rest[0].split("/")[0]; protocol = "tcp"
    else:
        seen = set()
        while rest:
            if len(rest) < 2 or rest[0] not in {"from", "to", "port", "proto"} or rest[0] in seen:
                return None
            key, value = rest[:2]; rest = rest[2:]; seen.add(key)
            if key == "from": source = value
            elif key == "to": destination = value
            elif key == "port": port = value
            else: protocol = value
    if protocol != "tcp" or port is None or not port.isdigit() or not 1 <= int(port) <= 65535:
        return None
    try:
        src = None if source == "any" else ipaddress.ip_network(source, strict=False)
        dst = None if destination == "any" else ipaddress.ip_network(destination, strict=False)
    except ValueError:
        return None
    versions = {n.version for n in (src, dst) if n is not None}
    if len(versions) > 1:
        return None
    families = versions or {4, 6}
    return dict(port=int(port), src=src, dst=dst, families=families, comment=comment)


def same(left, right):
    a, b = parse(left), parse(right)
    if not a or not b: return False
    if any(a[k] != b[k] for k in ("port", "families", "comment")): return False
    for key in ("src", "dst"):
        x, y = a[key], b[key]
        if x == y: continue
        if x is None and y is not None and y.prefixlen == 0: continue
        if y is None and x is not None and x.prefixlen == 0: continue
        return False
    return True


def covers(existing, desired):
    a, b = parse(existing), parse(desired)
    if not a or not b or a["port"] != b["port"] or not b["families"] <= a["families"]:
        return False
    for key in ("src", "dst"):
        x, y = a[key], b[key]
        if x is None: continue
        if y is None or y.version != x.version or not y.subnet_of(x): return False
    return True


def desired(data, instance_id):
    rules = []
    for value in addressing.listen_ips(data):
        ip = ipaddress.ip_address(value)
        if ip.is_loopback: continue
        source = "0.0.0.0/0" if ip.version == 4 else "::/0"
        target = source if ip.is_unspecified else str(ip)
        rules.append(["ufw", "allow", "from", source, "to", target, "port", str(data["server"]["port"]),
                      "proto", "tcp", "comment", "telemt-setup:" + instance_id])
    return rules


def validate(manifest, instance_id):
    records = manifest.get("ufw_rules", [])
    if not isinstance(records, list): raise ValueError("invalid recorded UFW rules")
    for rule in records:
        parsed = parse(rule) if isinstance(rule, list) and all(isinstance(x, str) for x in rule) else None
        if (not parsed or parsed["comment"] != "telemt-setup:" + instance_id
                or parsed["src"] is None or parsed["src"].prefixlen != 0
                or len(parsed["families"]) != 1):
            raise ValueError("invalid recorded UFW rule ownership")
    if any(same(a, b) for index, a in enumerate(records) for b in records[index+1:]):
        raise ValueError("duplicate owned UFW records")
    return records


def saved_rules(root=Path("/etc/ufw")):
    """Read family-preserving UFW tuples; show added collapses wildcard families.

    Only the simple incoming TCP allowances we can own are accepted. The UFW
    CLI remains the writer; these files are never edited by the installer.
    """
    result = []
    for version, name in ((4, "user.rules"), (6, "user6.rules")):
        path = root / name
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            if not line.startswith("### tuple ### "):
                continue
            body, separator, encoded = line[len("### tuple ### "):].partition(" comment=")
            fields = body.split()
            if len(fields) != 7:
                continue
            action, proto, port, dst, sport, src, direction = fields
            if action != "allow" or proto != "tcp" or sport != "any" or direction != "in":
                continue
            try:
                comment = bytes.fromhex(encoded.strip()).decode("utf-8") if separator else ""
                if ipaddress.ip_network(src, strict=False).version != version or ipaddress.ip_network(dst, strict=False).version != version:
                    continue
            except (ValueError, UnicodeError):
                continue
            rule = ["ufw", "allow", "from", src, "to", dst, "port", port, "proto", proto]
            if comment:
                rule += ["comment", comment]
            if parse(rule):
                result.append(rule)
    return result
