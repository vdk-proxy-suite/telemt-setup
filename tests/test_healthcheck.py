from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import healthcheck  # noqa: E402


class HealthcheckTests(unittest.TestCase):
    config = {
        "server": {"api_listen": "127.0.0.1:9091"},
        "probes": {"timeout_seconds": 10},
    }

    def test_api_ready_accepts_healthy_upstream(self) -> None:
        response = {"ok": True, "data": {"ready": True, "status": "ready", "reason": None}}
        with patch.object(healthcheck, "http_get", return_value=json.dumps(response).encode()) as get:
            payload = healthcheck.api_ready(self.config)

        self.assertEqual(payload, response)
        get.assert_called_once_with("http://127.0.0.1:9091/v1/health/ready", 10)

    def test_api_ready_rejects_unhealthy_upstream(self) -> None:
        response = {
            "ok": True,
            "data": {"ready": False, "status": "not_ready", "reason": "no_healthy_upstreams"},
        }
        url = "http://127.0.0.1:9091/v1/health/ready"
        error = HTTPError(url, 503, "Service Unavailable", {}, BytesIO(json.dumps(response).encode()))
        with patch.object(healthcheck, "http_get", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "no_healthy_upstreams"):
                healthcheck.api_ready(self.config)

    def test_api_ready_rejects_unexpected_shape(self) -> None:
        for body in (b'[]', b'{"ok": true, "data": []}'):
            with self.subTest(body=body):
                with patch.object(healthcheck, "http_get", return_value=body):
                    with self.assertRaisesRegex(RuntimeError, "unexpected"):
                        healthcheck.api_ready(self.config)

    def test_telegram_upstream_ready_ignores_scoped_direct_route(self) -> None:
        config = {
            **self.config,
            "upstreams": [{"type": "socks5", "address": "proxy.example.invalid:1080"}],
        }
        response = {
            "ok": True,
            "data": {
                "enabled": True,
                "upstreams": [
                    {
                        "route_kind": "socks5",
                        "scopes": "",
                        "healthy": True,
                        "dc": [{"latency_ema_ms": 42.0}],
                    },
                    {
                        "route_kind": "direct",
                        "scopes": "telemt_setup_tls_front_direct",
                        "healthy": True,
                    },
                ],
            },
        }
        with patch.object(healthcheck, "http_get", return_value=json.dumps(response).encode()) as get:
            payload = healthcheck.api_telegram_upstream_ready(config)

        self.assertEqual(payload, response)
        get.assert_called_once_with("http://127.0.0.1:9091/v1/runtime/upstream-quality", 10)

    def test_telegram_upstream_ready_rejects_only_healthy_scoped_direct(self) -> None:
        config = {
            **self.config,
            "upstreams": [{"type": "socks5", "address": "proxy.example.invalid:1080"}],
        }
        response = {
            "ok": True,
            "data": {
                "enabled": True,
                "upstreams": [
                    {"route_kind": "socks5", "scopes": "", "healthy": False},
                    {
                        "route_kind": "direct",
                        "scopes": "telemt_setup_tls_front_direct",
                        "healthy": True,
                    },
                ],
            },
        }
        with patch.object(healthcheck, "http_get", return_value=json.dumps(response).encode()):
            with self.assertRaisesRegex(RuntimeError, "no healthy unscoped SOCKS5"):
                healthcheck.api_telegram_upstream_ready(config)

    def test_telegram_upstream_ready_rejects_unobserved_initial_health(self) -> None:
        config = {
            **self.config,
            "upstreams": [{"type": "socks5", "address": "proxy.example.invalid:1080"}],
        }
        response = {
            "ok": True,
            "data": {
                "enabled": True,
                "upstreams": [
                    {
                        "route_kind": "socks5",
                        "scopes": "",
                        "healthy": True,
                        "dc": [{"latency_ema_ms": None}],
                    }
                ],
            },
        }
        with patch.object(healthcheck, "http_get", return_value=json.dumps(response).encode()):
            with self.assertRaisesRegex(RuntimeError, "observed Telegram DC connectivity"):
                healthcheck.api_telegram_upstream_ready(config)

    def test_telegram_upstream_ready_is_skipped_without_upstreams(self) -> None:
        with patch.object(healthcheck, "http_get") as get:
            payload = healthcheck.api_telegram_upstream_ready(self.config)

        self.assertIsNone(payload)
        get.assert_not_called()

    def test_e2e_without_tls_domain_fails_with_clear_error(self) -> None:
        config = {
            **self.config,
            "links": {"public_host": "proxy.example.invalid", "public_port": 443},
            "proxy": {"modes": {"tls": False}},
        }

        with self.assertRaisesRegex(RuntimeError, "requires TLS mode and proxy.tls_domain"):
            healthcheck.check_e2e(config)


if __name__ == "__main__":
    unittest.main()
