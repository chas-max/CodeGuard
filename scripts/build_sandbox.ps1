param(
    [string]$Tag = "codeguard-sandbox-python:3.12"
)
$ErrorActionPreference = "Stop"
docker build -f dockerfiles/sandbox/python.Dockerfile -t $Tag .
Write-Output "built $Tag"
