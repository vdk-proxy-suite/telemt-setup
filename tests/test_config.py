from __future__ import annotations

import copy
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import config as config_tool  # noqa: E402


class ConfigTests(unittest.TestCase):
    def base_config(self) -> dict:
        data = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
        data["links"]["public_host"] = "proxy.example.invalid"
        return data

    def parse_rendered(self, data: dict) -> dict:
        validated = config_tool.validate(copy.deepcopy(data))
        return tomllib.loads(config_tool.render(validated, {}))

    def test_config_without_upstreams_remains_backward_compatible(self) -> None:
        for representation in ("absent", "empty"):
            with self.subTest(representation=representation):
                data = self.base_config()
                if representation == "absent":
                    data.pop("upstreams")

                rendered = self.parse_rendered(data)

                self.assertNotIn("upstreams", rendered)
                self.assertNotIn("tls_fetch_scope", rendered["censorship"])
                self.assertNotIn("tls_fetch", rendered["censorship"])

    def test_authenticated_socks5_is_rendered_with_middle_proxy(self) -> None:
        data = self.base_config()
        data["upstreams"] = [
            {
                "type": "socks5",
                "address": "proxy.example.invalid:1080",
                "username": "proxy-user",
                "password": 'p\\ass"word',
                "weight": 1,
                "enabled": True,
            }
        ]

        rendered = self.parse_rendered(data)

        self.assertTrue(rendered["general"]["use_middle_proxy"])
        self.assertEqual(
            rendered["upstreams"],
            [
                {
                    "type": "socks5",
                    "address": "proxy.example.invalid:1080",
                    "username": "proxy-user",
                    "password": 'p\\ass"word',
                    "weight": 1,
                    "enabled": True,
                },
                {
                    "type": "direct",
                    "scopes": config_tool.TLS_FETCH_DIRECT_SCOPE,
                    "weight": 1,
                    "enabled": True,
                },
            ],
        )
        self.assertEqual(rendered["censorship"]["tls_fetch_scope"], config_tool.TLS_FETCH_DIRECT_SCOPE)
        self.assertEqual(rendered["censorship"]["tls_fetch"], {"strict_route": True})

    def test_unauthenticated_socks5_uses_safe_defaults(self) -> None:
        data = self.base_config()
        data["upstreams"] = [
            {
                "type": "socks5",
                "address": "[2001:db8::10]:1080",
            }
        ]

        rendered = self.parse_rendered(data)

        self.assertEqual(
            rendered["upstreams"],
            [
                {
                    "type": "socks5",
                    "address": "[2001:db8::10]:1080",
                    "weight": 1,
                    "enabled": True,
                },
                {
                    "type": "direct",
                    "scopes": config_tool.TLS_FETCH_DIRECT_SCOPE,
                    "weight": 1,
                    "enabled": True,
                },
            ],
        )

    def test_no_tls_domain_does_not_add_scoped_direct_route(self) -> None:
        for representation in ("absent", "empty"):
            with self.subTest(representation=representation):
                data = self.base_config()
                data["proxy"]["modes"] = {"classic": True, "secure": False, "tls": False}
                if representation == "absent":
                    data["proxy"].pop("tls_domain")
                else:
                    data["proxy"]["tls_domain"] = ""
                data["proxy"]["mask"] = False
                data["proxy"]["tls_emulation"] = False
                data["upstreams"] = [
                    {
                        "type": "socks5",
                        "address": "proxy.example.invalid:1080",
                    }
                ]

                rendered = self.parse_rendered(data)

                self.assertNotIn("tls_domain", rendered["censorship"])
                self.assertNotIn("tls_fetch_scope", rendered["censorship"])
                self.assertNotIn("tls_fetch", rendered["censorship"])
                self.assertEqual(
                    rendered["upstreams"],
                    [
                        {
                            "type": "socks5",
                            "address": "proxy.example.invalid:1080",
                            "weight": 1,
                            "enabled": True,
                        }
                    ],
                )

    def test_tls_emulation_without_explicit_domain_still_fetches_direct(self) -> None:
        data = self.base_config()
        data["proxy"]["modes"] = {"classic": True, "secure": False, "tls": False}
        data["proxy"].pop("tls_domain")
        data["proxy"]["mask"] = False
        data["upstreams"] = [
            {
                "type": "socks5",
                "address": "proxy.example.invalid:1080",
            }
        ]

        rendered = self.parse_rendered(data)

        self.assertNotIn("tls_domain", rendered["censorship"])
        self.assertEqual(rendered["censorship"]["tls_fetch_scope"], config_tool.TLS_FETCH_DIRECT_SCOPE)
        self.assertEqual(rendered["censorship"]["tls_fetch"], {"strict_route": True})
        self.assertEqual(rendered["upstreams"][0]["type"], "socks5")
        self.assertNotIn("scopes", rendered["upstreams"][0])
        self.assertEqual(
            rendered["upstreams"][1],
            {
                "type": "direct",
                "scopes": config_tool.TLS_FETCH_DIRECT_SCOPE,
                "weight": 1,
                "enabled": True,
            },
        )

    def test_without_upstreams_or_tls_domain_uses_telemt_defaults(self) -> None:
        data = self.base_config()
        data.pop("upstreams")
        data["proxy"]["modes"] = {"classic": True, "secure": False, "tls": False}
        data["proxy"].pop("tls_domain")
        data["proxy"]["mask"] = False
        data["proxy"]["tls_emulation"] = False

        rendered = self.parse_rendered(data)

        self.assertNotIn("tls_domain", rendered["censorship"])
        self.assertNotIn("tls_fetch_scope", rendered["censorship"])
        self.assertNotIn("tls_fetch", rendered["censorship"])
        self.assertNotIn("upstreams", rendered)

    def test_multiple_socks_upstreams_share_one_internal_direct_route(self) -> None:
        data = self.base_config()
        data["upstreams"] = [
            {"type": "socks5", "address": "proxy-a.example.invalid:1080"},
            {"type": "socks5", "address": "proxy-b.example.invalid:1080", "weight": 2},
        ]

        rendered = self.parse_rendered(data)

        socks_routes = [item for item in rendered["upstreams"] if item["type"] == "socks5"]
        direct_routes = [item for item in rendered["upstreams"] if item["type"] == "direct"]
        self.assertEqual(len(socks_routes), 2)
        self.assertTrue(all("scopes" not in item for item in socks_routes))
        self.assertEqual(
            direct_routes,
            [
                {
                    "type": "direct",
                    "scopes": config_tool.TLS_FETCH_DIRECT_SCOPE,
                    "weight": 1,
                    "enabled": True,
                }
            ],
        )

    def test_sensitive_sections_cannot_be_printed_by_lookup(self) -> None:
        data = self.base_config()
        for key in ("users", "users.hello.secret", "upstreams"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(config_tool.ConfigError, "sensitive"):
                    config_tool.lookup(data, key)

    def test_internal_routing_keys_are_not_part_of_public_yaml(self) -> None:
        proxy_case = self.base_config()
        proxy_case["proxy"]["tls_fetch_scope"] = config_tool.TLS_FETCH_DIRECT_SCOPE
        with self.assertRaisesRegex(config_tool.ConfigError, "unknown keys in proxy"):
            config_tool.validate(proxy_case)

        upstream_case = self.base_config()
        upstream_case["upstreams"] = [
            {
                "type": "socks5",
                "address": "proxy.example.invalid:1080",
                "scopes": config_tool.TLS_FETCH_DIRECT_SCOPE,
            }
        ]
        with self.assertRaisesRegex(config_tool.ConfigError, r"unknown keys in upstreams\[0\]"):
            config_tool.validate(upstream_case)

    def test_invalid_upstreams_are_rejected_without_leaking_password(self) -> None:
        base = self.base_config()
        cases = [
            ("not-list", {}, "upstreams must be a list"),
            ("not-mapping", ["socks5"], r"upstreams\[0\] must be a mapping"),
            (
                "unknown-key",
                [{"type": "socks5", "address": "proxy.example.invalid:1080", "extra": True}],
                r"unknown keys in upstreams\[0\]",
            ),
            (
                "unsupported-type",
                [{"type": "direct", "address": "proxy.example.invalid:1080"}],
                r"upstreams\[0\]\.type must be socks5",
            ),
            (
                "bad-address",
                [{"type": "socks5", "address": "proxy.example.invalid"}],
                r"upstreams\[0\]\.address",
            ),
            (
                "bad-port",
                [{"type": "socks5", "address": "proxy.example.invalid:65536"}],
                r"upstreams\[0\]\.address port",
            ),
            (
                "missing-password",
                [{"type": "socks5", "address": "proxy.example.invalid:1080", "username": "user"}],
                r"username and upstreams\[0\]\.password must be set together",
            ),
            (
                "empty-username",
                [
                    {
                        "type": "socks5",
                        "address": "proxy.example.invalid:1080",
                        "username": "",
                        "password": "do-not-leak-this-password",
                    }
                ],
                r"upstreams\[0\]\.username must be a non-empty string",
            ),
            (
                "bad-weight",
                [{"type": "socks5", "address": "proxy.example.invalid:1080", "weight": 0}],
                r"upstreams\[0\]\.weight",
            ),
            (
                "all-disabled",
                [{"type": "socks5", "address": "proxy.example.invalid:1080", "enabled": False}],
                "upstreams must contain at least one enabled entry",
            ),
        ]

        for name, upstreams, pattern in cases:
            with self.subTest(name=name):
                data = copy.deepcopy(base)
                data["upstreams"] = upstreams
                with self.assertRaisesRegex(config_tool.ConfigError, pattern) as raised:
                    config_tool.validate(data)
                self.assertNotIn("do-not-leak-this-password", str(raised.exception))

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits are not available")
    def test_authenticated_upstream_requires_private_yaml(self) -> None:
        data = self.base_config()
        data["upstreams"] = [
            {
                "type": "socks5",
                "address": "proxy.example.invalid:1080",
                "username": "proxy-user",
                "password": "proxy-password",
            }
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.yaml"
            path.write_text(yaml.safe_dump(data), encoding="utf-8")
            path.chmod(0o644)
            with self.assertRaisesRegex(config_tool.ConfigError, "must be chmod 600"):
                config_tool.validate(config_tool.load_yaml(path), path)
            path.chmod(0o600)
            config_tool.validate(config_tool.load_yaml(path), path)


if __name__ == "__main__":
    unittest.main()
