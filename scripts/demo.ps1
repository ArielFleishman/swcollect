# Demo: the stack with the rich mock, the collector filling PostgreSQL / TimescaleDB / Loki through its normal jobs,
# and Grafana on http://127.0.0.1:13000 (admin / SWCOLLECT_GRAFANA_PASSWORD, default change-me).
#   .\scripts\demo.ps1              # start (or keep) the demo and wait until PostgreSQL holds the data
#   .\scripts\demo.ps1 -Down        # stop it and delete ITS volumes (project swcollect-demo only)
# It runs as its own compose project, so another swcollect stack and its data are not touched. Images must be
# present (docker load, see load_offline.ps1); the swcollect image is built from deploy/wheels.
param([switch]$Down, [string]$Project = "swcollect-demo", [int]$TimeoutMinutes = 20)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$f = @("-p", $Project, "-f", "deploy/docker-compose.yml", "-f", "deploy/docker-compose.demo.yml")

if ($Down) {
    docker compose @f down -v --remove-orphans
    exit $LASTEXITCODE
}

Write-Host "== up (project $Project)"
docker compose @f up -d --build db loki mock collector grafana
if ($LASTEXITCODE -ne 0) { throw "stack did not start" }

Write-Host "== waiting for the collector's first runs (rich mock: about 6 minutes)"
$deadline = (Get-Date).AddMinutes($TimeoutMinutes)
$ready = $false
while ((Get-Date) -lt $deadline) {
    $ErrorActionPreference = "Continue"
    $json = docker compose @f exec -T collector python -m swcollect dbstats --network mock --json 2>$null
    $ErrorActionPreference = "Stop"
    if ($LASTEXITCODE -le 1 -and $json) {
        try {
            $rep = ($json -join "`n") | ConvertFrom-Json
            $ts = ($rep.schemas.sw_ts | Measure-Object -Property rows -Sum).Sum
            $core = ($rep.schemas.sw_core | Where-Object { $_.table -eq "config_version" }).rows
            Write-Host ("  sw_ts samples: {0:N0}; sw_core.config_version: {1}" -f $ts, $core)
            if ($ts -gt 0 -and $core -gt 0) { $ready = $true; break }
        } catch { }
    }
    Start-Sleep -Seconds 20
}
if (-not $ready) { throw "PostgreSQL did not fill within $TimeoutMinutes minutes: docker compose -p $Project logs collector" }

Write-Host "== what reached PostgreSQL"
docker compose @f exec -T collector python -m swcollect dbstats --network mock
Write-Host ""
Write-Host "Grafana: http://127.0.0.1:13000  (admin / `$env:SWCOLLECT_GRAFANA_PASSWORD, default change-me)"
Write-Host "Stop and delete the demo's volumes: .\scripts\demo.ps1 -Down"
