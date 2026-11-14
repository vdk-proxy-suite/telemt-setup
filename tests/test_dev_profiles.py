from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import dev_profiles
import config


class DevProfilesTests(unittest.TestCase):
    def test_matrix_keeps_production_ports_and_secrets_out(self):
        data = dev_profiles.profiles("192.0.2.10", "2001:db8::10", "proxy.example.org")
        self.assertEqual(len(data), 8)
        self.assertEqual(len({item["instance"]["id"] for item in data}), 8)
        self.assertEqual([item["server"]["port"] for item in data], list(range(21110, 21118)))
        self.assertEqual([item["upstreams"][0]["address"] for item in data[4:]],
                         [f"127.0.0.1:{port}" for port in range(11080, 11084)])
        for item in data:
            config.validate(item)
            self.assertTrue(all(user["secret"] == "GENERATE" for user in item["users"].values()))
        self.assertEqual(data[0]["links"]["primary"], "ipv4")
        self.assertEqual(data[1]["links"]["primary"], "ipv6")
        self.assertTrue(data[3]["proxy"]["use_middle_proxy"])
        self.assertTrue(all(not item["proxy"]["use_middle_proxy"] for item in data[4:]))

    def test_invalid_port_ranges_fail_before_writing(self):
        with self.assertRaises(ValueError):
            dev_profiles.profiles("192.0.2.10", "2001:db8::10", "proxy.example.org", api_base=21110)
