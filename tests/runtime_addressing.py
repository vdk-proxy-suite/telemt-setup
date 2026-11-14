"""Disposable Linux runtime verification; no systemd, UFW or production mutations.

Run: python3 tests/runtime_addressing.py /absolute/path/to/pinned/telemt
This verifies sockets/API/links/TLS camouflage, not Telegram message delivery.
"""
import contextlib
import json
from pathlib import Path
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import config
import healthcheck
import links


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run(binary):
    pinned = config.load_yaml(ROOT / "config.example.yaml")["install"]["version"]
    assert pinned in subprocess.check_output([binary, "--version"], text=True)
    with tempfile.TemporaryDirectory(prefix="telemt-addressing-") as temporary:
        root = Path(temporary)
        key, cert = root / "key.pem", root / "cert.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                        "-out", str(cert), "-days", "1", "-subj", "/CN=localhost"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(cert, key)
        front = socket.socket(); front.bind(("127.0.0.1", 443)); front.listen(); front.settimeout(.25)
        stop = threading.Event()
        socks = socket.socket(); socks.bind(("127.0.0.1", 0)); socks.listen(); socks.settimeout(.25)
        socks_targets = []
        front_hits = []
        def receive(sock, size):
            result = b""
            while len(result) < size:
                chunk = sock.recv(size-len(result))
                if not chunk: raise EOFError()
                result += chunk
            return result
        def serve_socks():
            while not stop.is_set():
                try: client, _ = socks.accept()
                except socket.timeout: continue
                except OSError: break
                with client:
                    try:
                        client.settimeout(2)
                        hello = receive(client, 2); receive(client, hello[1]); client.sendall(b"\x05\x00")
                        header = receive(client, 4)
                        if header[3] == 1: host = socket.inet_ntop(socket.AF_INET, receive(client, 4))
                        elif header[3] == 4: host = socket.inet_ntop(socket.AF_INET6, receive(client, 16))
                        else: host = receive(client, receive(client, 1)[0]).decode()
                        port = struct.unpack("!H", receive(client, 2))[0]
                        socks_targets.append((host, port))
                        client.sendall(b"\x05\x04\x00\x01" + b"\x00" * 6)
                    except (OSError, EOFError): pass
        threading.Thread(target=serve_socks, daemon=True).start()
        def serve():
            while not stop.is_set():
                try: client, _ = front.accept()
                except socket.timeout: continue
                except OSError: break
                front_hits.append(time.monotonic())
                try:
                    client.settimeout(2)
                    with ctx.wrap_socket(client, server_side=True) as tls:
                        tls.recv(4096)
                except OSError: client.close()
        threading.Thread(target=serve, daemon=True).start()
        try:
            for name, binds, api_host in (("dual", ["0.0.0.0", "::"], "127.0.0.1"),
                                          ("v6", ["::1"], "::1"),
                                          ("v6-egress", ["127.0.0.1", "::1"], "127.0.0.1"),
                                          ("socks-fail-closed", ["127.0.0.1", "::1"], "127.0.0.1")):
                work = root / name; work.mkdir()
                data = config.load_yaml(ROOT / "config.example.yaml")
                data["schema_version"] = 2
                data["server"].pop("listen_ip")
                proxy_port, api_port, metrics_port = free_port(), free_port(), free_port()
                data["server"].update(listen_ips=binds, port=proxy_port,
                    api_listen=healthcheck.addressing.authority(api_host, api_port),
                    metrics_listen=healthcheck.addressing.authority(api_host, metrics_port))
                items = [dict(id="v6", host="::1", port=proxy_port, families=["ipv6"])]
                if name == "dual":
                    items += [dict(id="v4", host="127.0.0.1", port=proxy_port, families=["ipv4"]),
                              dict(id="dns", host="localhost", port=proxy_port, families=["ipv4", "ipv6"])]
                data["links"] = dict(primary=items[0]["id"], endpoints=items)
                data["proxy"].update(use_middle_proxy=False, tls_domain="localhost", tls_emulation=False)
                hits_before = len(front_hits)
                if name == "v6-egress":
                    data["outbound"] = dict(families=["ipv6"], prefer="ipv6")
                    data["proxy"]["tls_emulation"] = True
                if name == "socks-fail-closed":
                    data["upstreams"] = [dict(type="socks5", address=f"127.0.0.1:{socks.getsockname()[1]}")]
                    data["proxy"]["tls_emulation"] = True
                config.validate(data)
                toml = config.render(data, {})
                target = work / "telemt.toml"; target.write_text(toml); target.chmod(0o600)
                with (work / "runtime.log").open("w") as log:
                    process = subprocess.Popen([binary, str(target)], cwd=work, stdout=log, stderr=subprocess.STDOUT)
                    try:
                        deadline = time.monotonic() + 45
                        while True:
                            if process.poll() is not None:
                                raise RuntimeError("Telemt exited: " + (work / "runtime.log").read_text()[-2000:])
                            try:
                                payload = healthcheck.api_users(data)
                                break
                            except OSError:
                                if time.monotonic() >= deadline: raise
                                time.sleep(.25)
                        catalogue = links.catalogue(data, payload)
                        assert len(catalogue["users"][0]["links"]) == len(items)
                        healthcheck.verify_listener_owners(data, str(process.pid))
                        if name in {"socks-fail-closed", "v6-egress"}:
                            assert any(path.is_file() for path in (work / "tlsfront").rglob("*")), "TLS metadata was not cached"
                        report = healthcheck.check_e2e_matrix(data, timeout=3)
                        assert report["summary"]["failed"] == 0, report["summary"]
                        metrics = healthcheck.http_get(f"http://{healthcheck.addressing.authority(api_host, metrics_port)}/metrics", 3)
                        assert b"# HELP" in metrics
                        if name == "socks-fail-closed":
                            deadline = time.monotonic() + 10
                            while not socks_targets and time.monotonic() < deadline: time.sleep(.1)
                            assert socks_targets, "Telemt made no SOCKS DC observations"
                            assert all(":" not in host and host not in {"localhost", "127.0.0.1"} for host, _ in socks_targets)
                            assert len(front_hits) > hits_before, "TLS-front did not use direct route"
                            quality = json.loads(healthcheck.http_get(f"http://127.0.0.1:{api_port}/v1/runtime/upstream-quality", 3))
                            routes = quality["data"]["upstreams"]
                            assert any(row["route_kind"] == "socks5" and not row.get("scopes", "") for row in routes)
                            assert not any(row["route_kind"] == "direct" and not row.get("scopes", "") for row in routes)
                            try: healthcheck.api_telegram_upstream_ready(data)
                            except RuntimeError: pass
                            else: raise AssertionError("failed SOCKS was accepted as ready")
                        if name == "v6":
                            try:
                                with socket.create_connection(("127.0.0.1", proxy_port), timeout=.5): pass
                            except OSError: pass
                            else: raise AssertionError("IPv6-only listener unexpectedly accepts IPv4")
                        print("RUNTIME PASS", name, "API/metrics/native-links/TLS-smoke; Telegram delivery not tested", flush=True)
                    finally:
                        process.terminate()
                        try: process.wait(timeout=8)
                        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
        finally:
            stop.set(); front.close(); socks.close()


if __name__ == "__main__":
    # Local API must not accidentally use an environment HTTP proxy.
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
    run(str(Path(sys.argv[1]).resolve()))
