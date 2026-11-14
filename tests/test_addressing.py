import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import addressing as addr


class AddressTests(unittest.TestCase):
    def test_dns_normalization_and_authority(self):
        self.assertEqual(addr.host("ПРИМЕР.рф"), "xn--e1afmkfd.xn--p1ai")
        self.assertEqual(addr.authority("::1", 9091), "[::1]:9091")
        self.assertEqual(addr.loopback("::"), "::1")

    def test_reject_ambiguous_or_unsafe_hosts(self):
        for value in ("::", "0.0.0.0", "https://host", "user@host", "host:443", "fe80::1%eth0", "::ffff:192.0.2.1", "ff02::1", "host/path"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                addr.host(value)

    def test_v6only_overlap(self):
        self.assertFalse(addr.overlaps("0.0.0.0", "::"))
        self.assertTrue(addr.overlaps("::", "2001:db8::1"))
        self.assertTrue(addr.overlaps("0.0.0.0", "192.0.2.1"))
        self.assertFalse(addr.overlaps("192.0.2.1", "192.0.2.2"))

    def test_resolution_is_family_specific_and_deduplicated(self):
        rows = [(addr.socket.AF_INET6, 1, 6, "", ("2001:db8::1", 443, 0, 0))] * 2
        with patch.object(addr.socket, "getaddrinfo", return_value=rows) as resolve:
            self.assertEqual(addr.resolve("example.org", 443, "ipv6"), ["2001:db8::1"])
            self.assertEqual(resolve.call_args.args[2], addr.socket.AF_INET6)
        with patch.object(addr.socket, "getaddrinfo", return_value=[]), self.assertRaises(ValueError):
            addr.resolve("example.org", 443, "ipv6")
