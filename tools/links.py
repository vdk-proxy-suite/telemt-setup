"""Explicit credential-bearing link catalogue. Never called by health checks."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urlsplit

import addressing


def catalogue(config: dict, payload: dict, endpoint_id: str | None = None) -> dict:
    endpoints = addressing.endpoints(config)
    if endpoint_id is not None:
        endpoints = [item for item in endpoints if item["id"] == endpoint_id]
        if not endpoints:
            raise ValueError("unknown endpoint id")
    rows = payload.get("data")
    if payload.get("ok") is not True or not isinstance(rows, list):
        raise ValueError("unexpected user API response")
    names = [row.get("username") for row in rows if isinstance(row, dict)]
    if len(names) != len(rows) or len(set(names)) != len(names) or set(names) != set(config["users"]):
        raise ValueError("active users differ from configuration")
    result = []
    for row in rows:
        variants = []
        active_secret = None
        for mode in ("classic", "secure", "tls"):
            if not config["proxy"]["modes"][mode]:
                continue
            native = row.get("links", {}).get(mode)
            if not isinstance(native, list) or not native:
                raise ValueError("active API does not supply the configured link mode")
            secrets = set()
            for link in native:
                parsed = urlsplit(link)
                query = parse_qs(parsed.query, strict_parsing=True)
                if parsed.scheme != "tg" or parsed.netloc != "proxy" or len(query.get("secret", [])) != 1:
                    raise ValueError("unexpected native link format")
                secrets.add(query["secret"][0])
            if len(secrets) != 1:
                raise ValueError("active link modes/domains differ from configuration")
            secret = secrets.pop()
            prefix = {"classic": "", "secure": "dd", "tls": "ee"}[mode]
            suffix = config["proxy"]["tls_domain"].encode().hex() if mode == "tls" else ""
            pattern = re.escape(prefix) + r"([0-9a-fA-F]{32})" + re.escape(suffix)
            match = re.fullmatch(pattern, secret)
            if match is None:
                raise ValueError("active secret format or TLS domain differs from configuration")
            base = match.group(1).lower()
            configured = config["users"][row["username"]]["secret"]
            if configured != "GENERATE" and base != configured.lower():
                raise ValueError("active user secret differs from configuration")
            if active_secret is not None and base != active_secret:
                raise ValueError("active modes disagree on user secret")
            active_secret = base
            for item in endpoints:
                query = urlencode(dict(server=item["host"], port=item["port"], secret=secret))
                variants.append(dict(endpoint_id=item["id"], host=item["host"], port=item["port"], mode=mode,
                                     tg_url="tg://proxy?" + query, https_url="https://t.me/proxy?" + query))
        result.append(dict(username=row["username"], links=variants))
    return dict(schema_version=2, instance=config.get("instance", {}).get("id"),
                primary=config["links"].get("primary", "default"), users=result)
