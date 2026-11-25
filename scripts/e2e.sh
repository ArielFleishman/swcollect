#!/bin/sh
# Linux version of scripts/e2e.ps1.
set -e
cd "$(dirname "$0")/.."
F="-f deploy/docker-compose.yml -f deploy/docker-compose.e2e.yml"
docker compose $F --profile e2e down -v --remove-orphans
docker compose $F up -d --build db loki mock collector
set +e
docker compose $F --profile e2e run --rm e2e; E2E=$?
echo "== outbound traffic must fail"
docker compose $F run --rm --no-deps collector python -c "import requests; requests.get('https://pypi.org', timeout=5)" >/dev/null 2>&1 && { echo "a container reached the internet"; exit 1; }
set -e
echo "OK: no route out of swnet"
[ "$E2E" -eq 0 ] || { echo "e2e failed"; exit 1; }
echo "e2e PASS on the closed network"
