#!/usr/bin/env python3
"""PyraCMS code-runner gateway.

The only component that talks to the host Docker daemon. The public backend
never gets docker.sock; its `docker` CLI is a shim (docker-shim.sh) that
forwards `docker run ... IMAGE CODE` here as

    POST /run?image=<image>   body = code (text)
    -> 200, body = combined output, header X-Exit-Code

so a compromised backend can do no more than any site user already can: run
code in one of the allowlisted sandbox images, under fixed limits.

Differences from calling `docker run` directly (DockerExecutionService.cpp):
  * the container itself is killed at the deadline -- `timeout docker run`
    only kills the CLI and leaves the container running;
  * one sandbox at a time (the host has a single CPU), with a short queue;
  * all capabilities dropped.
"""
import os
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREFIX = os.environ.get("RUNNER_IMAGE_PREFIX", "ghcr.io/johndoe6345789/pyracms-runner-")
LANGS = ("python", "node", "cpp", "rust", "go", "java", "ruby")
ALLOWED = {PREFIX + lang for lang in LANGS}

RUN_TIMEOUT = int(os.environ.get("RUN_TIMEOUT", "25"))  # backend gives up at 30s
QUEUE_WAIT = int(os.environ.get("QUEUE_WAIT", "4"))
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "1"))
MAX_CODE = 100 * 1024  # a single argv string is capped at 128KiB by Linux
MAX_OUTPUT = 64 * 1024
LABEL = "pyracms-runner=1"

slots = threading.BoundedSemaphore(MAX_CONCURRENT)


def docker(*args, timeout=15):
    return subprocess.run(["docker", *args], capture_output=True, timeout=timeout)


def run_sandbox(image, code):
    name = "pyracms-run-" + uuid.uuid4().hex[:12]
    cmd = [
        "docker", "run", "--rm", "--name", name, "--label", LABEL,
        "--network=none",
        "--memory=512m", "--memory-swap=512m",
        "--cpus=1", "--pids-limit=256",
        "--read-only", "--tmpfs", "/tmp:rw,exec,nosuid,size=256m",
        "--security-opt=no-new-privileges", "--cap-drop=ALL",
        image, code,
    ]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=RUN_TIMEOUT)
        out, code_ = p.stdout, p.returncode
    except subprocess.TimeoutExpired as e:
        docker("rm", "-f", name)
        out = (e.stdout or b"") + f"\nExecution timed out ({RUN_TIMEOUT} second limit)".encode()
        # 137 (killed), not 124: the backend appends its own hard-coded
        # "timed out (10 second limit)" line whenever it sees 124.
        code_ = 137
    if len(out) > MAX_OUTPUT:
        out = out[:MAX_OUTPUT] + b"\n... output truncated (64KB limit)"
    return code_, out


class Handler(BaseHTTPRequestHandler):
    server_version = "pyracms-runner-gateway"

    def reply(self, status, body, exit_code=None):
        body = body if isinstance(body, bytes) else body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if exit_code is not None:
            self.send_header("X-Exit-Code", str(exit_code))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, "ok")
        self.reply(404, "not found")

    def do_POST(self):
        path, _, query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        image = params.get("image", "")
        if path != "/run" or image not in ALLOWED:
            return self.reply(400, "unsupported runner image", 1)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_CODE:
            return self.reply(413, "code too large (100KB limit)", 1)
        code = self.rfile.read(length).decode("utf-8", "replace")
        if "\x00" in code:
            return self.reply(400, "code contains NUL bytes", 1)
        if not slots.acquire(timeout=QUEUE_WAIT):
            return self.reply(503, "Code runner is busy, try again shortly.", 1)
        try:
            exit_code, out = run_sandbox(image, code)
        finally:
            slots.release()
        self.reply(200, out, exit_code)

    def log_message(self, fmt, *args):  # no request bodies in logs
        print("%s %s" % (self.address_string(), fmt % args), flush=True)


def reap_leftovers():
    """Remove sandboxes orphaned by a gateway restart mid-run."""
    ids = docker("ps", "-aq", "--filter", "label=" + LABEL).stdout.split()
    if ids:
        docker("rm", "-f", *[i.decode() for i in ids])
        print(f"removed {len(ids)} leftover sandbox container(s)", flush=True)


if __name__ == "__main__":
    reap_leftovers()
    print(f"runner gateway on :8080, {len(ALLOWED)} images, "
          f"timeout={RUN_TIMEOUT}s, concurrent={MAX_CONCURRENT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
