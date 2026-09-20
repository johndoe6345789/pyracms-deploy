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
- It runs one sandbox at a time (single-CPU host), with a short queue, and
  reaps orphaned sandboxes on start.

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
