#!/usr/bin/env python3
"""VM-side and external transport checks for a Telemt deployment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

import yaml


def load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("invalid YAML")
    return data


def endpoint(value: str) -> tuple[str, int]:
    host, port = value.rsplit(":", 1)
    return host.strip("[]"), int(port)


def http_get(url: str, timeout: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "telemt-setup-healthcheck/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        return response.read()


def api_users(config: dict) -> dict:
    host, port = endpoint(config["server"]["api_listen"])
    timeout = config["probes"]["timeout_seconds"]
    payload = json.loads(http_get(f"http://{host}:{port}/v1/users", timeout))
    if not isinstance(payload, dict) or payload.get("ok") is not True or not isinstance(payload.get("data"), list):
        raise RuntimeError("unexpected /v1/users response shape")
    if len(payload["data"]) != len(config["users"]):
        raise RuntimeError(f"API returned {len(payload['data'])} users, expected {len(config['users'])}")
    return payload


def api_ready(config: dict) -> dict:
    host, port = endpoint(config["server"]["api_listen"])
    timeout = config["probes"]["timeout_seconds"]
    url = f"http://{host}:{port}/v1/health/ready"
    try:
        body = http_get(url, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code != 503:
            raise
        body = exc.read()
    payload = json.loads(body)
    data = payload.get("data") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("ok") is not True
        or not isinstance(data, dict)
    ):
        raise RuntimeError("unexpected /v1/health/ready response shape")
    if data.get("ready") is not True or data.get("status") != "ready":
        reason = data.get("reason", "unspecified")
        raise RuntimeError(f"Telemt is not ready: {reason}")
    return payload


def api_telegram_upstream_ready(config: dict) -> dict | None:
    configured = config.get("upstreams", [])
    if not isinstance(configured, list) or not any(
        isinstance(upstream, dict) and upstream.get("enabled", True) for upstream in configured
    ):
        return None

    host, port = endpoint(config["server"]["api_listen"])
    timeout = config["probes"]["timeout_seconds"]
    url = f"http://{host}:{port}/v1/runtime/upstream-quality"
    payload = json.loads(http_get(url, timeout))
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("upstreams") if isinstance(data, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("ok") is not True
        or not isinstance(data, dict)
        or data.get("enabled") is not True
        or not isinstance(rows, list)
    ):
        reason = data.get("reason", "unexpected response") if isinstance(data, dict) else "unexpected response"
        raise RuntimeError(f"Telegram upstream status is unavailable: {reason}")

    telegram_routes = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("route_kind") == "socks5"
        and isinstance(row.get("scopes", ""), str)
        and not row.get("scopes", "").strip()
    ]
    if not telegram_routes:
        raise RuntimeError("Telemt reports no unscoped SOCKS5 route for Telegram")
    observed_healthy_routes = [
        row
        for row in telegram_routes
        if row.get("healthy") is True
        and isinstance(row.get("dc"), list)
        and any(
            isinstance(dc, dict) and dc.get("latency_ema_ms") is not None
            for dc in row["dc"]
        )
    ]
    if not observed_healthy_routes:
        raise RuntimeError(
            "Telemt reports no healthy unscoped SOCKS5 route with observed Telegram DC connectivity"
        )
    return payload


def check_vm(config: dict) -> None:
    service = config["install"]["service_name"] + ".service"
    subprocess.run(["systemctl", "is-active", "--quiet", service], check=True)
    version_output = subprocess.run(
        [config["install"]["binary_path"], "--version"], check=True, text=True, capture_output=True
    ).stdout
    if config["install"]["version"] not in version_output:
        raise RuntimeError("installed Telemt version does not match YAML")

    timeout = config["probes"]["timeout_seconds"]
    listen_host = config["server"]["listen_ip"]
    connect_host = "127.0.0.1" if listen_host == "0.0.0.0" else listen_host
    metrics_host, metrics_port = endpoint(config["server"]["metrics_listen"])
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    upstream_payload: dict | None = None
    while True:
        try:
            with socket.create_connection((connect_host, config["server"]["port"]), timeout=1):
                pass
            payload = api_users(config)
            api_ready(config)
            upstream_payload = api_telegram_upstream_ready(config)
            metrics = http_get(f"http://{metrics_host}:{metrics_port}/metrics", 2).decode("utf-8", "replace")
            break
        except Exception as exc:
            last_error = exc
            if time.monotonic() >= deadline:
                raise RuntimeError(f"service did not become ready within {timeout}s: {last_error}") from exc
            time.sleep(0.5)
    if not metrics.strip() or "#" not in metrics:
        raise RuntimeError("Prometheus endpoint returned no recognizable metrics")

    properties = subprocess.run(
        ["systemctl", "show", service, "-p", "MainPID", "-p", "NRestarts", "-p", "ActiveState"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip().replace("\n", " ")
    print(f"PASS vm service: {properties}")
    print(f"PASS proxy listener: {connect_host}:{config['server']['port']}")
    print(f"PASS API: {len(payload['data'])} user record(s), secrets suppressed")
    print("PASS readiness: admission open and at least one upstream healthy")
    if upstream_payload is not None:
        print("PASS Telegram route: at least one unscoped SOCKS5 upstream healthy")
    print(f"PASS metrics: {metrics_host}:{metrics_port}/metrics")


def check_e2e(config: dict) -> None:
    host = config["links"]["public_host"]
    port = config["links"]["public_port"]
    timeout = config["probes"]["timeout_seconds"]
    proxy = config.get("proxy")
    modes = proxy.get("modes") if isinstance(proxy, dict) else None
    domain = proxy.get("tls_domain") if isinstance(proxy, dict) else None
    if not isinstance(modes, dict) or modes.get("tls") is not True or not domain:
        raise RuntimeError("external Fake-TLS check requires TLS mode and proxy.tls_domain")
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    started = time.monotonic()
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=domain) as tls:
            version = tls.version()
            cipher = tls.cipher()[0] if tls.cipher() else "unknown"
            cert_len = len(tls.getpeercert(binary_form=True) or b"")
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if not version or cert_len == 0:
        raise RuntimeError("Fake-TLS handshake did not return a certificate")
    print(f"PASS external Fake-TLS: {host}:{port} SNI={domain} {version} {cipher} cert={cert_len}B {elapsed_ms}ms")
    print("INFO authenticated Telegram message delivery is not tested")


def show_links(config: dict) -> None:
    payload = api_users(config)
    print("WARNING: the following output contains proxy credentials")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=("vm", "e2e", "links"), required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = load(args.config)
        {"vm": check_vm, "e2e": check_e2e, "links": show_links}[args.scope](config)
        return 0
    except Exception as exc:
        print(f"FAIL {args.scope}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
