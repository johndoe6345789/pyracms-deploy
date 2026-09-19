# pyracms-deploy

How [PyraCMS](https://github.com/johndoe6345789/pyracms_core) (the C++ port)
runs at **https://pyracms.pynguins.xyz** on CapRover, behind a Cloudflare
Tunnel. It uses the images CI publishes to GHCR; this repo only holds the
deployment-specific pieces.

## CapRover apps

| App | Image / source | Exposed | Notes |
| --- | --- | --- | --- |
| `pyracms` | `ghcr.io/johndoe6345789/pyracms-frontend@<digest>` | yes | Next.js on :3000. Custom nginx config ([caprover/pyracms-nginx.ejs](caprover/pyracms-nginx.ejs)) sends `/api/` to `pyracms-api`, like `pyracms-cpp-port/nginx.conf`. Custom domain `pyracms.pynguins.xyz` |
| `pyracms-api` | [api/](api/) | no | Published backend image, minus demo seeding, with the docker CLI replaced by a shim |
| `pyracms-runner` | [runner-gateway/](runner-gateway/) | no | The **only** app with `/var/run/docker.sock` |
| `pyracms-db` | `postgres:15-alpine` | no | Persistent volume `pyracms-db-data` |
| `pyracms-redis` | `redis:7-alpine`, 128 MB LRU, no persistence | no | Cache only |

Elasticsearch is not deployed: with `SEARCH_ENGINE=postgres` the backend uses
PostgreSQL full-text search (saves ~1 GB RAM on a 1-CPU / 7 GB host).

Backend environment: `DB_HOST=srv-captain--pyracms-db`, `DB_PORT`, `DB_NAME`,
`DB_USER`, `DB_PASSWORD`, `JWT_SECRET`, `SERVER_HOST`, `SERVER_PORT=8080`,
`REDIS_HOST=srv-captain--pyracms-redis`, `REDIS_PORT`, `SEARCH_ENGINE=postgres`,
`RUNNER_IMAGE_PREFIX=ghcr.io/johndoe6345789/pyracms-runner-`. Secrets live in
`~/pyracms-secrets.txt` on the host, not here.

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
- It kills the **container** at the 25 s deadline. Upstream's
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
