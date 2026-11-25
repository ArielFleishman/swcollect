# Run INSIDE the closed network. Verifies the manifest, loads the images, builds the swcollect image
# from local wheels only, starts the stack and runs the readiness check against the mock.
param([switch]$SkipVerify)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not $SkipVerify) {
    Write-Host "== verify deploy/MANIFEST.sha256"
    $bad = 0
    foreach ($line in Get-Content deploy/MANIFEST.sha256) {
        $hash, $path = $line -split '\s+', 2
        if (-not (Test-Path $path)) { Write-Host "MISSING $path"; $bad++; continue }
        if ((Get-FileHash -Algorithm SHA256 $path).Hash.ToLower() -ne $hash) { Write-Host "BAD     $path"; $bad++ }
    }
    if ($bad) { throw "$bad file(s) missing or changed" }
}

Write-Host "== docker load"
foreach ($tar in Get-ChildItem deploy/images/*.tar) {
    docker load -i $tar.FullName
    if ($LASTEXITCODE -ne 0) { throw "docker load $($tar.Name) failed" }
}

Write-Host "== build + up"
docker compose -f deploy/docker-compose.yml build
if ($LASTEXITCODE -ne 0) { throw "build failed" }
docker compose -f deploy/docker-compose.yml up -d db loki mock
docker compose -f deploy/docker-compose.yml up -d collector grafana
docker compose -f deploy/docker-compose.yml run --rm collector python -m swcollect check --network mock --wait 180
if ($LASTEXITCODE -ne 0) { throw "readiness check failed" }
Write-Host "Stack is up and ready."
