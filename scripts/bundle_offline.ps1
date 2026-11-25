# Run OUTSIDE the closed network, on a machine with internet and Docker.
# Downloads everything the closed network needs and writes a SHA256 manifest:
#   deploy/wheels/   Linux wheels for the swcollect image (manylinux, Python 3.12)
#   wheels/          Windows wheels for a local .venv (win_amd64, Python 3.12)
#   deploy/images/   docker save of every image in deploy/docker-compose.yml + the python base image
param(
    [string]$PythonImage = "python:3.12.10-slim",
    [string]$PythonVersion = "3.12"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$req = Join-Path $root "requirements.txt"

Write-Host "== wheels"
New-Item -ItemType Directory -Force deploy/wheels, wheels, deploy/images | Out-Null
python -m pip download -r $req -d deploy/wheels --only-binary=:all: --platform manylinux2014_x86_64 --python-version $PythonVersion
if ($LASTEXITCODE -ne 0) { throw "pip download (linux) failed" }
python -m pip download -r $req -d wheels --only-binary=:all: --platform win_amd64 --python-version $PythonVersion
if ($LASTEXITCODE -ne 0) { throw "pip download (windows) failed" }

Write-Host "== images"
$compose = "deploy/docker-compose.yml"
$images = @(docker compose -f $compose config --images | Where-Object { $_ -and ($_ -notlike "swcollect:*") }) + $PythonImage
$images = $images | Sort-Object -Unique
foreach ($img in $images) {
    docker pull $img
    if ($LASTEXITCODE -ne 0) { throw "docker pull $img failed" }
    $file = "deploy/images/" + ($img -replace '[/:]', '_') + ".tar"
    docker save -o $file $img
    if ($LASTEXITCODE -ne 0) { throw "docker save $img failed" }
    Write-Host "  saved $img -> $file"
}
$images | Set-Content -Encoding ascii deploy/images/IMAGES.txt

Write-Host "== manifest"
$files = Get-ChildItem -File deploy/images/*.tar, deploy/wheels/*, wheels/* | Sort-Object FullName
$lines = foreach ($f in $files) {
    $rel = (Resolve-Path -Relative $f.FullName).TrimStart('.', '\', '/') -replace '\', '/'
    "{0}  {1}" -f (Get-FileHash -Algorithm SHA256 $f.FullName).Hash.ToLower(), $rel
}
$lines | Set-Content -Encoding ascii deploy/MANIFEST.sha256
Write-Host "Done. Carry the whole swcollect folder into the closed network and run scripts/load_offline.ps1 (or .sh)."
