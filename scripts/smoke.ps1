# Phase 0 smoke test on the internal (no internet) network:
#   1. the readiness check passes against the mock, PostgreSQL + TimescaleDB and Loki;
#   2. a container on that network cannot reach the internet.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$c = "deploy/docker-compose.yml"

docker compose -f $c up -d --build db loki mock collector
docker compose -f $c run --rm collector python -m swcollect check --network mock --wait 180
if ($LASTEXITCODE -ne 0) { throw "readiness check failed" }

Write-Host "== outbound traffic must fail"
# Windows PowerShell 5.1 turns native stderr into errors under "Stop"; the failure here is the expected result.
$ErrorActionPreference = "Continue"
docker compose -f $c run --rm --no-deps collector python -c "import requests; requests.get('https://pypi.org', timeout=5)" *> $null
$code = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($code -eq 0) { throw "a container on swnet reached the internet: the network is not closed" }
Write-Host "OK: no route out of swnet"
