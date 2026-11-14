"""Generate independent, opt-in dev profiles without reading production secrets."""
import argparse
import copy
import os
from pathlib import Path
import yaml
import config
import addressing


def profiles(ipv4, ipv6, domain, proxy_base=21110, api_base=29100, metrics_base=29200,
             socks_base=11080, prefix="dev140"):
    if addressing.family(addressing.host(ipv4)) != "ipv4" or addressing.family(addressing.host(ipv6)) != "ipv6":
        raise ValueError("public IP families do not match")
    domain = addressing.host(domain)
    try:
        addressing.family(domain)
    except ValueError:
        pass
    else:
        raise ValueError("domain must be DNS")
    base = config.load_yaml(Path(__file__).resolve().parents[1] / "config.dualstack.example.yaml")
    scenarios = [("v4", ["0.0.0.0"], False, None), ("v6", ["::"], False, None),
                 ("dual", ["0.0.0.0", "::"], False, None), ("me", ["0.0.0.0", "::"], True, None)]
    scenarios += [("via-" + name, ["0.0.0.0", "::"], False, socks_base + i)
                  for i, name in enumerate(("direct", "socks", "http", "https"))]
    ports = [base + i for base in (proxy_base, api_base, metrics_base) for i in range(len(scenarios))]
    if len(ports) != len(set(ports)) or any(not 1024 <= port <= 65535 for port in ports):
        raise ValueError("dev port ranges must be distinct and unprivileged")
    result = []
    for index, (name, binds, me, socks_port) in enumerate(scenarios):
        data = copy.deepcopy(base)
        data["instance"]["id"] = prefix + "-" + name
        data["install"]["manage_ufw"] = True
        port = proxy_base + index
        data["server"].update(listen_ips=binds, port=port, api_listen=f"127.0.0.1:{api_base+index}", metrics_listen=f"127.0.0.1:{metrics_base+index}")
        endpoints = []
        if len(binds) == 2:
            endpoints.append(dict(id="dns", host=domain, port=port, families=["ipv4", "ipv6"]))
        if "0.0.0.0" in binds:
            endpoints.append(dict(id="ipv4", host=ipv4, port=port, families=["ipv4"]))
        if "::" in binds:
            endpoints.append(dict(id="ipv6", host=ipv6, port=port, families=["ipv6"]))
        data["links"] = dict(primary=endpoints[0]["id"], endpoints=endpoints)
        data["proxy"]["use_middle_proxy"] = me
        if socks_port:
            data["upstreams"] = [dict(type="socks5", address=f"127.0.0.1:{socks_port}", enabled=True, weight=1)]
        config.validate(data)
        result.append(data)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-ipv4", required=True)
    parser.add_argument("--public-ipv6", required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--proxy-base", type=int, default=21110)
    parser.add_argument("--api-base", type=int, default=29100)
    parser.add_argument("--metrics-base", type=int, default=29200)
    parser.add_argument("--socks-base", type=int, default=11080)
    parser.add_argument("--prefix", default="dev140")
    args = parser.parse_args()
    data = profiles(args.public_ipv4, args.public_ipv6, args.domain, args.proxy_base,
                    args.api_base, args.metrics_base, args.socks_base, args.prefix)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for item in data:
        file = args.output / ("config." + item["instance"]["id"] + ".yaml")
        with os.fdopen(os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as stream:
            yaml.safe_dump(item, stream, sort_keys=False)
        print(f"Created {file.name}: TCP {item['server']['port']}; secrets generated only during setup")


if __name__ == "__main__":
    main()
