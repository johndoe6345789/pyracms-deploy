# PyraCMS backend for pyracms.pynguins.xyz (CapRover app pyracms-api):
# the published image, minus demo seeding, with the docker CLI replaced by a
# shim that forwards code runs to the runner gateway (app pyracms-runner).
FROM ghcr.io/johndoe6345789/pyracms-backend@sha256:703a4726e6842b0219f80b60373dd65e3dcab884b0d6883c4c7fd2853d8e2b81
USER root
# seed.sh registers admin/password123 on an empty DB (the entrypoint also
# skips it unless SEED_DEV=1 and PYRACMS_ENV != production -- belt and braces).
RUN rm -f /app/seed.sh /app/seed_games.sh
COPY docker-shim.sh /usr/local/bin/docker
RUN chmod 755 /usr/local/bin/docker
USER 10001:10001
