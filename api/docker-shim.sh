#!/bin/sh
# Stand-in for the docker CLI inside the PyraCMS backend. The backend execs
#   timeout -k 5 30 docker run --rm <sandbox flags> IMAGE CODE
# (DockerRunArgv.cpp, no shell); this forwards IMAGE and CODE to the runner
# gateway, which owns docker.sock and applies the sandbox limits itself. The
# backend never talks to the Docker daemon.
[ "$1" = run ] || { echo "docker shim: only 'run' is supported" >&2; exit 125; }
[ $# -ge 3 ] || { echo "docker shim: expected IMAGE and CODE" >&2; exit 125; }
eval "image=\${$(($# - 1))}"
eval "code=\${$#}"

# A run with input files (a snippet's attachments) carries the directory the
# backend staged them in as `--label pyracms.inputs=DIR`; DIR also holds the
# code, as __code__. Send all of it as one tar; plain runs send the code.
inputs=""; prev=""
for a in "$@"; do
    [ "$prev" = "--label" ] && case "$a" in
        pyracms.inputs=/tmp/pyracms-run-*) inputs=${a#pyracms.inputs=} ;;
    esac
    prev=$a
done

url="${RUNNER_GATEWAY_URL:-http://srv-captain--pyracms-runner:8080}/run?image=$image"
hdr=$(mktemp)
if [ -n "$inputs" ] && [ -f "$inputs/__code__" ]; then
    tar -C "$inputs" -cf - . | curl -sS -m 29 -D "$hdr" --data-binary @- \
        -H 'Content-Type: application/x-tar' "$url"
else
    printf '%s' "$code" | curl -sS -m 29 -D "$hdr" --data-binary @- \
        -H 'Content-Type: text/plain; charset=utf-8' "$url"
fi
rc=$?
ec=$(tr -d '\r' < "$hdr" | awk 'tolower($1)=="x-exit-code:" {print $2}')
rm -f "$hdr"
[ $rc -eq 0 ] || { echo "Code runner unavailable."; exit 1; }
exit "${ec:-1}"
