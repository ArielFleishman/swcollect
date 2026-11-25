# End-to-end check on the closed network (no internet): fresh volumes, the stack on the internal network, the e2e
# against the mock, PostgreSQL + TimescaleDB, Loki and the push receiver, then the "no route out" check.
#   .\scripts\e2e.ps1            # builds offline from deploy/wheels; images must be loaded (load_offline.ps1)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$f = @("-f", "deploy/docker-compose.yml", "-f", "deploy/docker-compose.e2e.yml")

docker compose @f --profile e2e down -v --remove-orphans
docker compose @f up -d --build db loki mock collector
if ($LASTEXITCODE -ne 0) { throw "stack did not start" }
docker compose @f --profile e2e run --rm e2e
$e2e = $LASTEXITCODE

Write-Host "== outbound traffic must fail"
$ErrorActionPreference = "Continue"
docker compose @f run --rm --no-deps collector python -c "import requests; requests.get('https://pypi.org', timeout=5)" *> $null
$out = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($out -eq 0) { throw "a container on swnet reached the internet: the network is not closed" }
Write-Host "OK: no route out of swnet"
if ($e2e -ne 0) { throw "e2e failed (exit $e2e)" }
Write-Host "e2e PASS on the closed network"
