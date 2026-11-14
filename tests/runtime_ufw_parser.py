"""Verify against unpacked Ubuntu UFW, without touching host firewall.

python3 tests/runtime_ufw_parser.py /tmp/unpacked/usr/lib/python3/dist-packages
"""
import gettext
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, sys.argv[1])
gettext.install("ufw")
from ufw.parser import UFWCommandRule
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import firewall


data = {"schema_version": 2, "server": {"listen_ips": ["0.0.0.0", "::"], "port": 24443}}
rules = firewall.desired(data, "test")
with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    for index, command in enumerate(rules):
        response = UFWCommandRule("allow").parse(command[1:])
        assert response.data["iptype"] == ("v4" if index == 0 else "v6")
        parsed = response.data["rule"]
        parsed.set_v6(index == 1)
        canonical = UFWCommandRule.get_command(parsed)
        assert canonical == "allow 24443/tcp comment 'telemt-setup:test'", canonical
        row = f"### tuple ### allow {parsed.protocol} {parsed.dport} {parsed.dst} {parsed.sport} {parsed.src} in comment={parsed.comment}\n"
        (root / ("user.rules" if index == 0 else "user6.rules")).write_text(row)
    actual = firewall.saved_rules(root)
    assert len(actual) == 2
    assert all(firewall.same(left, right) for left, right in zip(rules, actual))
    assert not firewall.same(actual[0], actual[1])
print("PASS Ubuntu UFW parser: wildcard shorthand loses family; saved tuples preserve exact ownership")
