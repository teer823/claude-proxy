# start_proxy.ps1 — Start the proxy container via Podman
$ErrorActionPreference = "Stop"

$ImageName     = "claude-proxy"
$ImageTag      = if ($args[0]) { $args[0] } else { "latest" }
$ContainerName = "claude-proxy"
$ScriptDir     = Split-Path -Parent $MyInvocation.MyCommand.Definition

# Remove any existing container with the same name
$exists = podman container exists $ContainerName 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "Removing existing container: $ContainerName"
    podman rm -f $ContainerName
}

Write-Host "Starting Claude Code Proxy container on port 8082..."

podman run -d `
    --name $ContainerName `
    --env-file "$ScriptDir\.env" `
    -p 8082:8082 `
    --restart unless-stopped `
    "${ImageName}:${ImageTag}"

Write-Host "Container '$ContainerName' started."
Write-Host "Logs: podman logs -f $ContainerName"
Write-Host "Stop: podman stop $ContainerName"