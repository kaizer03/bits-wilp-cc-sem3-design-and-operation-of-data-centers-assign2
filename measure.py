#!/usr/bin/env python3
"""Measure route and ICMP recovery after one leaf uplink is disabled."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
from threading import Event, Thread
from time import monotonic, sleep

ROOT = Path(__file__).resolve().parent
PREFIX = {"leaf1": "10.76.102.0/24", "leaf2": "10.76.101.0/24"}
GATEWAYS = {
    "leaf1": {"to_s1": "10.76.0.3", "to_s2": "10.76.0.11"},
    "leaf2": {"to_s1": "10.76.0.19", "to_s2": "10.76.0.27"},
}
UPLINKS = GATEWAYS["leaf1"].keys()
SOURCE, DESTINATION = "10.76.101.10", "10.76.102.10"
INTERVAL, PROBES = 0.2, 42
REPLY = re.compile(r"^\[(\d+\.\d+)\].*bytes from .*icmp_seq=(\d+)")


def command(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True, timeout=12).strip()


def exec_args(node, *args):
    return ("docker", "compose", "exec", "-T", node, *args)


def run(node, *args):
    return command(*exec_args(node, *args))


def paths(leaf):
    routes = json.loads(run(leaf, "ip", "-j", "route", "show", "exact", PREFIX[leaf]))
    return {
        (hop["dev"], hop["gateway"])
        for route in routes if route.get("protocol") == "ospf"
        for hop in (route.get("nexthops") or [route])
        if hop.get("dev") and hop.get("gateway")
    }


def wait_paths(devices, started, timeout=12):
    expected = {
        leaf: {(device, gateways[device]) for device in devices}
        for leaf, gateways in GATEWAYS.items()
    }
    observed = {}
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        for leaf, hops in expected.items():
            if paths(leaf) == hops:
                observed.setdefault(leaf, round((monotonic() - started) * 1000, 1))
            else:
                observed.pop(leaf, None)
        if len(observed) == len(expected):
            return observed
        sleep(0.1)
    raise RuntimeError(f"Expected paths {expected}; observed {observed}")


def snapshot(title):
    lines = [title]
    for leaf in PREFIX:
        lines += [
            f"$ docker compose exec -T {leaf} vtysh -c 'show ip ospf neighbor'",
            run(leaf, "vtysh", "-c", "show ip ospf neighbor"),
            f"$ docker compose exec -T {leaf} vtysh -c 'show ip route {PREFIX[leaf]}'",
            run(leaf, "vtysh", "-c", f"show ip route {PREFIX[leaf]}"),
            f"$ docker compose exec -T {leaf} ip route show exact {PREFIX[leaf]}",
            run(leaf, "ip", "route", "show", "exact", PREFIX[leaf]),
            "",
        ]
    return "\n".join(lines)


def chosen_uplink():
    route = json.loads(run("leaf1", "ip", "-j", "route", "get", DESTINATION,
                           "from", SOURCE, "iif", "edge", "ipproto", "icmp"))
    dev = route[0]["dev"]
    if dev not in UPLINKS:
        raise RuntimeError(f"Unexpected baseline path: {dev}")
    return dev


def trial(number, failed, route_log, ping_log):
    if chosen_uplink() != failed:
        raise RuntimeError("The test flow changed its baseline uplink")
    other = UPLINKS - {failed}
    lines, replies = [], {}
    ready = Event()
    ping = exec_args("host1", "ping", "-n", "-D", "-O", "-i", str(INTERVAL),
                     "-W", "1", "-c", str(PROBES), DESTINATION)
    proc = subprocess.Popen(ping, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)

    def read_ping():
        for line in proc.stdout:
            lines.append(line)
            match = REPLY.search(line)
            if match:
                stamp, seq = match.groups()
                replies[int(seq)] = float(stamp)
                if all(i in replies for i in range(1, 7)):
                    ready.set()

    reader = Thread(target=read_ping)
    reader.start()
    fault_started = None
    try:
        if not ready.wait(4):
            raise RuntimeError("Six consecutive baseline replies were not received")
        fault_started = monotonic()
        run("leaf1", "ip", "link", "set", "dev", failed, "down")
        ack_ms = round((monotonic() - fault_started) * 1000, 1)
        down = wait_paths(other, fault_started)
        route_log.append(snapshot(f"Trial {number}: leaf1 {failed} down"))
        proc.wait(timeout=14)
        reader.join(timeout=3)
        if proc.returncode != 0 or not all(i in replies for i in range(PROBES-4, PROBES+1)):
            raise RuntimeError("Traffic did not recover before the link was restored")
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        reader.join(timeout=3)
        ping_log.append(f"Trial {number}\n$ {' '.join(ping)}\n" + "".join(lines))
        if fault_started is not None:
            restore_started = monotonic()
            run("leaf1", "ip", "link", "set", "dev", failed, "up")
            restored = wait_paths(UPLINKS, restore_started)
    route_log.append(snapshot(f"Trial {number}: both paths restored"))
    times = sorted(replies.values())
    return {
        "trial": number,
        "failed_uplink": failed,
        "sent": PROBES,
        "received": len(replies),
        "missing_sequences": sorted(set(range(1, PROBES+1)) - replies.keys()),
        "longest_reply_gap_ms": round(max(b-a for a, b in zip(times, times[1:])) * 1000, 1),
        "down_command_return_ms": ack_ms,
        "one_path_observed_ms": down,
        "two_paths_restored_ms": restored,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    default = Path("runs") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    parser.add_argument("--out", type=Path, default=default)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    wait_paths(UPLINKS, monotonic(), timeout=45)
    failed = chosen_uplink()
    routes = [snapshot("Baseline: both paths available")]
    baseline = run("host1", "ping", "-n", "-D", "-i", str(INTERVAL), "-c", "10", DESTINATION)
    pings = ["Baseline: 10 probes\n" + baseline + "\n"]
    try:
        results = [trial(i, failed, routes, pings) for i in range(1, 4)]
    finally:
        (args.out / "routes.txt").write_text("\n".join(routes) + "\n")
        (args.out / "ping.txt").write_text("\n".join(pings))
    record = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "docker": command("docker", "version", "--format", "{{.Server.Version}} ({{.Server.Arch}})"),
        "compose": command("docker", "compose", "version", "--short"),
        "frr": run("leaf1", "vtysh", "-c", "show version").splitlines()[0],
        "kernel": run("host1", "uname", "-r"),
        "frr_image": command("docker", "image", "inspect", "quay.io/frrouting/frr:10.5.5",
                             "--format", "{{index .RepoDigests 0}}"),
        "source": SOURCE, "destination": DESTINATION, "interval_ms": INTERVAL * 1000,
        "baseline_sent": 10,
        "baseline_received": sum(bool(REPLY.search(line)) for line in baseline.splitlines()),
        "trials": results,
    }
    if record["baseline_received"] != 10:
        raise RuntimeError("The no-fault baseline lost traffic")
    (args.out / "results.json").write_text(json.dumps(record, indent=2) + "\n")
    for item in results:
        print(f"Trial {item['trial']}: {item['received']}/{item['sent']} replies; "
              f"longest gap {item['longest_reply_gap_ms']} ms")
    print(args.out)


if __name__ == "__main__":
    main()
