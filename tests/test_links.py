import copy
import sys
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import links
from test_config_v2 import fixture


class LinksTests(unittest.TestCase):
    def payload(self, data):
        name = next(iter(data["users"]))
        token = "ee" + "ab" * 16 + data["proxy"]["tls_domain"].encode().hex()
        return {"ok": True, "data": [{"username": name, "links": {"tls": [f"tg://proxy?server=192.0.2.1&port=443&secret={token}"]}}]}

    def test_all_aliases_keep_active_secret_and_decode_ipv6(self):
        data = fixture()
        result = links.catalogue(data, self.payload(data))
        variants = result["users"][0]["links"]
        self.assertEqual(len(variants), 3)
        queries = [parse_qs(urlsplit(item["tg_url"]).query) for item in variants]
        self.assertEqual(len({q["secret"][0] for q in queries}), 1)
        self.assertEqual(queries[-1]["server"], ["2001:db8::10"])
        self.assertEqual(len(links.catalogue(data, self.payload(data), "v6")["users"][0]["links"]), 1)

    def test_no_generation_on_api_or_config_mismatch(self):
        data = fixture()
        payload = self.payload(data)
        bad = copy.deepcopy(payload); bad["data"][0]["username"] = "unknown"
        with self.assertRaises(ValueError): links.catalogue(data, bad)
        changed = copy.deepcopy(data); changed["proxy"]["tls_domain"] = "different.example.org"
        with self.assertRaises(ValueError): links.catalogue(changed, payload)
        changed = copy.deepcopy(data); next(iter(changed["users"].values()))["secret"] = "cd" * 16
        with self.assertRaises(ValueError): links.catalogue(changed, payload)
