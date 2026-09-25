# pyracms-deploy

How [PyraCMS](https://github.com/johndoe6345789/pyracms_core) (the C++ port)
runs at **https://pyracms.pynguins.xyz** on CapRover, behind a Cloudflare
Tunnel. It uses the images CI publishes to GHCR; this repo only holds the
deployment-specific pieces.

## CapRover apps

| App | Image / source | Exposed | Notes |
| --- | --- | --- | --- |
| `pyracms` | `ghcr.io/johndoe6345789/pyracms-frontend@<digest>` | yes | Next.js on :3000. Custom nginx config ([caprover/pyracms-nginx.ejs](caprover/pyracms-nginx.ejs)) sends `/api/` to `pyracms-api`, like `pyracms-cpp-port/nginx.conf`. Custom domain `pyracms.pynguins.xyz` |
| `pyracms-api` | [api/](api/) | no | Published backend image, minus demo seeding, with the docker CLI replaced by a shim. Runs as uid 10001; persistent volume `pyracms-api-uploads` → `/app/uploads` |
| `pyracms-runner` | [runner-gateway/](runner-gateway/) | no | The **only** app with `/var/run/docker.sock` |
| `pyracms-db` | `postgres:15-alpine` | no | Persistent volume `pyracms-db-data` |
| `pyracms-redis` | `redis:7-alpine`, 128 MB LRU, no persistence | no | Cache only |
| `pyracms-objects` | `ghcr.io/johndoe6345789/object-store@<digest>` | no | S3-compatible store for uploaded file bytes. Persistent volume `pyracms-objects-data` → `/data/s3`; its tables live in an `objectstore` database on `pyracms-db` |
| `pyracms-objects-ui` | `ghcr.io/johndoe6345789/object-store-frontend@<digest>` | no | Admin UI for the store. **Not exposed** (`notExposeAsWebApp`); env `S3_BACKEND_URL=http://srv-captain--pyracms-objects:9000`. Reach it with [scripts/objects-ui-tunnel.sh](scripts/objects-ui-tunnel.sh) over SSH; sign in with the `OBJECTS_UI_*` key |

Elasticsearch is not deployed: with `SEARCH_ENGINE=postgres` the backend uses
PostgreSQL full-text search (saves ~1 GB RAM on a 1-CPU / 7 GB host).

Backend environment: `DB_HOST=srv-captain--pyracms-db`, `DB_PORT`, `DB_NAME`,
`DB_USER`, `DB_PASSWORD`, `JWT_SECRET`, `SERVER_HOST`, `SERVER_PORT=8080`,
`REDIS_HOST=srv-captain--pyracms-redis`, `REDIS_PORT`, `SEARCH_ENGINE=postgres`,
`RUNNER_IMAGE_PREFIX=ghcr.io/johndoe6345789/pyracms-runner-`,
`PYRACMS_ENV=production` (fatal on a weak `JWT_SECRET`, no demo seeding),
`CORS_ALLOWED_ORIGINS=https://pyracms.pynguins.xyz,https://pyracms.wardcrew.com`,
`PUBLIC_BASE_URL=https://pyracms.pynguins.xyz`, plus the storage settings
below. Secrets live in `~/pyracms-secrets.txt` on the host, not here.

## File storage (S3)

Uploaded bytes go to the object store rather than the app's disk
(`docs/STORAGE.md` in pyracms_core). The backend runs with:

    STORAGE_BACKEND=s3
    S3_ENDPOINT=http://srv-captain--pyracms-objects:9000
    S3_BUCKET=pyracms            # created on first use
    S3_ACCESS_KEY / S3_SECRET_KEY

The store authenticates with `Authorization: AWS <key>:<secret>` against an
`api_keys` row, not an env var. Its seed ships a `minioadmin/minioadmin` key:
that row is **deleted** here and replaced with a generated one, as
`scripts/objectstore-init.sh` advises for production.

    -- in the objectstore database on pyracms-db
    INSERT INTO api_keys (access_key, secret_key, owner, permissions)
    VALUES (:ak, :sk, 'pyracms', 'read,write')
    ON CONFLICT (access_key) DO UPDATE SET secret_key = EXCLUDED.secret_key;
    DELETE FROM api_keys WHERE access_key = 'minioadmin';

Keys are flat: `tenant-<siteId>-<uuid>`, where the site comes from the file's
row and a file with no site is the platform's (site 0). The `/app/uploads`
volume stays mounted: rows written before the switch say `storage=local` and
are still read from disk.

### The object-store bug this deployment hit (fixed upstream)

Gallery uploads failed with "File storage is temporarily unavailable" (the
backend's 503 for a store that timed out). object-store ran every handler's
blocking work -- `execSqlSync`, blob reads and writes -- on drogon's IO loop
threads, so a loop stuck in one query stopped answering every later
connection: ~1 request in 3 got no response at all, `/health` included.

Fixed in object-store
[2643290](https://github.com/johndoe6345789/object-store/commit/2643290):
handlers run on a worker pool and reply from there. That commit also stops a
delete of one key removing another key's data (blobs are content-addressed,
so identical bytes are one file), stops a missing or half-written blob being
served as an empty 200, and stops the seed migration resurrecting the
`minioadmin` key on every restart.

The store app here runs that build. Verified after deploying it: 165
requests with no failures (was ~30%), the storage test below passes, and 12
gallery-sized uploads (150 KB - 1.8 MB) all succeeded.

[scripts/test-storage.sh](scripts/test-storage.sh) checks the whole path
(upload → `files.storage` → object in the bucket → public download → delete
removes both → a pre-switch local file still downloads):

    ./scripts/test-storage.sh ~/pyracms-secrets.txt

## Code runner: why a gateway

Upstream's compose file mounts `docker.sock` into the backend so it can
`docker run` the `pyracms-runner-*` sandboxes, and warns that this is for
trusted dev machines only: whoever holds the socket is root on the host. On a
public site with open registration, any account can press **Run**, so here:

- The backend's `docker` is [api/docker-shim.sh](api/docker-shim.sh). It turns
  `docker run ... IMAGE CODE` into `POST /run?image=IMAGE` to the gateway. The
  backend itself never talks to Docker.
- [runner-gateway/gateway.py](runner-gateway/gateway.py) accepts only the 7
  allowlisted runner images, and applies the same limits as upstream (no
  network, 512 MB, 1 CPU, 256 pids, read-only root, 256 MB `/tmp`,
  `no-new-privileges`) plus `--cap-drop=ALL`.
- It kills the **container** at 27 s (the backend gives up at 30 s and adds
  the "timed out" message). Upstream's
  `timeout 30 docker run` only kills the CLI, which leaves the container
  running.
- It runs up to `MAX_CONCURRENT` (default 4) sandboxes at once, each capped
  at 1 CPU and 512 MB, with a short queue, and reaps orphaned sandboxes on
  start.

### Input files (snippet attachments)

A snippet's attachments are its input files: the backend reads them from file
storage, stages them with the code in a temp dir, and the shim (`--label
pyracms.inputs=DIR` on the `docker run` argv) sends the lot to the gateway as
one tar (`Content-Type: application/x-tar`, member `__code__` plus the files).
The gateway checks it (regular files only, one plain path component per name,
at most 64 files / 8 MB) and streams a tar to the sandbox's stdin:

- **Python** (`python3 -c`): the gateway passes a short preamble as the code;
  it unpacks the tar into `/tmp`, `chdir`s there and `exec`s `__code__` as
  `__main__`, so tracebacks and exit codes look like a normal run.
- **C / C++**: the entrypoints (`docker/c`, `docker/cpp` in pyracms_core)
  `tar -x` into `/tmp` when `PYRACMS_INPUTS=1` and run from there.
- Other languages run as before, without the files.

`/tmp` (the only writable place, a 256 MB tmpfs) is the working directory, so
a script can also *write* a file. Nothing else about the sandbox changes: no
network, read-only root, no capabilities.

So a compromised backend can do no more than any site user already can.

## Deploying / updating

The seed script (`seed.sh`) registers `admin` / `password123` on an empty
database, so [api/Dockerfile](api/Dockerfile) deletes it. The real admin
account was created by hand; its password is in `~/pyracms-secrets.txt`.

To take new images:

1. Back up: `pg_dump` from `srv-captain--pyracms-db` into `~/backups/`
   (the entrypoint re-runs every migration with `ON_ERROR_STOP=1`).
2. Pin the new backend digest in `api/Dockerfile`, then deploy `api/` as a
   tarball to `pyracms-api` (e.g. `caprover deploy -a pyracms-api` from `api/`).
3. Deploy the frontend digest to `pyracms` (Deploy via ImageName).
4. `docker pull` the `pyracms-runner-*` images on the host. The gateway runs
   whatever is tagged `:latest` locally.
5. Deploy `runner-gateway/` to `pyracms-runner` when `gateway.py` changes. The
   app needs persistent data enabled for the host-path volume
   `/var/run/docker.sock` → `/var/run/docker.sock`.

## Object store auth and big uploads

- Keys live in the store's `api_keys` table (`owner`, `permissions`, exact
  tokens `read` / `write` / `admin`) and every bucket is scoped to its key's
  owner. There is no default key (`minioadmin` is deleted). Two keys exist:
  the app's (`S3_ACCESS_KEY` in `~/pyracms-secrets.txt`) and a separate one
  for the UI (`OBJECTS_UI_ACCESS_KEY`), so either can be revoked alone.
- The store is never exposed publicly; only `pyracms-api` and the UI reach it.
- Files over ~40 MB upload in 50 MB parts (`POST /api/files/uploads`, see
  pyracms_core `docs/STORAGE.md`), so a 1 GB archive gets past Cloudflare's
  100 MB request cap. The store assembles the parts on disk (cap
  `S3_MAX_OBJECT_BYTES`, 2 GiB).
