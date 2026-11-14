import contextlib
import io
import socket
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import healthcheck
from test_config_v2 import fixture


class MatrixTests(unittest.TestCase):
    def run_matrix(self, data, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return healthcheck.check_e2e_matrix(data, **kwargs)

    def test_checks_every_address_without_ipv4_hiding_ipv6_failure(self):
        def resolve(host, port, family, timeout):
            return ["192.0.2.1", "192.0.2.2"] if family == "ipv4" else ["2001:db8::1"]
        def probe(ip, *args):
            if ":" in ip: raise TimeoutError()
        with patch.object(healthcheck.addressing, "resolve_bounded", side_effect=resolve), patch.object(healthcheck, "tls_smoke", side_effect=probe) as tls:
            result = self.run_matrix(fixture(), endpoint_id="dns")
        self.assertEqual(tls.call_count, 3)
        self.assertEqual(result["summary"]["failed"], 1)
        self.assertEqual(result["results"][-1]["error_kind"], "TIMEOUT")
        self.assertFalse(result["authenticated_telegram_tested"])

    def test_missing_aaaa_is_failure_and_filter_is_explicit(self):
        def resolve(host, port, family, timeout):
            if family == "ipv6": raise socket.gaierror()
            return ["192.0.2.1"]
        with patch.object(healthcheck.addressing, "resolve_bounded", side_effect=resolve), patch.object(healthcheck, "tls_smoke"):
            result = self.run_matrix(fixture(), endpoint_id="dns")
            filtered = self.run_matrix(fixture(), endpoint_id="dns", selected_family="ipv4")
        self.assertEqual(result["summary"]["failed"], 1)
        self.assertEqual(result["results"][-1]["error_kind"], "DNS_MISSING")
        self.assertEqual(filtered["summary"], dict(passed=2, failed=0, skipped=1))

    def test_ipv6_api_url(self):
        data = fixture(); data["server"]["api_listen"] = "[::1]:9091"
        with patch.object(healthcheck, "http_get", return_value=b'{"ok":true,"data":[{}]}') as get:
            healthcheck.api_users(data)
        self.assertEqual(get.call_args.args[0], "http://[::1]:9091/v1/users")

    def test_exception_details_do_not_leak_into_report(self):
        with patch.object(healthcheck.addressing, "resolve_bounded", side_effect=ValueError("secret-sensitive-example")):
            report = self.run_matrix(fixture(), endpoint_id="dns")
        self.assertNotIn("secret-sensitive-example", str(report))
