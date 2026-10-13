#!/usr/bin/env python3
"""Appbox Syncthing release checks, run on the dedicated builder before push.

Checks the built image on a fresh install, a restart, and an upgrade from the
current catalogue image on the same volume, then requires the release tag to be
definitively absent from the registry.
"""
import atexit
import os
import re
import secrets
import subprocess
import sys
import time

EXPECTED = "repo.cylo.net/syncthing:2.1.6"
EXPECTED_VERSION = "v2.1.6"
PREVIOUS = "repo.cylo.net/syncthing@sha256:15028aab2d6557aaa24a4f5503116ce0e41f6989e3f686a6226fe4d289947455"
CALLBACK_IMAGE = "python:3.12-alpine"

assert len(sys.argv) == 2 and sys.argv[1] == EXPECTED, "Unexpected image reference"
RUN = "syncthing-release-gate-" + secrets.token_hex(6)
LABEL = "appbox.release-gate=" + RUN
USER = "releasegate"
password = secrets.token_urlsafe(24)
containers, volumes = [], []
network = None


def command(args, *, env=None, timeout=300):
    result = subprocess.run(args, text=True, capture_output=True, env=env, timeout=timeout)
    if result.returncode:
        details = result.stderr.replace(password, "[REDACTED]")[-4000:]
        raise RuntimeError("Command failed: " + " ".join(args[:3]) + " (exit " + str(result.returncode) + ")\n" + details)
    return result.stdout.strip()


def cleanup():
    for container in reversed(containers):
        owned = subprocess.run(["docker", "inspect", "--format", '{{index .Config.Labels "appbox.release-gate"}}', container], capture_output=True, text=True)
        if owned.returncode == 0 and owned.stdout.strip() == RUN:
            subprocess.run(["docker", "rm", "-fv", container], capture_output=True)
    for volume in volumes:
        owned = subprocess.run(["docker", "volume", "inspect", "--format", '{{index .Labels "appbox.release-gate"}}', volume], capture_output=True, text=True)
        if owned.returncode == 0 and owned.stdout.strip() == RUN:
            subprocess.run(["docker", "volume", "rm", volume], capture_output=True)
    if network:
        subprocess.run(["docker", "network", "rm", network], capture_output=True)


atexit.register(cleanup)


def diagnostics(container):
    result = subprocess.run(["docker", "logs", "--tail", "80", container], capture_output=True, text=True, timeout=30)
    output = (result.stdout + result.stderr).replace(password, "[REDACTED]")
    output = re.sub(r"(?im)^.*(?:password|apikey|api-key|csrf|token).*$", "[authentication detail omitted]", output)
    print(output[-8000:], flush=True)


def wait_healthy(container):
    # The image health check runs once a minute.
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        status = command(["docker", "inspect", "--format", "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}", container])
        if status == "running healthy":
            return
        if status.startswith(("exited", "dead")):
            diagnostics(container)
            raise RuntimeError(container + " stopped: " + status)
        time.sleep(3)
    diagnostics(container)
    raise RuntimeError(container + " did not become healthy")


def new_volume(suffix):
    volume = RUN + "-" + suffix
    command(["docker", "volume", "create", "--label", LABEL, volume])
    volumes.append(volume)
    command(["docker", "run", "--rm", "--label", LABEL, "--entrypoint", "chown", "-v", volume + ":/var/syncthing", EXPECTED, "1000:1000", "/var/syncthing"])
    return volume


def run_app(name, volume, instance):
    containers.append(name)
    command(["docker", "run", "-d", "--platform", "linux/amd64", "--name", name, "--label", LABEL, "--network", network,
             "-e", "USERNAME=" + USER, "-e", "PASSWORD", "-e", "INSTANCE_ID=" + instance,
             "-e", "TCP_PORT=22000", "-e", "UDP_PORT=21027", "-e", "APPBOX_CALLBACK_BASE=http://callback:8123",
             "-v", volume + ":/var/syncthing", EXPECTED], env={**os.environ, "PASSWORD": password})
    wait_healthy(name)


def inside(container, script):
    # USERNAME and PASSWORD come from the container's own environment.
    return command(["docker", "exec", container, "sh", "-ec", script])


def http_code(container, path, auth):
    creds = '-u "$USERNAME:$PASSWORD" ' if auth else ""
    return inside(container, "curl -s -o /dev/null -w '%{http_code}' " + creds + "http://127.0.0.1:8384" + path)


def check_app(container, instance):
    assert http_code(container, "/rest/system/version", False) in ("401", "403"), "GUI accepted an unauthenticated request"
    version = inside(container, 'curl -fsS -u "$USERNAME:$PASSWORD" http://127.0.0.1:8384/rest/system/version')
    assert '"version":"' + EXPECTED_VERSION + '"' in version.replace(" ", ""), "Unexpected running version"
    config = inside(container, "cat /var/syncthing/config/config.xml")
    assert "<listenAddress>tcp://0.0.0.0:22000</listenAddress>" in config, "Listen address not applied"
    assert "<localAnnouncePort>21027</localAnnouncePort>" in config, "Local announce port not applied"
    assert "<maxFolderConcurrency>2</maxFolderConcurrency>" in config, "Folder concurrency limit missing"
    assert "<maxConcurrentWrites>4</maxConcurrentWrites>" in config, "Concurrent write limit missing"
    assert inside(container, "stat -c %u:%g /var/syncthing/config/config.xml") == "1000:1000", "Config not owned by UID 1000"
    callbacks = command(["docker", "exec", RUN + "-callback", "cat", "/tmp/callbacks"])
    assert "/v1/apps/installed/" + instance in callbacks.splitlines(), "Install callback not received"


assert command(["docker", "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", EXPECTED]) == "linux/amd64"
binary_version = command(["docker", "run", "--rm", "--entrypoint", "/bin/syncthing", EXPECTED, "--version"])
assert binary_version.startswith("syncthing " + EXPECTED_VERSION + " "), "Binary reports " + binary_version
print("PASS image: linux/amd64, " + binary_version, flush=True)

command(["docker", "pull", "-q", PREVIOUS])
command(["docker", "pull", "-q", CALLBACK_IMAGE])
network = RUN + "-network"
command(["docker", "network", "create", "--internal", "--label", LABEL, network])
callback = RUN + "-callback"
containers.append(callback)
callback_code = """
from http.server import BaseHTTPRequestHandler,HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        with open('/tmp/callbacks','a') as f: f.write(self.path+'\\n')
        self.send_response(200);self.end_headers()
    def log_message(self,*args): pass
HTTPServer(('0.0.0.0',8123),Handler).serve_forever()
"""
command(["docker", "run", "-d", "--name", callback, "--label", LABEL, "--network", network,
         "--network-alias", "callback", "--entrypoint", "python3", CALLBACK_IMAGE, "-u", "-c", callback_code])

# Fresh install through the Appbox entrypoint.
fresh = RUN + "-fresh"
run_app(fresh, new_volume("fresh"), "9001")
check_app(fresh, "9001")
print("PASS fresh install: authenticated GUI, Appbox settings and install callback", flush=True)

command(["docker", "restart", fresh])
wait_healthy(fresh)
check_app(fresh, "9001")
print("PASS restart: login and settings preserved", flush=True)

# Upgrade: the current catalogue image creates a config, database and folder.
storage = new_volume("upgrade")
old = RUN + "-old"
containers.append(old)
command(["docker", "run", "--rm", "--label", LABEL, "--network", network, "--user", "1000:1000", "-e", "PASSWORD",
         "-v", storage + ":/var/syncthing", "--entrypoint", "sh", PREVIOUS, "-ec",
         '/bin/syncthing generate --home /var/syncthing/config --gui-user ' + USER + ' --gui-password "$PASSWORD" >/dev/null'],
        env={**os.environ, "PASSWORD": password})
command(["docker", "run", "-d", "--name", old, "--label", LABEL, "--network", network, "--user", "1000:1000",
         "-e", "USERNAME=" + USER, "-e", "PASSWORD", "-v", storage + ":/var/syncthing", "--entrypoint", "/bin/syncthing", PREVIOUS],
        env={**os.environ, "PASSWORD": password})
wait_healthy(old)
inside(old, "mkdir -p /var/syncthing/gate-data && echo appbox > /var/syncthing/gate-data/file.txt && "
            'curl -fsS -u "$USERNAME:$PASSWORD" -H "Content-Type: application/json" -X POST '
            "-d '{\"id\":\"gate-folder\",\"label\":\"Gate\",\"path\":\"/var/syncthing/gate-data\"}' "
            "http://127.0.0.1:8384/rest/config/folders >/dev/null")
deadline = time.monotonic() + 120
while '"localFiles":1' not in inside(old, 'curl -fsS -u "$USERNAME:$PASSWORD" "http://127.0.0.1:8384/rest/db/status?folder=gate-folder"').replace(" ", ""):
    assert time.monotonic() < deadline, "Baseline image did not scan the test folder"
    time.sleep(2)
command(["docker", "stop", old])
print("PASS baseline: current catalogue image created config, database and folder", flush=True)

upgraded = RUN + "-upgraded"
run_app(upgraded, storage, "9002")
check_app(upgraded, "9002")
folders = inside(upgraded, 'curl -fsS -u "$USERNAME:$PASSWORD" http://127.0.0.1:8384/rest/config/folders')
assert '"id":"gate-folder"' in folders.replace(" ", ""), "Upgrade lost the folder configuration"
deadline = time.monotonic() + 120
while '"localFiles":1' not in inside(upgraded, 'curl -fsS -u "$USERNAME:$PASSWORD" "http://127.0.0.1:8384/rest/db/status?folder=gate-folder"').replace(" ", ""):
    assert time.monotonic() < deadline, "Upgraded image did not report the existing folder contents"
    time.sleep(2)
print("PASS upgrade: login, folder and database carried over from the catalogue image", flush=True)

# Publication precondition: the tag must be definitively absent.
known = subprocess.run(["docker", "manifest", "inspect", PREVIOUS], capture_output=True, text=True, timeout=60)
if known.returncode:
    raise RuntimeError("Registry read of the current published digest failed; state is indeterminate")
result = subprocess.run(["docker", "manifest", "inspect", EXPECTED], capture_output=True, text=True, timeout=60)
if result.returncode == 0:
    raise RuntimeError("Release tag exists; refusing to replace it")
if result.stderr.strip() != "no such manifest: " + EXPECTED:
    raise RuntimeError("Registry did not return definitive MANIFEST_UNKNOWN; refusing publication")
print("PASS registry: current digest readable; new tag definitively absent", flush=True)
