#!/bin/bash
# The object-store admin UI (CapRover app pyracms-objects-ui) is not exposed
# publicly. This forwards it to 127.0.0.1:3210 on the docker host; reach it
# from your machine with:  ssh -L 3210:127.0.0.1:3210 <host>
# then open http://localhost:3210 and sign in with OBJECTS_UI_ACCESS_KEY /
# OBJECTS_UI_SECRET_KEY from ~/pyracms-secrets.txt. Ctrl-C stops it.
exec docker run --rm --name objects-ui-tunnel \
  --network captain-overlay-network -p 127.0.0.1:3210:3210 \
  alpine/socat TCP-LISTEN:3210,fork,reuseaddr \
  TCP:srv-captain--pyracms-objects-ui:3000
