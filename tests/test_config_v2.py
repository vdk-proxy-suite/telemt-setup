import copy
from pathlib import Path
import sys
import tomllib
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import config


def fixture():
    data = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    data["schema_version"] = 2
    data["server"].pop("listen_ip")
    data["server"]["listen_ips"] = ["0.0.0.0", "::"]
    data["links"] = {"primary": "dns", "endpoints": [
        dict(id="dns", host="proxy.example.org", port=443, families=["ipv4", "ipv6"]),
        dict(id="v4", host="192.0.2.10", port=443, families=["ipv4"]),
        dict(id="v6", host="2001:db8::10", port=443, families=["ipv6"]),
    ]}
    return data


class ConfigV2Tests(unittest.TestCase):
    def test_binds_primary_and_egress_are_independent(self):
        data = config.validate(fixture())
        result = tomllib.loads(config.render(data, {}))
        self.assertEqual([x["ip"] for x in result["server"]["listeners"]], ["0.0.0.0", "::"])
        self.assertEqual(result["general"]["links"]["public_host"], "proxy.example.org")
        self.assertEqual(result["network"], dict(ipv4=True, ipv6=True, prefer=4))
        self.assertEqual(len(result["upstreams"]), 2)
        self.assertEqual(result["upstreams"][0]["type"], "direct")
        self.assertFalse(result["upstreams"][0]["ipv6"])
        self.assertEqual(result["upstreams"][1]["scopes"], config.TLS_FETCH_DIRECT_SCOPE)

    def test_socks_remains_exclusive_telegram_route(self):
        data = fixture()
        data["upstreams"] = [dict(type="socks5", address="127.0.0.1:11081")]
        rendered = tomllib.loads(config.render(config.validate(data), {}))
        unscoped = [u for u in rendered["upstreams"] if not u.get("scopes")]
        self.assertEqual([u["type"] for u in unscoped], ["socks5"])
        self.assertFalse(unscoped[0]["ipv6"])
        self.assertTrue(rendered["censorship"]["tls_fetch"]["strict_route"])

    def test_alias_addition_preserves_user_secret(self):
        data = config.validate(fixture())
        old = {name: "12" * 16 for name in data["users"]}
        result = tomllib.loads(config.render(data, old))
        self.assertEqual(result["access"]["users"], old)

    def test_rejects_mixed_schema_and_bad_addresses(self):
        cases = []
        data = fixture(); data["server"]["listen_ip"] = "0.0.0.0"; cases.append(data)
        data = fixture(); data["links"]["public_host"] = "example.org"; cases.append(data)
        data = fixture(); data["server"]["listen_ips"] = ["::", "::1"]; cases.append(data)
        data = fixture(); data["links"]["primary"] = "missing"; cases.append(data)
        data = fixture(); data["links"]["endpoints"][2]["families"] = ["ipv4"]; cases.append(data)
        data = fixture(); data["schema_version"] = True; cases.append(data)
        data = fixture(); data["outbound"] = dict(families=["ipv4"], prefer="ipv6"); cases.append(data)
        for data in cases:
            with self.subTest(data_index=cases.index(data)), self.assertRaises(config.ConfigError):
                config.validate(copy.deepcopy(data))
