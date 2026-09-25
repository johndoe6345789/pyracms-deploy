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
  * a few sandboxes at a time (MAX_CONCURRENT, each capped at 1 CPU and
    512 MB), with a short queue;
  * all capabilities dropped.
"""
import io
import os
import re
import subprocess
import tarfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREFIX = os.environ.get("RUNNER_IMAGE_PREFIX", "ghcr.io/johndoe6345789/pyracms-runner-")
LANGS = ("python", "node", "c", "cpp", "rust", "go", "java", "ruby")
ALLOWED = {PREFIX + lang for lang in LANGS}

RUN_TIMEOUT = int(os.environ.get("RUN_TIMEOUT", "27"))  # backend kills the shim at 30s
QUEUE_WAIT = int(os.environ.get("QUEUE_WAIT", "10"))
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "4"))  # 1 CPU + 512 MB each
MAX_CODE = 100 * 1024  # a single argv string is capped at 128KiB by Linux
MAX_INPUT = 8 * 1024 * 1024  # total bytes of a run's input files
MAX_INPUT_FILES = 64
MAX_TAR = MAX_CODE + MAX_INPUT + 256 * 1024  # tar headers and padding
# One plain path component, no leading dot (and __code__, the code itself).
INPUT_NAME = re.compile(r"^[^/\\\x00-\x1f\x7f.][^/\\\x00-\x1f\x7f]{0,99}$")
PY_IMAGE = PREFIX + "python"
# C and C++ entrypoints unpack a tar from stdin when PYRACMS_INPUTS is set.
TAR_IMAGES = {PREFIX + "c", PREFIX + "cpp"}
# The Python runner's entrypoint is `python3 -c`, so files can only arrive
# on stdin: a tar of the code (__code__) and the input files. This unpacks
# it into /tmp (the one writable place), makes that the working directory
# and runs the code as __main__ with the wrapper's own frame left out of any
# traceback.
PREAMBLE = (
    "import sys,os,tarfile,traceback\n"
    "os.chdir('/tmp')\n"
    "t=tarfile.open(fileobj=sys.stdin.buffer,mode='r|')\n"
    "c=b''\n"
    "for m in t:\n"
    " d=t.extractfile(m).read()\n"
    " if m.name=='__code__':c=d\n"
    " else:open(m.name,'wb').write(d)\n"
    "sys.stdin=open(os.devnull)\n"
    "g={'__name__':'__main__'}\n"
    "try:exec(compile(c,'<string>','exec'),g)\n"
    "except SystemExit:raise\n"
    "except BaseException:\n"
    " e=sys.exc_info();traceback.print_exception(e[0],e[1],e[2].tb_next)\n"
    " sys.exit(1)\n"
)
MAX_OUTPUT = 64 * 1024
LABEL = "pyracms-runner=1"

slots = threading.BoundedSemaphore(MAX_CONCURRENT)


def docker(*args, timeout=15):
    return subprocess.run(["docker", *args], capture_output=True, timeout=timeout)


def read_bundle(raw):
    """(code, {name: bytes}) from the shim's tar. ValueError when it is not
    a plain tar of regular files within the caps: the shim only ever sends
    one, so anything else is refused, not repaired."""
    code, files, total = None, {}, 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tf:
        for m in tf:
            name = m.name[2:] if m.name.startswith("./") else m.name
            if m.isdir() and name in ("", "."):
                continue
            if not m.isreg() or m.size > MAX_INPUT:
                raise ValueError("unsupported tar member")
            data = tf.extractfile(m).read()
            if name == "__code__":
                code = data
                continue
            total += len(data)
            if (not INPUT_NAME.match(name) or total > MAX_INPUT
                    or len(files) >= MAX_INPUT_FILES):
                raise ValueError("input file refused")
            files[name] = data
    if code is None or len(code) > MAX_CODE or b"\x00" in code:
        raise ValueError("missing or invalid code")
    return code.decode("utf-8", "replace"), files


def stdin_tar(code, files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in [("__code__", code.encode())] + list(files.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, int(time.time())
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def run_sandbox(image, code, files=None):
    name = "pyracms-run-" + uuid.uuid4().hex[:12]
    cmd = ["docker", "run", "--rm", "--name", name, "--label", LABEL]
    stdin = None
    if image == PY_IMAGE:
        # /tmp is the only writable place; make it the working directory so
        # relative paths work for reading (input files) and writing alike.
        cmd += ["-w", "/tmp"]
        if files:
            cmd += ["-i"]
            stdin, code = stdin_tar(code, files), PREAMBLE
    elif image in TAR_IMAGES and files:
        cmd += ["-i", "-e", "PYRACMS_INPUTS=1"]
        stdin = stdin_tar(code, files)
    cmd += [
        "--network=none",
        "--memory=512m", "--memory-swap=512m",
        "--cpus=1", "--pids-limit=256",
        "--read-only", "--tmpfs", "/tmp:rw,exec,nosuid,size=256m",
        "--security-opt=no-new-privileges", "--cap-drop=ALL",
        image, code,
    ]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           input=stdin, timeout=RUN_TIMEOUT)
        out, code_ = p.stdout, p.returncode
    except subprocess.TimeoutExpired as e:
        docker("rm", "-f", name)
        # 137 = killed. The backend appends its own "Execution timed out"
        # line for 124/137, so no message is added here.
        out, code_ = e.stdout or b"", 137
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
        bundle = self.headers.get("Content-Type", "").startswith(
            "application/x-tar")
        if length > (MAX_TAR if bundle else MAX_CODE):
            return self.reply(413, "request too large", 1)
        raw = self.rfile.read(length)
        files = None
        if bundle:
            try:
                code, files = read_bundle(raw)
            except (ValueError, tarfile.TarError) as e:
                return self.reply(400, "bad input bundle: %s" % e, 1)
        else:
            code = raw.decode("utf-8", "replace")
            if "\x00" in code:
                return self.reply(400, "code contains NUL bytes", 1)
        if not slots.acquire(timeout=QUEUE_WAIT):
            return self.reply(503, "Code runner is busy, try again shortly.", 1)
        try:
            exit_code, out = run_sandbox(image, code, files)
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
