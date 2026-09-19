# PyraCMS backend for pyracms.pynguins.xyz (CapRover app pyracms-api):
# the published image, minus demo seeding, with the docker CLI replaced by a
# shim that forwards code runs to the runner gateway (app pyracms-runner).
FROM ghcr.io/johndoe6345789/pyracms-backend@sha256:4f763fa1a1bf5051207f94166396136d7161fb839a3793986d41d7cc2ac1e255
# seed.sh registers admin/password123 on an empty DB; seed_games.sh is only
# ever sourced by it.
RUN rm -f /app/seed.sh /app/seed_games.sh
COPY docker-shim.sh /usr/local/bin/docker
RUN chmod 755 /usr/local/bin/docker
