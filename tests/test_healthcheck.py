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


if __name__ == "__main__":
    unittest.main()
