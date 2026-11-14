#!/usr/bin/env python3
"""VM-side and external transport checks for a Telemt deployment."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

import yaml
import config as schema
import addressing


def load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("invalid YAML")
    return schema.validate(data, path)


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
    payload = json.loads(http_get(f"http://{addressing.authority(host, port)}/v1/users", timeout))
    if not isinstance(payload, dict) or payload.get("ok") is not True or not isinstance(payload.get("data"), list):
        raise RuntimeError("unexpected /v1/users response shape")
    if len(payload["data"]) != len(config["users"]):
        raise RuntimeError(f"API returned {len(payload['data'])} users, expected {len(config['users'])}")
    return payload


def api_ready(config: dict) -> dict:
    host, port = endpoint(config["server"]["api_listen"])
    timeout = config["probes"]["timeout_seconds"]
    url = f"http://{addressing.authority(host, port)}/v1/health/ready"
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
    url = f"http://{addressing.authority(host, port)}/v1/runtime/upstream-quality"
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
    connect_hosts = [addressing.loopback(ip) for ip in addressing.listen_ips(config)]
    metrics_host, metrics_port = endpoint(config["server"]["metrics_listen"])
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    upstream_payload: dict | None = None
    while True:
        try:
            if config.get("schema_version") == 2:
                pid = subprocess.check_output(["systemctl", "show", service, "-p", "MainPID", "--value"], text=True).strip()
                verify_listener_owners(config, pid)
            for connect_host in connect_hosts:
                with socket.create_connection((connect_host, config["server"]["port"]), timeout=1):
                    pass
            payload = api_users(config)
            api_ready(config)
            upstream_payload = api_telegram_upstream_ready(config)
            metrics = http_get(f"http://{addressing.authority(metrics_host, metrics_port)}/metrics", 2).decode("utf-8", "replace")
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
    for connect_host in connect_hosts:
        print(f"PASS proxy listener: {addressing.authority(connect_host, config['server']['port'])}")
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
    print(f"PASS external TLS camouflage smoke: {host}:{port} SNI={domain} {version} {cipher} cert={cert_len}B {elapsed_ms}ms")
    print("INFO authenticated Telegram message delivery is not tested")


def show_links(config: dict, endpoint_id=None, output_format="json") -> None:
    payload = api_users(config)
    print("WARNING: the following output contains proxy credentials", file=sys.stderr)
    if config.get("schema_version") == 2 or endpoint_id is not None or output_format == "text":
        from links import catalogue
        payload = catalogue(config, payload, endpoint_id)
    if output_format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for user in payload["users"]:
            for item in user["links"]:
                print(f"{user['username']} {item['endpoint_id']} {item['mode']}\n{item['tg_url']}\n{item['https_url']}")


def verify_listener_owners(config, pid):
    if not pid.isdigit() or pid == "0":
        raise ValueError("service has no MainPID")
    port = config["server"]["port"]
    for bind in addressing.listen_ips(config):
        family = addressing.family(bind)
        output = subprocess.check_output(["ss", "-H", "-4" if family == "ipv4" else "-6", "-lntp", f"sport = :{port}"], text=True)
        matched = False
        for line in output.splitlines():
            fields = line.split()
            if len(fields) < 4:
                continue
            host = fields[3].rsplit(":", 1)[0].strip("[]")
            if host == "*":
                host = "0.0.0.0" if family == "ipv4" else "::"
            if host == bind:
                if set(re.findall(r"pid=(\d+)", line)) != {pid}:
                    raise ValueError("listener is not exclusively owned by service MainPID")
                matched = True
        if not matched:
            raise ValueError("configured listener socket is missing")


class ConnectFailure(Exception):
    def __init__(self, cause):
        self.cause = cause


def tls_smoke(ip, port, domain, timeout):
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        raw = socket.create_connection((ip, port), timeout=timeout)
    except OSError as exc:
        raise ConnectFailure(exc) from exc
    with raw:
        with context.wrap_socket(raw, server_hostname=domain) as tls:
            if not tls.version() or not tls.getpeercert(binary_form=True):
                raise ValueError("TLS smoke returned no certificate")


def check_e2e_matrix(config, endpoint_id=None, selected_family="all", timeout=None):
    proxy = config["proxy"]
    if not proxy["modes"]["tls"] or not proxy.get("tls_domain"):
        raise ValueError("external TLS smoke requires TLS mode and proxy.tls_domain")
    items = addressing.endpoints(config)
    if endpoint_id is not None:
        items = [item for item in items if item["id"] == endpoint_id]
        if not items:
            raise ValueError("unknown endpoint id")
    timeout = timeout or config["probes"]["timeout_seconds"]
    results = []
    deadline = time.monotonic() + min(300, timeout * 64)
    def record(item, family, ip, check, call):
        started = time.monotonic()
        row = dict(endpoint_id=item["id"], address_family=family, connect_address=ip,
                   port=item["port"], check=check, status="PASS", error_kind=None)
        print(f"RUN {item['id']} {family} {check} {ip or item['host']}", flush=True)
        try:
            if started >= deadline:
                raise TimeoutError("total check budget exhausted")
            value = call()
        except Exception as exc:
            import errno
            connect_failure = isinstance(exc, ConnectFailure)
            if connect_failure:
                exc = exc.cause
            row["stage"] = "resolve" if check == "resolve" else "connect" if connect_failure else "handshake"
            row["status"] = "FAIL"
            row["error_kind"] = ("DNS_MISSING" if isinstance(exc, socket.gaierror) else
                                 "NO_ROUTE" if isinstance(exc, OSError) and exc.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH, 10051, 10065) else
                                 "TIMEOUT" if isinstance(exc, TimeoutError) else
                                 "DNS_FAILED" if check == "resolve" else "CONNECT_FAILED" if connect_failure else "TLS_SMOKE_FAILED")
            # Do not serialize arbitrary exception strings which may contain sensitive context.
            value = None
        row["duration_ms"] = round((time.monotonic() - started) * 1000)
        results.append(row)
        print(f"{row['status']} {item['id']} {family} {check} {row['error_kind'] or ''}", flush=True)
        return value
    for item in items:
        for family in item["families"]:
            if selected_family not in ("all", family):
                results.append(dict(endpoint_id=item["id"], address_family=family, connect_address=None,
                                    port=item["port"], check="family", status="SKIP", error_kind=None, duration_ms=0))
                continue
            ips = record(item, family, None, "resolve", lambda: addressing.resolve_bounded(
                item["host"], item["port"], family, max(.01, min(timeout, deadline-time.monotonic()))))
            for ip in ips or []:
                record(item, family, ip, "tls_camouflage_smoke", lambda: tls_smoke(
                    ip, item["port"], proxy["tls_domain"], max(.01, min(timeout, (deadline-time.monotonic()) / 2))))
    counts = {key: sum(row["status"] == status for row in results)
              for key, status in (("passed", "PASS"), ("failed", "FAIL"), ("skipped", "SKIP"))}
    report = dict(schema_version=2, scope="e2e", filters=dict(endpoint=endpoint_id, family=selected_family),
                  results=results, summary=counts, authenticated_telegram_tested=False)
    print(f"SUMMARY passed={counts['passed']} failed={counts['failed']} skipped={counts['skipped']}", flush=True)
    print("INFO TLS camouflage only; authenticated MTProto and message delivery are not tested", flush=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=("vm", "e2e", "links"), required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--instance")
    parser.add_argument("--endpoint")
    parser.add_argument("--family", choices=("all", "ipv4", "ipv6"), default="all")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    args = parser.parse_args()
    try:
        if args.config is not None:
            config_path = args.config
        elif args.instance:
            from instance import paths, read_manifest, guard_path
            if read_manifest(args.instance) is None:
                raise ValueError("instance ownership manifest missing")
            config_path = Path(paths(args.instance)["state"]) / "config.yaml"
            guard_path(config_path, private=True)
        else:
            raise ValueError("--config or --instance is required")
        config = load(config_path)
        if args.instance and config.get("instance", {}).get("id") != args.instance:
            raise ValueError("--instance conflicts with config instance.id")
        if args.timeout is not None and not 0 < args.timeout <= 60:
            raise ValueError("timeout must be in (0,60]")
        if args.scope == "links":
            if args.json:
                raise ValueError("credential links cannot be written as healthcheck reports")
            show_links(config, args.endpoint, args.format)
        elif args.scope == "e2e" and (config.get("schema_version") == 2 or args.endpoint or args.json or args.family != "all" or args.timeout):
            report = check_e2e_matrix(config, args.endpoint, args.family, args.timeout)
            if args.json:
                with args.json.open("x", encoding="utf-8") as output:
                    json.dump(report, output, indent=2)
            return int(report["summary"]["failed"] > 0 or report["summary"]["passed"] == 0)
        else:
            if args.endpoint or args.json or args.family != "all" or args.timeout:
                raise ValueError("matrix options require e2e scope")
            {"vm": check_vm, "e2e": check_e2e}[args.scope](config)
        return 0
    except Exception as exc:
        print(f"FAIL {args.scope}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
