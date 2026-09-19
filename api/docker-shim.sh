#!/bin/sh
# Stand-in for the docker CLI inside the PyraCMS backend. The backend runs
#   timeout 30 docker run --rm <sandbox flags> IMAGE 'CODE' 2>&1
# (DockerExecutionService.cpp); this forwards IMAGE and CODE to the runner
# gateway, which owns docker.sock and applies the sandbox limits itself. The
# backend never talks to the Docker daemon.
[ "$1" = run ] || { echo "docker shim: only 'run' is supported" >&2; exit 125; }
[ $# -ge 3 ] || { echo "docker shim: expected IMAGE and CODE" >&2; exit 125; }
eval "image=\${$(($# - 1))}"
eval "code=\${$#}"

hdr=$(mktemp)
printf '%s' "$code" | curl -sS -m 29 -D "$hdr" --data-binary @- \
    -H 'Content-Type: text/plain; charset=utf-8' \
    "${RUNNER_GATEWAY_URL:-http://srv-captain--pyracms-runner:8080}/run?image=$image"
rc=$?
ec=$(tr -d '\r' < "$hdr" | awk 'tolower($1)=="x-exit-code:" {print $2}')
rm -f "$hdr"
[ $rc -eq 0 ] || { echo "Code runner unavailable."; exit 1; }
exit "${ec:-1}"
