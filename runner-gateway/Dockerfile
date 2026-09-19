# Code-runner gateway (CapRover app pyracms-runner): the only PyraCMS
# component with the host's docker.sock. Internal only; see gateway.py.
FROM docker:27-cli
RUN apk add --no-cache python3
COPY gateway.py /gateway.py
ENV RUNNER_IMAGE_PREFIX=ghcr.io/johndoe6345789/pyracms-runner-
EXPOSE 8080
CMD ["python3", "-u", "/gateway.py"]
