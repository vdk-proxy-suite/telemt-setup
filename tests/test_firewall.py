import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import firewall
from test_config_v2 import fixture


class FirewallTests(unittest.TestCase):
    def test_exact_family_and_wildcard_rules(self):
        rules = firewall.desired(fixture(), "example")
        self.assertEqual([firewall.parse(r)["families"] for r in rules], [{4}, {6}])
        self.assertFalse(firewall.same(*rules))
        self.assertEqual(firewall.validate({"ufw_rules": rules}, "example"), rules)

    def test_shorthand_covers_both_but_cannot_be_claimed_as_single_family(self):
        broad = ["ufw", "allow", "443/tcp", "comment", "telemt-setup:example"]
        for rule in firewall.desired(fixture(), "example"):
            self.assertTrue(firewall.covers(broad, rule))
            self.assertFalse(firewall.same(broad, rule))

    def test_loopback_never_exposed_and_exact_destination_preserved(self):
        data = fixture(); data["server"]["listen_ips"] = ["127.0.0.1", "::1"]
        self.assertEqual(firewall.desired(data, "example"), [])
        data["server"]["listen_ips"] = ["2001:db8::2"]
        rule = firewall.desired(data, "example")[0]
        self.assertEqual(str(firewall.parse(rule)["dst"]), "2001:db8::2/128")

    def test_foreign_marker_and_broken_ownership_rejected(self):
        rules = firewall.desired(fixture(), "example")
        with self.assertRaises(ValueError): firewall.validate({"ufw_rules": rules}, "neighbor")
        for invalid in ("bad", [["ufw", "delete", "443/tcp"]], rules + rules):
            with self.assertRaises(ValueError): firewall.validate({"ufw_rules": invalid}, "example")

    def test_canonical_ufw_reordering_and_host_prefix(self):
        data = fixture(); data["server"]["listen_ips"] = ["192.0.2.10"]
        rule = firewall.desired(data, "example")[0]
        actual = ["ufw", "allow", "proto", "tcp", "from", "0.0.0.0/0", "to", "192.0.2.10/32", "port", "443", "comment", "telemt-setup:example"]
        self.assertTrue(firewall.same(rule, actual))
