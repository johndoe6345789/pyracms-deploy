#!/bin/sh
# End-to-end check of PyraCMS file storage (docs/STORAGE.md in pyracms_core).
#
#   scripts/test-storage.sh [SECRETS_FILE]        # default ~/pyracms-secrets.txt
#
# Uploads a file through the public API, then proves the bytes are where the
# database says they are:
#   1. upload            -> /api/files returns a uuid + sha256
#   2. files.storage     -> 's3' when STORAGE_BACKEND=s3
#   3. object in store   -> GET {bucket}/tenant-<site>-<uuid> byte-identical
#                           (site comes from the row; no site = 0, the platform)
#   4. public download   -> byte-identical
#   5. delete            -> row gone and object gone (404)
#   6. legacy 'local'    -> a pre-switch file still downloads, if one exists
#
# Needs: curl, python3, and docker (for the internal-only store and database,
# which are not published outside the CapRover overlay network).
set -eu
SECRETS=${1:-$HOME/pyracms-secrets.txt}
SITE=${SITE_URL:-https://pyracms.pynguins.xyz}
TENANT=${TENANT_ID:-1}
UA='Mozilla/5.0 (X11; Linux x86_64) pyracms-storage-test'
NET=captain-overlay-network
DBHOST=srv-captain--pyracms-db
STORE=http://srv-captain--pyracms-objects:9000
CURL_IMG=curlimages/curl:8.10.1
PG_IMG=postgres:15-alpine

# shellcheck disable=SC1090
. "$SECRETS"
: "${ADMIN_PASSWORD:?}" "${DB_PASSWORD:?}" "${S3_ACCESS_KEY:?}" "${S3_SECRET_KEY:?}"
BUCKET=${S3_BUCKET:-pyracms}
fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "ok   $*"; }
psql_() { docker run --rm --network "$NET" -e PGPASSWORD="$DB_PASSWORD" "$PG_IMG" \
    psql -h "$DBHOST" -U pyracms -d pyracms -tAc "$1"; }
# curl runs inside a container, so it cannot write to host paths: fetch to
# stdout (captured here) and ask for the status code separately.
store_body() { docker run --rm --network "$NET" "$CURL_IMG" -sS -m 20 \
    -H "Authorization: AWS $S3_ACCESS_KEY:$S3_SECRET_KEY" "$1"; }
store_code() { docker run --rm --network "$NET" "$CURL_IMG" -s -m 20 -o /dev/null \
    -w '%{http_code}' -H "Authorization: AWS $S3_ACCESS_KEY:$S3_SECRET_KEY" "$1"; }

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
printf 'pyracms storage test %s\n' "$(date -u +%FT%TZ)" > "$tmp/payload.txt"
want=$(sha256sum "$tmp/payload.txt" | cut -d' ' -f1)

# /api/auth/login is rate limited ("Too many requests, slow down"), so a
# couple of runs back to back would otherwise fail on an empty token.
token=""
for attempt in 1 2 3 4 5; do
    body=$(curl -sS -m 20 -A "$UA" -X POST "$SITE/api/auth/login" \
        -H 'Content-Type: application/json' \
        -d "{\"username\":\"${ADMIN_USERNAME:-admin}\",\"password\":\"$ADMIN_PASSWORD\"}")
    token=$(printf '%s' "$body" |
        python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("token",""))
except Exception: print("")')
    [ -n "$token" ] && break
    [ "$attempt" = 5 ] && fail "could not log in as ${ADMIN_USERNAME:-admin}: $body"
    sleep 10
done
ok "logged in"

up=$(curl -sS -m 60 -A "$UA" -H "Authorization: Bearer $token" \
    -F "file=@$tmp/payload.txt;type=text/plain" "$SITE/api/files?tenant_id=$TENANT")
uuid=$(printf '%s' "$up" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("uuid",""))')
[ -n "$uuid" ] || fail "upload failed: $up"
ok "uploaded uuid=$uuid"

backend=$(psql_ "SELECT storage FROM files WHERE uuid = '$uuid'")
[ -n "$backend" ] || fail "no files row for $uuid"
# The key's site number comes from the row, not from the upload request: a
# file with no site is the platform's, which docs/STORAGE.md calls site 0.
site=$(psql_ "SELECT COALESCE(tenant_id, 0) FROM files WHERE uuid = '$uuid'")
ok "database says storage=$backend, site=$site"

if [ "$backend" = s3 ]; then
    key="tenant-$site-$uuid"
    code=$(store_code "$STORE/$BUCKET/$key")
    [ "$code" = 200 ] || fail "object $key not in the store (HTTP $code)"
    store_body "$STORE/$BUCKET/$key" > "$tmp/from-store"
    [ "$(sha256sum "$tmp/from-store" | cut -d' ' -f1)" = "$want" ] ||
        fail "object bytes differ from what was uploaded"
    ok "object $BUCKET/$key present and byte-identical"
else
    echo "note storage=$backend (set STORAGE_BACKEND=s3 to exercise the store)"
fi

curl -sS -m 30 -A "$UA" -o "$tmp/dl" "$SITE/api/files/$uuid"
[ "$(sha256sum "$tmp/dl" | cut -d' ' -f1)" = "$want" ] || fail "public download differs"
ok "public download byte-identical"

curl -sS -m 30 -A "$UA" -X DELETE -H "Authorization: Bearer $token" \
    -o /dev/null -w '' "$SITE/api/files/$uuid"
[ "$(psql_ "SELECT count(*) FROM files WHERE uuid = '$uuid'")" = 0 ] ||
    fail "files row still present after delete"
if [ "$backend" = s3 ]; then
    code=$(store_code "$STORE/$BUCKET/tenant-$site-$uuid")
    [ "$code" = 404 ] || fail "object still in the store after delete (HTTP $code)"
fi
ok "deleted: row and object gone"

legacy=$(psql_ "SELECT uuid FROM files WHERE storage = 'local' LIMIT 1")
if [ -n "$legacy" ]; then
    code=$(curl -sS -m 30 -A "$UA" -o /dev/null -w '%{http_code}' "$SITE/api/files/$legacy")
    [ "$code" = 200 ] || fail "pre-switch local file $legacy no longer downloads (HTTP $code)"
    ok "legacy local file still downloads"
else
    echo "note no 'local' rows left to check"
fi
echo "PASS"
