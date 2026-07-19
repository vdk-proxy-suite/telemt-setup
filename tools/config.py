#!/usr/bin/env python3
"""Validate the standalone YAML and render a strict Telemt TOML config."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tomllib

import yaml


TOP_KEYS = {"install", "server", "links", "proxy", "users", "probes"}
SCHEMA = {
    "install": {
        "version", "architecture", "libc", "sha256", "service_name", "user",
        "group", "binary_path", "config_path", "work_dir", "backup_root",
        "state_root", "manage_ufw",
    },
    "server": {"listen_ip", "port", "max_connections", "api_listen", "metrics_listen"},
    "links": {"public_host", "public_port"},
    "proxy": {"use_middle_proxy", "log_level", "modes", "tls_domain", "mask", "tls_emulation"},
    "probes": {"timeout_seconds"},
}
MODE_KEYS = {"classic", "secure", "tls"}
USER_KEYS = {"secret", "ad_tag", "max_unique_ips"}
NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,30}$")
SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}$")
HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")
USER_KEY_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
HOST_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


class ConfigError(ValueError):
    pass


def load_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("top-level YAML value must be a mapping")
    return data


def require_mapping(data: dict, key: str) -> dict:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a mapping")
    return value


def reject_unknown(mapping: dict, allowed: set[str], where: str) -> None:
    unknown = set(mapping) - allowed
    if unknown:
        raise ConfigError(f"unknown keys in {where}: {', '.join(sorted(unknown))}")


def require_bool(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where} must be true or false")
    return value


def require_int(value: object, where: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigError(f"{where} must be an integer in {minimum}..{maximum}")
    return value


def split_endpoint(value: object, where: str) -> tuple[str, int]:
    if not isinstance(value, str) or ":" not in value:
        raise ConfigError(f"{where} must have HOST:PORT form")
    host, raw_port = value.rsplit(":", 1)
    host = host.strip("[]")
    try:
        ipaddress.ip_address(host)
        port = int(raw_port)
    except ValueError as exc:
        raise ConfigError(f"{where} must contain a literal IP and numeric port") from exc
    require_int(port, where + " port", 1, 65535)
    return host, port


def require_abs_path(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or value == "/" or "\x00" in value:
        raise ConfigError(f"{where} must be a safe absolute path other than /")
    if any(part in {".", ".."} for part in Path(value).parts):
        raise ConfigError(f"{where} may not contain . or ..")
    return value


def validate(data: dict, yaml_path: Path | None = None) -> dict:
    reject_unknown(data, TOP_KEYS, "root")
    for section in TOP_KEYS:
        if section not in data:
            raise ConfigError(f"missing section: {section}")

    install = require_mapping(data, "install")
    server = require_mapping(data, "server")
    links = require_mapping(data, "links")
    proxy = require_mapping(data, "proxy")
    users = require_mapping(data, "users")
    probes = require_mapping(data, "probes")
    for name, mapping in (("install", install), ("server", server), ("links", links), ("proxy", proxy), ("probes", probes)):
        reject_unknown(mapping, SCHEMA[name], name)

    for key in SCHEMA["install"]:
        if key not in install:
            raise ConfigError(f"missing install.{key}")
    if not isinstance(install["version"], str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", install["version"]):
        raise ConfigError("install.version must be X.Y.Z")
    if install["architecture"] not in {"x86_64", "aarch64"}:
        raise ConfigError("install.architecture must be x86_64 or aarch64")
    if install["libc"] not in {"gnu", "musl"}:
        raise ConfigError("install.libc must be gnu or musl")
    if not isinstance(install["sha256"], str) or not re.fullmatch(r"[0-9a-fA-F]{64}", install["sha256"]):
        raise ConfigError("install.sha256 must be 64 hex characters")
    if not SERVICE_RE.fullmatch(str(install["service_name"])):
        raise ConfigError("install.service_name is invalid")
    for key in ("user", "group"):
        if not NAME_RE.fullmatch(str(install[key])):
            raise ConfigError(f"install.{key} is invalid")
    for key in ("binary_path", "config_path", "work_dir", "backup_root", "state_root"):
        require_abs_path(install[key], "install." + key)
    if not (install["binary_path"].startswith("/bin/") or install["binary_path"].startswith("/usr/local/")):
        raise ConfigError("install.binary_path must be under /bin or /usr/local")
    if not install["config_path"].startswith("/etc/"):
        raise ConfigError("install.config_path must be under /etc")
    if not install["work_dir"].startswith("/opt/"):
        raise ConfigError("install.work_dir must be under /opt")
    if not install["backup_root"].startswith("/var/backups/") and install["backup_root"] != "/var/backups/telemt-setup":
        raise ConfigError("install.backup_root must be under /var/backups")
    if not install["state_root"].startswith("/var/lib/"):
        raise ConfigError("install.state_root must be under /var/lib")
    require_bool(install["manage_ufw"], "install.manage_ufw")

    try:
        ipaddress.ip_address(server["listen_ip"])
    except (ValueError, TypeError) as exc:
        raise ConfigError("server.listen_ip must be a literal IP") from exc
    require_int(server["port"], "server.port", 1, 65535)
    require_int(server["max_connections"], "server.max_connections", 1, 1_000_000)
    _, api_port = split_endpoint(server["api_listen"], "server.api_listen")
    _, metrics_port = split_endpoint(server["metrics_listen"], "server.metrics_listen")
    if len({server["port"], api_port, metrics_port}) != 3:
        raise ConfigError("proxy, API and metrics ports must be distinct")

    public_host = links.get("public_host")
    if not isinstance(public_host, str) or public_host in {"", "CHANGE_ME"}:
        raise ConfigError("links.public_host must be replaced with a public IP or DNS name")
    try:
        ipaddress.ip_address(public_host)
    except ValueError:
        if not HOST_RE.fullmatch(public_host):
            raise ConfigError("links.public_host is not a valid IP or DNS name")
    require_int(links.get("public_port"), "links.public_port", 1, 65535)

    for key in ("use_middle_proxy", "mask", "tls_emulation"):
        require_bool(proxy.get(key), "proxy." + key)
    if proxy.get("log_level") not in {"debug", "verbose", "normal", "silent"}:
        raise ConfigError("proxy.log_level is invalid")
    modes = proxy.get("modes")
    if not isinstance(modes, dict):
        raise ConfigError("proxy.modes must be a mapping")
    reject_unknown(modes, MODE_KEYS, "proxy.modes")
    if set(modes) != MODE_KEYS or not all(isinstance(v, bool) for v in modes.values()):
        raise ConfigError("proxy.modes must define boolean classic, secure and tls")
    if not any(modes.values()):
        raise ConfigError("at least one proxy mode must be enabled")
    if modes["tls"] and (not isinstance(proxy.get("tls_domain"), str) or not HOST_RE.fullmatch(proxy["tls_domain"])):
        raise ConfigError("proxy.tls_domain must be a DNS name when TLS mode is enabled")

    if not users:
        raise ConfigError("users must contain at least one entry")
    has_explicit_secret = False
    for username, user in users.items():
        if not isinstance(username, str) or not USER_KEY_RE.fullmatch(username):
            raise ConfigError(f"invalid user key: {username!r}")
        if not isinstance(user, dict):
            raise ConfigError(f"users.{username} must be a mapping")
        reject_unknown(user, USER_KEYS, f"users.{username}")
        if set(user) != USER_KEYS:
            raise ConfigError(f"users.{username} must define secret, ad_tag and max_unique_ips")
        secret = user["secret"]
        if secret != "GENERATE" and (not isinstance(secret, str) or not HEX32_RE.fullmatch(secret)):
            raise ConfigError(f"users.{username}.secret must be GENERATE or 32 hex characters")
        has_explicit_secret |= secret != "GENERATE"
        tag = user["ad_tag"]
        if tag is not None and (not isinstance(tag, str) or not HEX32_RE.fullmatch(tag)):
            raise ConfigError(f"users.{username}.ad_tag must be null or 32 hex characters")
        limit = user["max_unique_ips"]
        if limit is not None:
            require_int(limit, f"users.{username}.max_unique_ips", 1, 1_000_000)

    require_int(probes.get("timeout_seconds"), "probes.timeout_seconds", 1, 120)
    if has_explicit_secret and yaml_path is not None and os.name == "posix":
        mode = stat.S_IMODE(yaml_path.stat().st_mode)
        if mode & 0o077:
            raise ConfigError(f"{yaml_path} contains explicit secrets and must be chmod 600")
    return data


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def existing_secrets(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            parsed = tomllib.load(handle)
        users = parsed.get("access", {}).get("users", {})
        return {str(k): str(v) for k, v in users.items() if isinstance(v, str) and HEX32_RE.fullmatch(v)}
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def render(data: dict, old_secrets: dict[str, str]) -> str:
    install, server, links, proxy, users = (data[k] for k in ("install", "server", "links", "proxy", "users"))
    resolved: dict[str, str] = {}
    for username, user in users.items():
        configured = user["secret"]
        resolved[username] = old_secrets.get(username, secrets.token_hex(16)) if configured == "GENERATE" else configured.lower()

    loopback_v4 = "127.0.0.1/32"
    loopback_v6 = "::1/128"
    lines = [
        "# Generated by telemt-setup. Edit the YAML source, not this file.",
        "[general]",
        f"use_middle_proxy = {str(proxy['use_middle_proxy']).lower()}",
        f"log_level = {toml_string(proxy['log_level'])}",
        "config_strict = true",
        "",
        "[general.modes]",
        f"classic = {str(proxy['modes']['classic']).lower()}",
        f"secure = {str(proxy['modes']['secure']).lower()}",
        f"tls = {str(proxy['modes']['tls']).lower()}",
        "",
        "[general.links]",
        "show = []",
        f"public_host = {toml_string(links['public_host'])}",
        f"public_port = {links['public_port']}",
        "",
        "[server]",
        f"port = {server['port']}",
        f"max_connections = {server['max_connections']}",
        f"metrics_listen = {toml_string(server['metrics_listen'])}",
        f"metrics_whitelist = [{toml_string(loopback_v4)}, {toml_string(loopback_v6)}]",
        "",
        "[server.api]",
        "enabled = true",
        f"listen = {toml_string(server['api_listen'])}",
        f"whitelist = [{toml_string(loopback_v4)}, {toml_string(loopback_v6)}]",
        "minimal_runtime_enabled = false",
        "minimal_runtime_cache_ttl_ms = 1000",
        "",
        "[[server.listeners]]",
        f"ip = {toml_string(server['listen_ip'])}",
        "",
        "[censorship]",
        f"tls_domain = {toml_string(proxy['tls_domain'])}",
        f"mask = {str(proxy['mask']).lower()}",
        f"tls_emulation = {str(proxy['tls_emulation']).lower()}",
        'tls_front_dir = "tlsfront"',
        "",
        "[access.users]",
    ]
    lines.extend(f"{toml_string(name)} = {toml_string(secret)}" for name, secret in resolved.items())
    tags = {name: user["ad_tag"].lower() for name, user in users.items() if user["ad_tag"] is not None}
    limits = {name: user["max_unique_ips"] for name, user in users.items() if user["max_unique_ips"] is not None}
    if tags:
        lines.extend(["", "[access.user_ad_tags]"])
        lines.extend(f"{toml_string(name)} = {toml_string(tag)}" for name, tag in tags.items())
    if limits:
        lines.extend(["", "[access.user_max_unique_ips]"])
        lines.extend(f"{toml_string(name)} = {limit}" for name, limit in limits.items())
    lines.append("")
    return "\n".join(lines)


def lookup(data: dict, key: str) -> object:
    if key == "server.api_listen_port":
        return split_endpoint(data["server"]["api_listen"], key)[1]
    if key == "server.metrics_listen_port":
        return split_endpoint(data["server"]["metrics_listen"], key)[1]
    value: object = data
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            raise ConfigError(f"unknown config key: {key}")
        value = value[part]
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "get", "render"):
        item = sub.add_parser(command)
        item.add_argument("--config", type=Path, required=True)
        if command == "get":
            item.add_argument("--key", required=True)
        if command == "render":
            item.add_argument("--output", type=Path, required=True)
            item.add_argument("--existing-toml", type=Path)
    args = parser.parse_args()
    try:
        data = validate(load_yaml(args.config), args.config)
        if args.command == "validate":
            print("configuration is valid")
        elif args.command == "get":
            value = lookup(data, args.key)
            print(str(value).lower() if isinstance(value, bool) else value)
        else:
            output = render(data, existing_secrets(args.existing_toml))
            args.output.write_text(output, encoding="utf-8")
            os.chmod(args.output, 0o600)
            with args.output.open("rb") as handle:
                tomllib.load(handle)
            print(f"rendered valid TOML sha256={hashlib.sha256(output.encode()).hexdigest()}")
        return 0
    except (ConfigError, OSError, tomllib.TOMLDecodeError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
