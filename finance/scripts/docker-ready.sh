#!/bin/sh
# Exits 0 when the Docker daemon answers, else 1 with one line on stderr that names the endpoint.
# The gate (.yardr/check) and the finance scripts that need Docker call it once, before their first
# docker call (erp-xfem). Colima on the mini is always on since IT's cutover (yard-vbpa, 11-10), so a
# daemon that does not answer is a fault for IT: nothing here starts, wakes or stops a runtime, and
# plain docker talks to whatever DOCKER_HOST or the current context names.
#
#   sh finance/scripts/docker-ready.sh || exit <the caller's code>
if docker info >/dev/null 2>&1; then
  exit 0
fi
ENDPOINT=${DOCKER_HOST:-}
if [ -z "$ENDPOINT" ]; then
  ENDPOINT=$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null || true)
fi
echo "docker daemon not reachable at ${ENDPOINT:-the default context}; on the mini ask IT, don't start or stop runtimes" >&2
exit 1
