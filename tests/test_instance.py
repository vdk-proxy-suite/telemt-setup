"""Lifecycle contract tests with real files and mocked OS service/account commands.
Real systemd/Telemt acceptance is a separate disposable-WSL integration run.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import config as schema
import instance as lifecycle


class SchemaIsolationTests(unittest.TestCase):
    def data(self):
        data = yaml.safe_load((ROOT / "config.example.yaml").read_text())
        data["links"]["public_host"] = "proxy.example.invalid"
        return data

    def test_id_validation_and_canonical_resources(self):
        for invalid in (None, "", "../other", "Main", "a_b", "a" * 21, "0bad"):
            with self.subTest(invalid=invalid), self.assertRaises(schema.ConfigError):
                schema.instance_paths(invalid)
        a, b = lifecycle.paths("alpha"), lifecycle.paths("beta")
        self.assertTrue(all(a[k] != b[k] for k in a))
        data = schema.validate(self.data())
        self.assertEqual(data["install"]["service_name"], "telemt-main")
        self.assertEqual(data["install"]["binary_path"], "/opt/telemt-setup/instances/main/bin/telemt")

    def test_override_and_public_observability_are_rejected(self):
        data = self.data()
        data["install"]["user"] = "root"
        with self.assertRaisesRegex(schema.ConfigError, "derived"):
            schema.validate(data)
        for key in ("api_listen", "metrics_listen"):
            data = self.data()
            data["server"][key] = "0.0.0.0:19091"
            with self.assertRaisesRegex(schema.ConfigError, "loopback"):
                schema.validate(data)

    def test_legacy_requires_explicit_selection_but_renderer_preserves_secrets(self):
        data = self.data()
        data.pop("instance")
        data["install"].update({"service_name": "telemt", "user": "telemt", "group": "telemt", "binary_path": "/bin/telemt", "config_path": "/etc/telemt/telemt.toml", "work_dir": "/opt/telemt", "backup_root": "/var/backups/telemt-setup", "state_root": "/var/lib/telemt-setup"})
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "config.yaml"
            file.write_text(yaml.safe_dump(data))
            args = lifecycle.parser().parse_args(["all", "--config", str(file)])
            with self.assertRaisesRegex(schema.ConfigError, "explicit --legacy"):
                lifecycle.select(args)
        secret = __import__("secrets").token_hex(16)
        output = schema.render(schema.validate(data), {"hello": secret})
        self.assertEqual(__import__("tomllib").loads(output)["access"]["users"]["hello"], secret)

    def test_conflicting_cli_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "config.yaml"
            file.write_text(yaml.safe_dump(self.data()))
            with self.assertRaisesRegex(schema.ConfigError, "conflicts"):
                lifecycle.select(lifecycle.parser().parse_args(["all", "--instance", "other", "--config", str(file)]))

    def test_external_e2e_accepts_instance_and_yaml_without_linux_manifest(self):
        import healthcheck
        data = self.data()
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "config.yaml"
            file.write_text(yaml.safe_dump(data), encoding="utf-8")
            argv = ["healthcheck.py", "--scope", "e2e", "--instance", "main", "--config", str(file)]
            with patch.object(sys, "argv", argv), patch.object(healthcheck, "check_e2e") as probe, patch.object(lifecycle, "read_manifest", side_effect=AssertionError("external e2e must not access Linux state")):
                self.assertEqual(healthcheck.main(), 0)
                probe.assert_called_once()
            argv[4] = "different"
            with patch.object(sys, "argv", argv), patch.object(healthcheck, "check_e2e") as probe:
                self.assertEqual(healthcheck.main(), 1)
                probe.assert_not_called()

    def test_unit_is_independent_of_extraction(self):
        p = lifecycle.paths("example")
        unit = lifecycle.unit_text(p, "example")
        self.assertIn("User=telemt-example", unit)
        self.assertIn("RuntimeDirectory=telemt-example", unit)
        self.assertIn("StandardOutput=append:/var/log/telemt/instances/example/telemt.log", unit)
        self.assertNotIn(str(ROOT), unit)
        self.assertNotIn("/bin/telemt ", unit.replace(p["binary_path"], ""))


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.real_paths = lifecycle.paths
        self.users, self.groups, self.services = {}, {}, {}
        self.calls, self.ufw, self.sockets = [], [], {}
        self.fail_healthcheck = False
        self.seq = 5000
        self.os_patches = [
            patch.object(lifecycle, "paths", side_effect=self.paths),
            patch.object(lifecycle, "guard_path", side_effect=self.guard),
            patch.object(lifecycle, "run", side_effect=self.command),
            patch.object(lifecycle.os, "chown", create=True),
            patch.dict(sys.modules, {"pwd": SimpleNamespace(getpwnam=self.user, getpwall=lambda: list(self.users.values())), "grp": SimpleNamespace(getgrnam=self.group)}),
        ]
        for item in self.os_patches:
            item.start()
            self.addCleanup(item.stop)
        self.archive = self.base / "official.tar.gz"
        binary = self.base / "telemt"
        binary.write_bytes(b"fixture for lifecycle tests; not a real Telemt executable\n")
        with tarfile.open(self.archive, "w:gz") as tar:
            tar.add(binary, arcname="telemt")

    def paths(self, instance_id):
        p = self.real_paths(instance_id)
        return {key: str(self.base / "system" / value.lstrip("/")) if value.startswith("/") else value for key, value in p.items()}

    def guard(self, path, private=False):
        path = Path(path)
        if not path.is_absolute() or ".." in path.parts or self.base not in path.parents:
            raise schema.ConfigError("unsafe test path")
        for ancestor in [path, *path.parents]:
            if ancestor.is_symlink():
                raise schema.ConfigError("symlink resource/ancestor refused")
        if path.exists() and path.is_file() and path.stat().st_nlink > 1:
            raise schema.ConfigError("hardlinked resource refused")
        return path

    def user(self, name):
        if name not in self.users:
            raise KeyError(name)
        return self.users[name]

    def group(self, name):
        if name not in self.groups:
            raise KeyError(name)
        return self.groups[name]

    def command(self, *args, check=True, capture=False):
        args = [str(x) for x in args]
        self.calls.append(args)
        rc, out = 0, ""
        if args[0] == "systemctl":
            action = args[1]
            unit = next((x for x in args[2:] if x.endswith(".service")), "")
            service = self.services.setdefault(unit, {"active": False, "enabled": False, "pid": "12345"})
            if action == "show":
                if "MainPID" in args:
                    out = service["pid"] if service["active"] else "0"
                elif "FragmentPath" in args:
                    out = ""
            elif action in {"is-active", "is-enabled"}:
                rc = 0 if service["active" if action == "is-active" else "enabled"] else 3
            elif action in {"start", "stop"}:
                service["active"] = action == "start"
            elif action in {"enable", "disable"}:
                service["enabled"] = action == "enable"
                if "--now" in args:
                    service["active"] = action == "enable"
        elif args[0] == "ss":
            port = int(args[-1].split(":")[-1])
            out = self.sockets.get(port, "")
        elif args[0] == "groupadd":
            self.seq += 1
            self.groups[args[-1]] = SimpleNamespace(gr_gid=self.seq, gr_mem=[])
        elif args[0] == "useradd":
            self.seq += 1
            self.users[args[-1]] = SimpleNamespace(pw_uid=self.seq, pw_gid=self.groups[args[args.index("--gid") + 1]].gr_gid, pw_dir=args[args.index("--home-dir") + 1], pw_shell=args[args.index("--shell") + 1])
        elif args[0] == "userdel":
            del self.users[args[-1]]
        elif args[0] == "groupdel":
            del self.groups[args[-1]]
        elif args[0] == "pgrep":
            rc = 1
        elif args[0] == "dpkg-query":
            out = "install ok installed"
        elif args[0] == "uname":
            out = "x86_64"
        elif args[0] == "ldd":
            out = "GNU libc 2.39"
        elif args[0] == "ufw":
            import shlex
            if args[1:3] == ["show", "added"]:
                out = "\n".join(shlex.join(rule) for rule in self.ufw)
            elif args[1] == "allow":
                self.ufw.append(args)
            elif args[1:3] == ["--force", "delete"]:
                self.ufw.remove(["ufw", *args[3:]])
            else:
                raise AssertionError(args)
        elif args[-1] == "--version":
            out = "Telemt 3.5.7"
        elif args[0] == sys.executable and "--scope" in args:
            rc = 1 if self.fail_healthcheck else 0
        else:
            raise AssertionError(f"unexpected external command: {args}")
        if check and rc:
            raise subprocess.CalledProcessError(rc, args)
        return subprocess.CompletedProcess(args, rc, stdout=out, stderr="")

    def create(self, name, port):
        source = self.base / ("unpacked-" + name)
        source.mkdir()
        for filename in ("setuptelemt.sh", "cleantelemt.sh", "README.md", "VERSION", "config.example.yaml", "THIRD_PARTY_NOTICES.md"):
            shutil.copy2(ROOT / filename, source / filename)
        for folder in ("tools", "lib", "steps"):
            shutil.copytree(ROOT / folder, source / folder, ignore=shutil.ignore_patterns("__pycache__"))
        data = yaml.safe_load((ROOT / "config.example.yaml").read_text())
        data["instance"]["id"] = name
        data["links"]["public_host"] = "proxy.example.invalid"
        data["server"].update(port=port, api_listen=f"127.0.0.1:{port+1}", metrics_listen=f"127.0.0.1:{port+2}")
        data["install"]["sha256"] = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        item = lifecycle.Instance(name, schema.validate(data), source)
        item.register()
        return item

    def install(self, item):
        item.backup(stop=True)
        item.persist_source()
        item.install(self.archive)
        item.configure()
        item.start()
        return item

    def args(self, *extra):
        return lifecycle.parser().parse_args(["cleanup", "--yes", *extra])

    def snapshot(self, item):
        result = {}
        for key in ("unit", "logrotate", "binary_path", "config_path"):
            result[key] = Path(item.p[key]).read_bytes()
        result["manifest"] = (item.state / "manifest.json").read_bytes()
        result["account"] = copy.deepcopy(self.users[item.p["user"]].__dict__)
        result["service"] = copy.deepcopy(self.services[item.p["service_name"] + ".service"])
        return result

    def test_two_instances_update_cleanup_and_deferred_purge_preserve_neighbor(self):
        a, b = self.install(self.create("alpha", 24000)), self.install(self.create("beta", 25000))
        snapshot = self.snapshot(b)
        log = Path(a.p["logs"]) / "telemt.log"
        log.write_text("retained service log\n")
        a.backup(stop=True)
        a.persist_source()
        a.configure()
        a.start()
        a.cleanup(self.args())
        self.assertEqual(snapshot, self.snapshot(b))
        self.assertTrue(log.exists())
        self.assertNotIn(a.p["user"], self.users)
        self.assertNotIn(a.p["group"], self.groups)
        self.assertFalse(a.config.exists())
        restored = lifecycle.Instance("alpha")
        restored.cleanup(self.args("--purge-logs", "--purge-shared-components", "--purge-setup"))
        self.assertFalse(log.exists())
        self.assertFalse(a.source.exists())
        self.assertTrue(Path(a.p["code"]).exists())
        self.assertEqual(snapshot, self.snapshot(b))
        restored.cleanup(self.args("--purge-logs"))
        self.assertTrue((a.state / "manifest.json").exists())
        self.assertFalse(any(cmd[0] in {"apt-get", "journalctl"} for cmd in self.calls))

    def test_duplicate_source_requires_update_existing_and_move_keeps_identity(self):
        a = self.create("alpha", 24000)
        other = self.base / "other"
        shutil.copytree(a.source, other)
        b = lifecycle.Instance("alpha", a.data, other)
        with self.assertRaisesRegex(schema.ConfigError, "update-existing"):
            b.register()
        b.register(update_existing=True)
        self.assertEqual(b.p, a.p)
        self.assertEqual(b.manifest["source"], str(other))

    def test_failed_update_restores_config_binary_ports_unit_and_running_state(self):
        a = self.install(self.create("alpha", 24000))
        old = {k: Path(a.p[k]).read_bytes() for k in ("binary_path", "config_path", "unit", "logrotate")}
        a.data["server"]["port"] = 26000
        a.backup(stop=True)
        a.persist_source()
        a.configure()
        self.fail_healthcheck = True
        with self.assertRaises(subprocess.CalledProcessError):
            a.start()
        a.rollback()
        a.verify()
        self.assertTrue(a.active())
        self.assertEqual(a.manifest["ports"], [24000, 24001, 24002])
        self.assertEqual(old, {k: Path(a.p[k]).read_bytes() for k in old})

    def test_port_conflicts_do_not_stop_active_target(self):
        a = self.install(self.create("alpha", 24000))
        self.sockets[24001] = 'LISTEN 0 128 127.0.0.1:24001 users:(("foreign",pid=777,fd=3))'
        before = len(self.calls)
        with self.assertRaisesRegex(schema.ConfigError, "another process"):
            a.backup(stop=True)
        self.assertTrue(a.active())
        self.assertFalse(any(cmd[:2] == ["systemctl", "stop"] for cmd in self.calls[before:]))

    def test_stopped_neighbor_reserves_ports(self):
        self.install(self.create("alpha", 24000))
        b = self.create("beta", 24000)
        with self.assertRaisesRegex(schema.ConfigError, "registered"):
            b.check_ports()

    def test_partial_install_cleanup_and_pending_atomic_file(self):
        a = self.create("alpha", 24000)
        a.persist_source()
        a.install(self.archive)
        a.configure()
        pending = "# interrupted but owned logrotate write\n"
        a.manifest["pending_hashes"] = {"logrotate": hashlib.sha256(pending.encode()).hexdigest()}
        a.save()
        Path(a.p["logrotate"]).write_bytes(pending.encode())
        a = lifecycle.Instance("alpha")
        a.cleanup(self.args("--purge-logs"))
        self.assertNotIn(a.p["user"], self.users)
        self.assertFalse(Path(a.p["binary_dir"]).exists())

    def test_malformed_manifest_foreign_unit_and_changed_account_fail_closed(self):
        a = self.install(self.create("alpha", 24000))
        unit = Path(a.p["unit"])
        original = unit.read_bytes()
        unit.write_text("[Service]\nExecStart=/bin/other\n")
        with self.assertRaisesRegex(schema.ConfigError, "unowned or modified"):
            a.cleanup(self.args())
        unit.write_bytes(original)
        self.users[a.p["user"]].pw_uid += 1
        with self.assertRaisesRegex(schema.ConfigError, "identity"):
            a.cleanup(self.args())
        file = a.state / "manifest.json"
        value = json.loads(file.read_text())
        value["paths"]["work_dir"] = str(self.base)
        file.write_text(json.dumps(value))
        with self.assertRaisesRegex(schema.ConfigError, "foreign or malformed"):
            lifecycle.Instance("alpha")

    def test_symlink_root_is_rejected_without_deleting_target(self):
        a = self.create("alpha", 24000)
        external = self.base / "external"
        external.mkdir()
        sentinel = external / "sentinel"
        sentinel.write_text("do not delete")
        path = Path(a.p["work_dir"])
        path.parent.mkdir(parents=True)
        try:
            path.symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest("OS disallows creation of symlinks")
        with self.assertRaisesRegex(schema.ConfigError, "symlink"):
            a.cleanup(self.args())
        self.assertTrue(sentinel.exists())

    def test_owned_firewall_rule_and_preexisting_shared_rule(self):
        a = self.create("alpha", 24000)
        a.data["install"]["manage_ufw"] = True
        self.install(a)
        self.assertIn(["ufw", "allow", "24000/tcp", "comment", "telemt-setup:alpha"], self.ufw)
        a.cleanup(self.args())
        self.assertTrue(self.ufw)
        a.cleanup(self.args("--purge-shared-components", "--purge-logs"))
        self.assertFalse(self.ufw)
        b = self.create("beta", 25000)
        b.data["install"]["manage_ufw"] = True
        self.ufw.append(["ufw", "allow", "25000/tcp"])
        self.install(b)
        b.cleanup(self.args("--purge-shared-components", "--purge-logs"))
        self.assertEqual(self.ufw, [["ufw", "allow", "25000/tcp"]])

    def test_cleanup_reinstall_recreates_exact_account_and_preserves_neighbor(self):
        a = self.install(self.create("alpha", 24000))
        b = self.install(self.create("beta", 25000))
        before = self.snapshot(b)
        old_uid = self.users[a.p["user"]].pw_uid
        a.cleanup(self.args())
        a = lifecycle.Instance("alpha", a.data, a.source)
        a.register()
        self.install(a)
        self.assertNotEqual(old_uid, self.users[a.p["user"]].pw_uid)
        self.assertEqual(a.manifest["uid"], self.users[a.p["user"]].pw_uid)
        self.assertEqual(before, self.snapshot(b))
        a.cleanup(self.args("--purge-logs"))
        a = lifecycle.Instance("alpha", a.data, a.source)
        a.register()
        self.install(a)
        self.assertTrue(a.active())
        self.assertEqual(before, self.snapshot(b))

    def test_source_logs_preserved_and_invalid_source_rejected_before_stop(self):
        a = self.install(self.create("alpha", 24000))
        log = a.source / "setup.log"
        log.write_text("retain setup output")
        a.cleanup(self.args("--purge-setup"))
        self.assertTrue(log.exists())
        a.cleanup(self.args("--purge-setup", "--purge-logs"))
        self.assertFalse(a.source.exists())
        b = self.install(self.create("beta", 25000))
        marker = b.source / ".telemt-setup-instance.json"
        marker.write_text("{}")
        before = self.snapshot(b)
        with self.assertRaises(schema.ConfigError):
            b.cleanup(self.args("--purge-setup"))
        self.assertEqual(before, self.snapshot(b))

    def test_hardlinked_log_aborts_before_cleanup_mutations(self):
        a = self.install(self.create("alpha", 24000))
        foreign = self.base / "foreign.log"
        foreign.write_text("outside instance")
        log = Path(a.p["logs"]) / "linked.log"
        os.link(foreign, log)
        before = self.snapshot(a)
        with self.assertRaisesRegex(schema.ConfigError, "hardlinked"):
            a.cleanup(self.args())
        self.assertEqual(before, self.snapshot(a))
        self.assertEqual(foreign.read_text(), "outside instance")

    def test_foreign_stopped_unit_keeps_account(self):
        a = self.install(self.create("alpha", 24000))
        with patch.object(a, "account_consumers", return_value=True):
            a.cleanup(self.args("--purge-logs"))
        self.assertIn(a.p["user"], self.users)

    def test_log_purge_and_keep_data_do_not_recycle_account(self):
        a = self.install(self.create("alpha", 24000))
        uid = self.users[a.p["user"]].pw_uid
        a.cleanup(self.args("--keep-data", "--purge-logs"))
        self.assertEqual(self.users[a.p["user"]].pw_uid, uid)
        a.cleanup(self.args("--purge-logs"))
        self.assertNotIn(a.p["user"], self.users)


if __name__ == "__main__":
    unittest.main()
