#!/usr/bin/env python3
"""Owned, sequential Telemt instance lifecycle. No shell-evaluated state files."""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

import yaml
import config as schema

PROJECT = "telemt-setup"
ROOT = Path(__file__).resolve().parents[1]
MANIFEST_VERSION = 1
MUTATING = {"all", "0", "1", "2", "3", "update", "reconfigure", "start", "stop", "backup"}


def run(*args, check=True, capture=False):
    return subprocess.run([str(x) for x in args], check=check, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def paths(instance_id):
    install = schema.instance_paths(instance_id)
    return {**install,
        "state": install["state_root"],
        "config_dir": str(PurePosixPath(install["config_path"]).parent),
        "binary_dir": f"/opt/telemt-setup/instances/{instance_id}",
        "logs": f"/var/log/telemt/instances/{instance_id}",
        "runtime": f"/run/telemt-{instance_id}",
        "code": f"/usr/local/lib/telemt-setup/instances/{instance_id}",
        "unit": f"/etc/systemd/system/telemt-{instance_id}.service",
        "logrotate": f"/etc/logrotate.d/telemt-{instance_id}",
    }


def guard_path(path, private=False):
    """Reject aliases and writable ancestors before any privileged filesystem use."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise schema.ConfigError("unsafe resource path")
    for item in [*reversed(path.parents), path]:
        if item.is_symlink():
            raise schema.ConfigError(f"symlink resource/ancestor refused: {item}")
        if item.exists() and os.name == "posix":
            st = item.stat()
            if item != path or private:
                trusted_log_root = False
                if item == Path("/var/log") and st.st_uid == 0 and not st.st_mode & 0o002:
                    import grp
                    try:
                        trusted_log_root = st.st_gid == grp.getgrnam("syslog").gr_gid
                    except KeyError:
                        pass
                if st.st_uid != 0 or (st.st_mode & 0o022 and not trusted_log_root):
                    raise schema.ConfigError(f"resource is not controlled by root: {item}")
    if path.exists() and path.is_file() and path.stat().st_nlink != 1:
        raise schema.ConfigError(f"hardlinked resource refused: {path}")
    return path


def write_private(path, data, mode=0o600):
    path = guard_path(path, private=True)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def remove_tree(path):
    path = guard_path(path)
    if not path.exists():
        return
    device = path.stat().st_dev
    for directory, dirs, files in os.walk(path, followlinks=False):
        for entry in dirs:
            child = Path(directory) / entry
            if not child.is_symlink() and (child.stat().st_dev != device or os.path.ismount(child)):
                raise schema.ConfigError(f"mounted subtree refused: {child}")
    if os.path.ismount(path):
        raise schema.ConfigError(f"mounted resource refused: {path}")
    shutil.rmtree(path)


def read_manifest(instance_id):
    p = paths(instance_id)
    file = guard_path(Path(p["state"]) / "manifest.json", private=True)
    if not file.exists():
        return None
    try:
        value = json.loads(file.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise schema.ConfigError("invalid ownership manifest") from exc
    if (not isinstance(value, dict) or value.get("schema") != MANIFEST_VERSION
            or value.get("project") != PROJECT or value.get("id") != instance_id
            or value.get("paths") != p or not isinstance(value.get("source"), str)
            or not isinstance(value.get("claims"), list)
            or not all(isinstance(x, str) for x in value["claims"])
            or not set(value["claims"]).issubset(RESOURCE_KEYS)
            or not isinstance(value.get("hashes"), dict)
            or not isinstance(value.get("pending_hashes", {}), dict)
            or not isinstance(value.get("ports", []), list)
            or not all(type(x) is int and 0 < x < 65536 for x in value.get("ports", []))
            or any(value.get(k) is not None and (type(value[k]) is not int or value[k] <= 0) for k in ("uid", "gid"))):
        raise schema.ConfigError("foreign or malformed ownership manifest")
    return value


RESOURCE_KEYS = {"config_dir", "binary_dir", "work_dir", "logs", "runtime", "code", "unit", "logrotate"}


class Instance:
    def __init__(self, instance_id, data=None, source=ROOT):
        self.id, self.p, self.data = instance_id, paths(instance_id), data
        self.source = Path(source).resolve()
        self.manifest = read_manifest(instance_id)
        self.state = Path(self.p["state"])
        self.config = self.state / "config.yaml"

    def mark_source(self):
        if str(self.source) == self.p["code"]:
            return
        marker = self.source / ".telemt-setup-instance.json"
        token = self.manifest.get("source_token") or secrets.token_hex(24)
        if marker.is_symlink():
            raise schema.ConfigError("source ownership marker is a symlink")
        if marker.exists():
            try:
                old = json.loads(marker.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise schema.ConfigError("invalid source ownership marker") from exc
            if old != {"project": PROJECT, "id": self.id, "token": token}:
                raise schema.ConfigError("unpacking directory belongs to another installation")
        else:
            with marker.open("x", encoding="utf-8") as stream:
                json.dump({"project": PROJECT, "id": self.id, "token": token}, stream)
            marker.chmod(0o600)
        source_stat = self.source.stat()
        self.manifest["source_token"] = token
        self.manifest["source_identity"] = [source_stat.st_dev, source_stat.st_ino]
        self.save()

    def remove_source(self, purge_logs=False, apply=True):
        source = Path(self.manifest["source"])
        if not source.exists():
            return
        if source != source.resolve() or len(source.parts) < 3 or (source / ".git").exists():
            raise schema.ConfigError("refusing to purge aliased, system-root or Git source directory")
        if source == Path(self.p["code"]):
            raise schema.ConfigError("installed controller is retained for repeated cleanup")
        current = source.stat()
        if [current.st_dev, current.st_ino] != self.manifest.get("source_identity"):
            raise schema.ConfigError("unpacking directory identity changed; it will not be deleted")
        marker = source / ".telemt-setup-instance.json"
        if marker.is_symlink() or not marker.is_file():
            raise schema.ConfigError("source ownership marker is absent or aliased")
        if json.loads(marker.read_text(encoding="utf-8")) != {"project": PROJECT, "id": self.id, "token": self.manifest.get("source_token")}:
            raise schema.ConfigError("source ownership marker does not match manifest")
        for directory, dirs, files in os.walk(source, followlinks=False):
            for child in dirs:
                path = Path(directory) / child
                if not path.is_symlink() and (path.stat().st_dev != current.st_dev or os.path.ismount(path)):
                    raise schema.ConfigError("mounted source subtree refused")
        if os.path.ismount(source):
            raise schema.ConfigError("mounted source directory refused")
        if not purge_logs:
            standard = {"setuptelemt.sh", "cleantelemt.sh", "README.md", "VERSION", "THIRD_PARTY_NOTICES.md", "config.example.yaml", ".telemt-setup-instance.json"}
            for file in source.rglob("*"):
                if not file.is_file():
                    continue
                relative = file.relative_to(source)
                known = (relative.as_posix() in standard
                         or file.name.startswith("config") and file.suffix == ".yaml"
                         or relative.parts[0] in {"tools", "lib", "steps"} and file.suffix in {".py", ".sh", ".pyc"})
                if not known:
                    print("unpacking directory retained: extra files may contain setup logs; add --purge-logs to --purge-setup")
                    return
        if apply:
            shutil.rmtree(source)

    def save(self):
        write_private(self.state / "manifest.json", json.dumps(self.manifest, indent=2) + "\n")

    def verify(self):
        if self.manifest is None:
            raise schema.ConfigError("instance is not registered; run setup with --config")
        for key in RESOURCE_KEYS:
            guard_path(self.p[key], private=key in {"code", "unit", "logrotate", "binary_dir", "config_dir", "logs"})
        for key in ("unit", "logrotate"):
            file = Path(self.p[key])
            if file.exists():
                expected = self.manifest.get("hashes", {}).get(key)
                if hashlib.sha256(file.read_bytes()).hexdigest() not in {expected, self.manifest.get("pending_hashes", {}).get(key)}:
                    raise schema.ConfigError(f"unowned or modified {key}; refusing lifecycle changes")
        self.recover_accounts()
        # Foreign drop-ins can change the service user, executable or namespace.
        dropins = Path(self.p["unit"] + ".d")
        if dropins.exists() or dropins.is_symlink():
            raise schema.ConfigError("unmanaged systemd drop-ins found")
        if self.manifest.get("uid") is not None:
            import pwd
            try:
                user = pwd.getpwnam(self.p["user"])
            except KeyError:
                pass
            else:
                if (user.pw_uid != self.manifest["uid"] or user.pw_gid != self.manifest.get("gid")
                        or user.pw_dir != self.p["work_dir"] or user.pw_shell != "/usr/sbin/nologin"):
                    raise schema.ConfigError("service user identity has changed")
        if self.manifest.get("gid") is not None:
            import grp
            try:
                group = grp.getgrnam(self.p["group"])
            except KeyError:
                pass
            else:
                if group.gr_gid != self.manifest["gid"]:
                    raise schema.ConfigError("service group identity has changed")

    def recover_accounts(self):
        import pwd
        import grp
        changed = False
        if self.manifest.get("gid") is None and self.manifest.get("creating_group"):
            try:
                group = grp.getgrnam(self.p["group"])
            except KeyError:
                pass
            else:
                if group.gr_mem:
                    raise schema.ConfigError("interrupted group creation has unexpected members")
                self.manifest["gid"] = group.gr_gid
                changed = True
        if self.manifest.get("uid") is None and self.manifest.get("creating_user"):
            try:
                user = pwd.getpwnam(self.p["user"])
            except KeyError:
                pass
            else:
                if user.pw_gid != self.manifest.get("gid") or user.pw_dir != self.p["work_dir"] or user.pw_shell != "/usr/sbin/nologin":
                    raise schema.ConfigError("interrupted account creation has unexpected identity")
                self.manifest["uid"] = user.pw_uid
                changed = True
        # Caller saves recovered identity together with its eventual mutation;
        # read-only verification and cleanup dry runs never write the manifest.

    def register(self, update_existing=False):
        if self.manifest is not None:
            self.verify()
            if str(self.source) not in {self.manifest["source"], self.p["code"]} and not update_existing:
                raise schema.ConfigError("instance.id already registered by another directory; use --update-existing")
            if update_existing:
                self.manifest["source"] = str(self.source)
                self.save()
            self.mark_source()
            return
        import pwd
        import grp
        for key in RESOURCE_KEYS:
            target = guard_path(self.p[key])
            if target.exists():
                raise schema.ConfigError(f"unowned existing resource: {target}")
        if Path(self.p["unit"] + ".d").exists():
            raise schema.ConfigError("unowned unit drop-ins exist")
        # A vendor unit or alias also owns the name, even without /etc unit file.
        fragment = run("systemctl", "show", self.p["service_name"] + ".service", "-p", "FragmentPath", "--value", capture=True, check=False)
        if fragment.stdout.strip():
            raise schema.ConfigError("service name already exists in systemd")
        for lookup, name in ((pwd.getpwnam, self.p["user"]), (grp.getgrnam, self.p["group"])):
            try:
                lookup(name)
            except KeyError:
                continue
            raise schema.ConfigError("service account already exists without ownership manifest")
        guard_path(self.state, private=True)
        if self.state.exists():
            raise schema.ConfigError("state directory exists without an ownership manifest")
        self.state.mkdir(parents=True, mode=0o700)
        self.manifest = {"schema": MANIFEST_VERSION, "project": PROJECT, "id": self.id,
                         "paths": self.p, "source": str(self.source), "claims": [],
                         "status": "preparing", "hashes": {}, "uid": None, "gid": None}
        self.save()
        self.mark_source()

    def claim(self, key):
        if key not in self.manifest["claims"]:
            target = guard_path(self.p[key])
            if target.exists():
                raise schema.ConfigError(f"unowned resource appeared: {target}")
            self.manifest["claims"].append(key)
            self.save()  # Durable ownership before creation; covers interrupted setup.

    def directory(self, key, mode=0o750):
        self.claim(key)
        path = guard_path(self.p[key])
        path.mkdir(parents=True, exist_ok=True, mode=mode)
        path.chmod(mode)
        return path

    def owned_file(self, key, text, mode=0o644):
        self.claim(key)
        # Record desired digest first so partial atomic writes are recognisable.
        self.manifest.setdefault("pending_hashes", {})[key] = hashlib.sha256(text.encode()).hexdigest()
        self.save()
        write_private(self.p[key], text, mode)
        self.manifest["hashes"][key] = self.manifest["pending_hashes"].pop(key)
        self.save()

    def persist_source(self):
        if self.source != Path(self.p["code"]):
            target = self.directory("code", 0o755)
            # Only runtime sources, never local YAML, tests, temporary downloads or Git.
            for relative in ["setuptelemt.sh", "cleantelemt.sh", "VERSION", "README.md", "config.example.yaml", "THIRD_PARTY_NOTICES.md"]:
                source = self.source / relative
                write_private(target / relative, source.read_text(encoding="utf-8"), 0o755 if relative.endswith(".sh") else 0o644)
            for folder in ("tools", "lib", "steps"):
                dest = target / folder
                dest.mkdir(exist_ok=True, mode=0o755)
                for source in (self.source / folder).iterdir():
                    if source.is_file() and source.suffix in {".py", ".sh"}:
                        write_private(dest / source.name, source.read_text(encoding="utf-8"), 0o755)
        write_private(self.config, yaml.safe_dump(self.data, sort_keys=False))

    def active(self):
        return run("systemctl", "is-active", "--quiet", self.p["service_name"] + ".service", check=False, capture=True).returncode == 0

    def check_ports(self):
        import re
        desired = {self.data["server"]["port"], schema.split_endpoint(self.data["server"]["api_listen"], "API")[1], schema.split_endpoint(self.data["server"]["metrics_listen"], "metrics")[1]}
        # Registered instances reserve their ports even while stopped.
        registry = self.state.parent
        for file in registry.glob("*/manifest.json"):
            if file.parent.name == self.id:
                continue
            neighbor = read_manifest(file.parent.name)
            if neighbor and neighbor.get("status") != "removed" and desired.intersection(neighbor.get("ports", [])):
                raise schema.ConfigError("ports overlap another registered instance")
        main_pid = run("systemctl", "show", self.p["service_name"] + ".service", "-p", "MainPID", "--value", capture=True, check=False).stdout.strip()
        for port in desired:
            output = run("ss", "-H", "-lntp", f"sport = :{port}", capture=True).stdout
            for line in output.splitlines():
                pids = set(re.findall(r"pid=(\d+)", line))
                if not pids or pids != {main_pid} or main_pid == "0":
                    raise schema.ConfigError(f"port {port} belongs to another process; no service was stopped")
        self.manifest["ports"] = sorted(desired)
        self.save()

    def backup(self, stop=False):
        self.verify()
        previous_ports = list(self.manifest.get("ports", []))
        self.check_ports()
        destination = self.state / "backups" / str(time.time_ns())
        guard_path(destination, private=True)
        destination.mkdir(parents=True, mode=0o700)
        files = {}
        for key, source in {"binary": self.p["binary_path"], "toml": self.p["config_path"], "unit": self.p["unit"], "logrotate": self.p["logrotate"], "yaml": self.config}.items():
            source = guard_path(source, private=True)
            files[key] = source.exists()
            if source.exists():
                shutil.copy2(source, destination / key)
        if Path(self.p["code"]).exists():
            shutil.copytree(self.p["code"], destination / "code")
        prior = {"directory": str(destination), "files": files, "active": self.active(),
                 "enabled": run("systemctl", "is-enabled", "--quiet", self.p["service_name"] + ".service", check=False, capture=True).returncode == 0,
                 "hashes": dict(self.manifest["hashes"]), "ports": previous_ports}
        write_private(destination / "backup.json", json.dumps(prior, indent=2))
        if stop:
            self.manifest["rollback"] = prior
            self.manifest["status"] = "preparing"
            self.save()
            run("systemctl", "stop", self.p["service_name"] + ".service", check=False, capture=True)
            if self.active():
                raise schema.ConfigError("target service did not stop")
        print("backup:", destination)

    def rollback(self):
        previous = self.manifest.get("rollback")
        if not previous:
            return
        destination = guard_path(previous["directory"], private=True)
        if destination.parent != self.state / "backups":
            raise schema.ConfigError("invalid rollback backup path")
        unit = self.p["service_name"] + ".service"
        run("systemctl", "stop", unit, check=False, capture=True)
        for key, target in {"binary": self.p["binary_path"], "toml": self.p["config_path"], "unit": self.p["unit"], "logrotate": self.p["logrotate"], "yaml": self.config}.items():
            target = guard_path(target, private=True)
            if previous["files"][key]:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination / key, target)
            elif target.exists():
                target.unlink()
        if (destination / "code").exists():
            remove_tree(self.p["code"])
            shutil.copytree(destination / "code", self.p["code"])
        self.manifest["pending_hashes"] = {}
        self.manifest["hashes"] = previous["hashes"]
        self.manifest["ports"] = previous["ports"]
        self.manifest["status"] = "rolled-back"
        self.manifest.pop("rollback", None)
        self.save()
        run("systemctl", "daemon-reload")
        run("systemctl", "enable" if previous["enabled"] else "disable", unit, check=False, capture=True)
        if previous["active"]:
            run("systemctl", "start", unit)
        print("restored prior target binary, config, unit and running state")

    def install(self, release_archive=None):
        if self.active():
            raise schema.ConfigError("step 1 refuses an active instance; run step 0 first")
        import pwd
        import grp
        packages = ("ca-certificates", "curl", "openssl", "python3", "python3-yaml", "tar", "logrotate")
        missing = [p for p in packages if run("dpkg-query", "-W", "-f=${Status}", p, check=False, capture=True).stdout.strip() != "install ok installed"]
        if missing:
            run("apt-get", "update")
            options = ["install", "--no-upgrade", "--no-install-recommends"]
            simulation = run("apt-get", "--simulate", *options, *missing, capture=True).stdout
            import re
            if any(re.match(r"^Inst \S+ \[", line) or line.startswith("Remv ") for line in simulation.splitlines()):
                raise schema.ConfigError("dependency installation would upgrade/remove existing shared packages; install dependencies explicitly")
            run("apt-get", *options, "-y", *missing)
        # Existing packages are neither upgraded nor later treated as exclusively owned.
        self.manifest["packages_requested"] = missing
        self.save()
        if run("uname", "-m", capture=True).stdout.strip() != self.data["install"]["architecture"]:
            raise schema.ConfigError("host architecture differs from configured binary")
        if self.data["install"]["libc"] != "gnu" or not any(x in run("ldd", "--version", capture=True).stdout.lower() for x in ("glibc", "gnu libc")):
            raise schema.ConfigError("GNU libc host and binary are required")
        # Intent persisted before account creation permits cleanup after interruption.
        try:
            group = grp.getgrnam(self.p["group"])
        except KeyError:
            self.manifest["gid"] = None
        else:
            if self.manifest.get("gid") is None and self.manifest.get("creating_group"):
                self.manifest["gid"] = group.gr_gid
                self.save()
        if self.manifest.get("gid") is None:
            self.manifest["creating_group"] = True
            self.save()
            run("groupadd", "--system", self.p["group"])
            self.manifest["gid"] = grp.getgrnam(self.p["group"]).gr_gid
            self.save()
        try:
            user = pwd.getpwnam(self.p["user"])
        except KeyError:
            self.manifest["uid"] = None
        else:
            if self.manifest.get("uid") is None and self.manifest.get("creating_user"):
                if user.pw_gid != self.manifest["gid"] or user.pw_dir != self.p["work_dir"] or user.pw_shell != "/usr/sbin/nologin":
                    raise schema.ConfigError("interrupted user creation does not match expected identity")
                self.manifest["uid"] = user.pw_uid
                self.save()
        if self.manifest.get("uid") is None:
            self.manifest["creating_user"] = True
            self.save()
            run("useradd", "--system", "--gid", self.p["group"], "--home-dir", self.p["work_dir"], "--shell", "/usr/sbin/nologin", self.p["user"])
            self.manifest["uid"] = pwd.getpwnam(self.p["user"]).pw_uid
            self.save()
        for key in ("config_dir", "work_dir", "binary_dir", "logs"):
            path = self.directory(key)
            os.chown(path, self.manifest["uid"] if key == "work_dir" else 0, self.manifest["gid"])
        Path(self.p["binary_path"]).parent.mkdir(exist_ok=True, mode=0o755)
        staging = self.state / "staging"
        guard_path(staging, private=True)
        staging.mkdir(exist_ok=True, mode=0o700)
        version = self.data["install"]["version"]
        asset = f"telemt-{self.data['install']['architecture']}-linux-gnu.tar.gz"
        with tempfile.TemporaryDirectory(prefix="download-", dir=staging) as tmp:
            archive = Path(tmp) / asset
            url = f"https://github.com/telemt/telemt/releases/download/{version}/{asset}"
            if release_archive is None:
                run("curl", "--fail", "--location", "--retry", "3", "--connect-timeout", "15", "--output", archive, url)
            else:
                shutil.copyfile(release_archive, archive)
            if hashlib.sha256(archive.read_bytes()).hexdigest() != self.data["install"]["sha256"].lower():
                raise schema.ConfigError("official binary SHA-256 mismatch")
            with tarfile.open(archive) as tar:
                members = [x for x in tar.getmembers() if Path(x.name).name == "telemt" and x.isfile()]
                if len(members) != 1:
                    raise schema.ConfigError("archive does not contain exactly one regular telemt binary")
                binary = Path(tmp) / "telemt"
                with tar.extractfile(members[0]) as src, binary.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                binary.chmod(0o755)
            if version not in run(binary, "--version", capture=True).stdout:
                raise schema.ConfigError("binary version differs from pin")
            target = guard_path(self.p["binary_path"], private=True)
            shutil.copy2(binary, target)
            os.chown(target, 0, 0)
        self.manifest["status"] = "installed"
        self.save()

    def configure(self):
        if self.active():
            raise schema.ConfigError("step 2 refuses an active instance; run step 0 first")
        if self.manifest.get("uid") is None or not Path(self.p["binary_path"]).is_file():
            raise schema.ConfigError("run step 1 before step 2")
        old_secrets = schema.existing_secrets(Path(self.p["config_path"]))
        if Path(self.p["config_path"]).exists():
            import tomllib
            old = tomllib.loads(Path(self.p["config_path"]).read_text(encoding="utf-8"))
            if old.get("censorship", {}).get("tls_domain", "") != self.data["proxy"].get("tls_domain", ""):
                print("WARNING: tls_domain changed; existing Fake-TLS links become invalid")
        write_private(self.p["config_path"], schema.render(self.data, old_secrets), 0o640)
        os.chown(self.p["config_path"], 0, self.manifest["gid"])
        self.claim("runtime")
        self.owned_file("unit", unit_text(self.p, self.id))
        self.owned_file("logrotate", f"# telemt-setup instance {self.id}\n{self.p['logs']}/*.log {{\n  daily\n  rotate 7\n  missingok\n  notifempty\n  compress\n  copytruncate\n  su root {self.p['group']}\n}}\n")
        if self.data["install"]["manage_ufw"]:
            self.configure_ufw()
        run("systemctl", "daemon-reload")
        self.manifest["status"] = "configured"
        self.save()

    def start(self):
        self.verify()
        if not Path(self.p["unit"]).exists():
            raise schema.ConfigError("instance is not configured")
        self.check_ports()
        run("systemctl", "enable", "--now", self.p["service_name"] + ".service")
        run(sys.executable, Path(self.p["code"]) / "tools/healthcheck.py", "--scope", "vm", "--config", self.config)
        self.manifest["status"] = "ready"
        self.manifest.pop("rollback", None)
        self.save()

    def cleanup(self, args):
        self.verify()
        if args.purge_setup:
            self.remove_source(purge_logs=args.purge_logs, apply=False)
        if Path(self.p["logs"]).exists():
            for directory, dirs, files in os.walk(self.p["logs"], followlinks=False):
                for name in files:
                    target = Path(directory) / name
                    if not target.is_symlink():
                        guard_path(target, private=True)
        print(f"cleanup {self.id}: owned unit/binary/config/data; logs purge={args.purge_logs}, keep user={args.keep_user}, shared purge={args.purge_shared_components}")
        for key in ("unit", "binary_dir", "config_dir", "work_dir", "runtime", "logrotate"):
            keep = (key == "config_dir" and args.keep_config) or (key == "work_dir" and args.keep_data)
            print(f"  {'keep' if keep else 'remove owned'}: {self.p[key]}")
        print(f"  {'remove' if args.purge_logs else 'keep'} logs: {self.p['logs']}")
        print(f"  keep ownership and cleanup controller: {self.state}, {self.p['code']}")
        if args.purge_setup:
            print(f"  purge verified extraction if no protected logs: {self.manifest['source']}")
        if not args.yes:
            print("dry run; use --yes to apply")
            return
        unit = self.p["service_name"] + ".service"
        if "unit" in self.manifest["claims"] and Path(self.p["unit"]).exists():
            run("systemctl", "disable", "--now", unit)
            if self.active():
                raise schema.ConfigError("service is still active; cleanup aborted")
        keys = ["unit", "logrotate", "binary_dir", "runtime"]
        if not args.keep_config:
            keys.append("config_dir")
        if not args.keep_data:
            keys.append("work_dir")
        if args.purge_logs:
            keys.append("logs")
        for key in keys:
            if key not in self.manifest["claims"]:
                continue
            path = Path(self.p[key])
            if path.is_dir():
                remove_tree(path)
            elif path.exists():
                guard_path(path, private=True).unlink()
        run("systemctl", "daemon-reload")
        if not args.keep_backups:
            remove_tree(self.state / "backups")
        remove_tree(self.state / "staging")
        if not args.keep_config and self.config.exists():
            guard_path(self.config, private=True).unlink()
        if args.purge_ufw or args.purge_shared_components:
            self.purge_ufw()
        if not args.keep_user or args.purge_user:
            self.purge_accounts()
        if args.purge_setup:
            self.remove_source(purge_logs=args.purge_logs)
        if args.purge_shared_components:
            print("shared OS packages retained: exclusive ownership/absence of other consumers cannot be proved; no journal vacuum")
        self.manifest["status"] = "removed"
        self.manifest.pop("rollback", None)
        self.save()  # Deliberate tombstone for later explicit purge without YAML.
        print("cleanup complete; ownership manifest retained for repeated cleanup")

    def ufw_rules(self):
        import shlex
        result = []
        for line in run("ufw", "show", "added", capture=True).stdout.splitlines():
            if line.startswith("ufw "):
                result.append(shlex.split(line))
        return result

    def configure_ufw(self):
        import ipaddress
        if ipaddress.ip_address(self.data["server"]["listen_ip"]).is_loopback:
            print("loopback listener: no public UFW rule needed")
            return
        port = str(self.data["server"]["port"]) + "/tcp"
        comment = "telemt-setup:" + self.id
        expected = ["ufw", "allow", port, "comment", comment]
        rules = self.ufw_rules()
        if expected in rules:
            if self.manifest.get("ufw_rule") != expected:
                raise schema.ConfigError("matching UFW marker exists without ownership")
            return
        # A preexisting rule is shared; never claim or delete it.
        if any(port in rule for rule in rules):
            print("preexisting UFW rule retained without ownership")
            return
        old = self.manifest.get("ufw_rule")
        if old and old != expected:
            raise schema.ConfigError("purge the old owned UFW rule explicitly before changing proxy port")
        self.manifest["ufw_rule"] = expected
        self.save()
        run(*expected)
        if expected not in self.ufw_rules():
            self.manifest.pop("ufw_rule", None)
            self.save()
            raise schema.ConfigError("UFW did not record the expected owned rule")

    def purge_ufw(self):
        rule = self.manifest.get("ufw_rule")
        if not rule:
            return
        expected_comment = "telemt-setup:" + self.id
        if (not isinstance(rule, list) or len(rule) != 5 or rule[:2] != ["ufw", "allow"]
                or rule[3:] != ["comment", expected_comment]
                or not __import__("re").fullmatch(r"[0-9]{1,5}/tcp", rule[2])):
            raise schema.ConfigError("invalid recorded UFW rule")
        port = int(rule[2].split("/")[0])
        for file in self.state.parent.glob("*/manifest.json"):
            if file.parent.name != self.id:
                other = read_manifest(file.parent.name)
                if other and other.get("status") != "removed" and port in other.get("ports", []):
                    print("UFW rule retained: another instance uses this port")
                    return
        rules = self.ufw_rules()
        if rule not in rules:
            print("UFW rule absent or changed; no foreign rule removed")
            return
        if sum(rule[2] in item for item in rules) != 1:
            print("UFW rule retained: ambiguous shared port rules")
            return
        run("ufw", "--force", "delete", *rule[1:])
        self.manifest.pop("ufw_rule", None)
        self.save()

    def account_consumers(self):
        import re
        user_values = {self.p["user"], str(self.manifest.get("uid"))}
        group_values = {self.p["group"], str(self.manifest.get("gid"))}
        for root in (Path("/etc/systemd/system"), Path("/usr/lib/systemd/system")):
            for file in root.rglob("*"):
                if file.suffix not in {".service", ".conf"} or not file.is_file() or file == Path(self.p["unit"]):
                    continue
                for line in file.read_text(encoding="utf-8", errors="replace").splitlines():
                    match = re.fullmatch(r"\s*(User|Group|SupplementaryGroups)\s*=\s*(.*?)\s*", line)
                    if match and set(match[2].split()).intersection(user_values if match[1] == "User" else group_values):
                        return True
        return False

    def purge_accounts(self):
        import pwd
        import grp
        # Stopped foreign units are consumers too; pgrep alone is insufficient.
        if self.account_consumers():
            print("service user/group retained: another systemd unit consumes the identity")
            return
        # Only exact accounts created by this manifest, never preexisting identities.
        if self.manifest.get("uid") is not None:
            try:
                pwd.getpwnam(self.p["user"])
            except KeyError:
                pass
            else:
                if run("pgrep", "-u", str(self.manifest["uid"]), check=False, capture=True).returncode == 0:
                    raise schema.ConfigError("service user still has processes; retain account")
                # Keep identity while retained data/config/logs need it.
                if any(Path(self.p[k]).exists() for k in ("work_dir", "config_dir")):
                    print("service user/group retained while owned config/data remain")
                    return
                # Preserved log files are root-owned; remove their service-group dependency.
                if Path(self.p["logs"]).exists():
                    for directory, dirs, files in os.walk(self.p["logs"], followlinks=False):
                        os.chown(directory, 0, 0)
                        for name in files:
                            target = Path(directory) / name
                            if not target.is_symlink():
                                os.chown(target, 0, 0)
                run("userdel", self.p["user"])
        if self.manifest.get("gid") is not None:
            try:
                group = grp.getgrnam(self.p["group"])
            except KeyError:
                return
            if group.gr_mem or any(x.pw_gid == group.gr_gid for x in pwd.getpwall()):
                print("service group retained: other accounts use it")
                return
            run("groupdel", self.p["group"])


def unit_text(p, instance_id):
    return f'''# telemt-setup instance {instance_id}
[Unit]
Description=Telemt MTProto Proxy ({instance_id})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={p['user']}
Group={p['group']}
WorkingDirectory={p['work_dir']}
ExecStart={p['binary_path']} {p['config_path']}
RuntimeDirectory=telemt-{instance_id}
RuntimeDirectoryMode=0750
StandardOutput=append:{p['logs']}/telemt.log
StandardError=append:{p['logs']}/telemt.log
Restart=on-failure
RestartSec=5
LimitNOFILE=65536
UMask=0027
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths={p['work_dir']} {p['runtime']}

[Install]
WantedBy=multi-user.target
'''


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("command", nargs="?", default="all", choices=[*sorted(MUTATING), "status", "healthcheck", "links", "cleanup"])
    result.add_argument("--instance")
    result.add_argument("--config", type=Path)
    result.add_argument("--update-existing", action="store_true")
    result.add_argument("--release-archive", type=Path, help="offline official tar.gz; pinned SHA-256 remains mandatory")
    for flag in ("yes", "keep-config", "keep-data", "keep-backups", "keep-user", "purge-user", "purge-setup", "purge-logs", "purge-shared-components", "purge-ufw"):
        result.add_argument("--" + flag, action="store_true")
    return result


def select(args):
    data = None
    config = args.config
    if config is None and not args.instance:
        config = ROOT / "config.yaml"
    if config is not None:
        data = schema.validate(schema.load_yaml(config), config)
        if "instance" not in data:
            raise schema.ConfigError("instance.id is required; old YAML requires explicit --legacy")
        instance_id = data["instance"]["id"]
        if args.instance and args.instance != instance_id:
            raise schema.ConfigError("--instance conflicts with config instance.id")
    elif args.instance:
        instance_id = args.instance
    else:
        raise schema.ConfigError("select an instance with --instance or --config")
    return instance_id, data


def main(argv=None):
    args = parser().parse_args(argv)
    instance = None
    rollback_allowed = False
    try:
        instance_id, data = select(args)
        instance = Instance(instance_id, data)
        if os.name != "posix" or os.geteuid() != 0:
            raise schema.ConfigError("lifecycle commands require Linux root; config validation and e2e support Windows")
        if args.command == "cleanup":
            instance.cleanup(args)
            return 0
        if args.command in {"stop", "status"}:
            instance.verify()
            return run("systemctl", args.command, *( ["--no-pager"] if args.command == "status" else [] ), instance.p["service_name"] + ".service", check=False).returncode
        if instance.data is None:
            guard_path(instance.config, private=True)
            instance.data = schema.validate(schema.load_yaml(instance.config), instance.config)
        if args.command in MUTATING:
            instance.register(args.update_existing)
            instance.verify()
        else:
            instance.verify()
        if args.command in {"all", "update", "reconfigure", "0"}:
            instance.backup(stop=True)
            rollback_allowed = True
        if args.command in {"all", "update", "reconfigure", "0", "1", "2", "3"}:
            instance.persist_source()
            rollback_allowed = True
        if args.command in {"all", "update", "1"}:
            instance.install(args.release_archive)
        if args.command in {"all", "update", "reconfigure", "2"}:
            instance.configure()
        if args.command in {"all", "update", "reconfigure", "3", "start"}:
            instance.start()
        if args.command == "stop":
            run("systemctl", "stop", instance.p["service_name"] + ".service")
        if args.command == "status":
            return run("systemctl", "status", "--no-pager", instance.p["service_name"] + ".service", check=False).returncode
        if args.command == "backup":
            instance.backup()
        if args.command in {"healthcheck", "links"}:
            return run(sys.executable, Path(instance.p["code"]) / "tools/healthcheck.py", "--scope", "vm" if args.command == "healthcheck" else "links", "--config", instance.config, check=False).returncode
        return 0
    except (schema.ConfigError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"telemt instance error: {exc}", file=sys.stderr)
        if rollback_allowed and instance and instance.manifest and args.command in {"all", "update", "reconfigure", "1", "2", "3"}:
            try:
                instance.rollback()
            except Exception as rollback_error:
                print(f"rollback failed; ownership retained for cleanup: {rollback_error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
