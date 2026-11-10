#!/usr/bin/env python3
"""Serve the fabric view and its recorded walkthrough on localhost."""

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from queue import Empty, Queue
import re
import subprocess
from threading import Lock, Thread
from time import monotonic
from uuid import uuid4

import measure as lab

WEB = lab.ROOT / "topology"
RECORDING = lab.ROOT / "evidence" / "walkthrough" / "observations.json"
FILES = {
    "/": (WEB / "index.html", "text/html"),
    "/fabric.css": (WEB / "fabric.css", "text/css"),
    "/fabric.js": (WEB / "fabric.js", "text/javascript"),
    "/recording.json": (RECORDING, "application/json"),
}
PROBE = Lock()
ECHO = re.compile(r"ICMP echo request, id \d+, seq (\d+)")


def live_state():
    links = {}
    for leaf, prefix in lab.PREFIX.items():
        output = lab.run(leaf, "vtysh", "-c", "show ip ospf neighbor")
        neighbours = []
        for row in output.splitlines():
            fields = row.split()
            if len(fields) >= 7 and fields[2].startswith("Full/"):
                neighbours.append([fields[0], fields[2], fields[6].split(":")[0]])
        interfaces = json.loads(lab.run(leaf, "ip", "-j", "link", "show"))
        links[leaf] = {
            "neighbours": neighbours,
            "route": lab.run(leaf, "ip", "route", "show", "exact", prefix),
            "down": [i["ifname"] for i in interfaces
                     if i["ifname"] in lab.UPLINKS and "UP" not in i["flags"]],
        }
    return {"phase": "live", "links": links,
            "observed_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def capture(emit):
    uplink = lab.chosen_uplink()
    leaf = lab.command("docker", "compose", "ps", "-q", "leaf1")
    name = f"dodc-ospf-probe-{uuid4().hex[:12]}"
    packet_filter = f"icmp[icmptype] = icmp-echo and src host {lab.SOURCE} and dst host {lab.DESTINATION}"
    capture_process = subprocess.Popen(
        ["docker", "run", "--rm", "--name", name, "--network", f"container:{leaf}",
         "dodc-ospf-host:1", "timeout", "15", "tcpdump", "-l", "-n", "-p", "-i", uplink, "-c", "6", packet_filter],
        cwd=lab.ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    ping = None
    lines = Queue()

    def read_capture():
        with capture_process.stdout as output:
            for line in output:
                lines.put(line)
        lines.put(None)

    reader = Thread(target=read_capture, daemon=True)
    reader.start()
    seen = set()
    try:
        deadline = monotonic() + 12
        while (line := lines.get(timeout=max(0, deadline - monotonic()))) is not None:
            if "listening on" in line:
                emit({"uplink": uplink})
                ping = subprocess.Popen(
                    lab.exec_args("host1", "ping", "-n", "-c", "6", "-i", "0.2", "-W", "1", lab.DESTINATION),
                    cwd=lab.ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                )
            if match := ECHO.search(line):
                sequence = int(match.group(1))
                if sequence not in seen:
                    seen.add(sequence)
                    emit({"sequence": sequence})
        if ping is None:
            raise RuntimeError("The capture did not start. Check the Docker lab and rebuild the host image.")
        output, _ = ping.communicate(timeout=3)
        summary = next((line for line in output.splitlines() if "packets transmitted" in line), "")
        emit({"summary": summary or "Ping did not return a summary.", "captured": len(seen)})
    except Empty as error:
        raise RuntimeError(f"Capture timed out after {len(seen)} requests.") from error
    finally:
        if ping is not None and ping.poll() is None:
            ping.kill()
            ping.communicate()
        if capture_process.poll() is None:
            capture_process.kill()
            capture_process.wait()
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=5)
        reader.join(timeout=1)


class Handler(BaseHTTPRequestHandler):
    def send_body(self, status, content_type, data):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status, data):
        self.send_body(status, "application/json", json.dumps(data).encode())

    def local_addresses(self):
        port = self.server.server_port
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def do_GET(self):
        if self.headers.get("Host") not in self.local_addresses():
            self.send_error(403)
            return
        if self.path in FILES:
            path, content_type = FILES[self.path]
            self.send_body(200, content_type, path.read_bytes())
        elif self.path == "/api/live":
            try:
                self.send_json(200, live_state())
            except (RuntimeError, OSError, subprocess.SubprocessError):
                self.send_json(503, {"error": "Start the lab with Docker Compose, then retry."})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/api/probe":
            self.send_error(404)
            return
        host = self.headers.get("Host")
        if host not in self.local_addresses() or self.headers.get("Origin") != f"http://{host}":
            self.send_error(403)
            return
        if not PROBE.acquire(blocking=False):
            self.send_error(409, "A capture is already running")
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

            def emit(data):
                self.wfile.write((json.dumps(data) + "\n").encode())
                self.wfile.flush()

            try:
                capture(emit)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (RuntimeError, OSError, subprocess.SubprocessError) as error:
                emit({"error": str(error) if isinstance(error, RuntimeError)
                      else "Could not capture traffic. Check that the lab is running."})
        finally:
            PROBE.release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18454)
    args = parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        print(f"Fabric view at http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
