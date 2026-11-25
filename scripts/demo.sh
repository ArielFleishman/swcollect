#!/bin/sh
# Demo (Linux): same as demo.ps1. Usage: scripts/demo.sh [--down]
set -e
cd "$(dirname "$0")/.."
P="${SWCOLLECT_DEMO_PROJECT:-swcollect-demo}"
F="-p $P -f deploy/docker-compose.yml -f deploy/docker-compose.demo.yml"
if [ "$1" = "--down" ]; then
  docker compose $F down -v --remove-orphans
  exit 0
fi
echo "== up (project $P)"
docker compose $F up -d --build db loki mock collector grafana
echo "== waiting for the collector's first runs (rich mock: about 6 minutes)"
i=0
until docker compose $F exec -T collector python -c "
import json, subprocess, sys
out = subprocess.run([sys.executable, '-m', 'swcollect', 'dbstats', '--network', 'mock', '--json'],
                     capture_output=True, text=True).stdout
r = json.loads(out)
ts = sum(t['rows'] or 0 for t in r['schemas'].get('sw_ts', []))
cv = sum(t['rows'] or 0 for t in r['schemas'].get('sw_core', []) if t['table'] == 'config_version')
print(f'  sw_ts samples: {ts}; sw_core.config_version: {cv}')
sys.exit(0 if ts and cv else 1)" 2>/dev/null; do
  i=$((i + 1))
  [ "$i" -gt 60 ] && { echo "PostgreSQL did not fill in time: docker compose -p $P logs collector"; exit 1; }
  sleep 20
done
echo "== what reached PostgreSQL"
docker compose $F exec -T collector python -m swcollect dbstats --network mock
echo
echo "Grafana: http://127.0.0.1:13000  (admin / \$SWCOLLECT_GRAFANA_PASSWORD, default change-me)"
echo "Stop and delete the demo's volumes: scripts/demo.sh --down"
