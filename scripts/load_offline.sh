#!/bin/sh
# Run INSIDE the closed network (Linux). Same steps as load_offline.ps1.
set -e
cd "$(dirname "$0")/.."
if [ "$1" != "--skip-verify" ]; then
  echo "== verify deploy/MANIFEST.sha256"
  sha256sum -c deploy/MANIFEST.sha256 --quiet
fi
echo "== docker load"
for tar in deploy/images/*.tar; do docker load -i "$tar"; done
echo "== build + up"
docker compose -f deploy/docker-compose.yml build
docker compose -f deploy/docker-compose.yml up -d db loki mock
docker compose -f deploy/docker-compose.yml up -d collector grafana
docker compose -f deploy/docker-compose.yml run --rm collector python -m swcollect check --network mock --wait 180
echo "Stack is up and ready."
