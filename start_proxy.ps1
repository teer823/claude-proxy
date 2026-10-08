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

# Host directory where the container's log files are persisted
$LogDir = Join-Path $ScriptDir "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# --userns=keep-id maps the host user to appuser (uid/gid 1000) inside the
# container so it can write to the mounted logs folder and files stay owned
# by the host user. DEBUG_LOG_DIR is forced to the mount point (overrides .env).
podman run -d `
    --name $ContainerName `
    --env-file "$ScriptDir\.env" `
    -e DEBUG_LOG_DIR=/app/logs `
    -v "${LogDir}:/app/logs" `
    -v "${ScriptDir}\model_routing.json:/app/model_routing.json:ro" `
    --userns=keep-id:uid=1000,gid=1000 `
    -p 8082:8082 `
    --restart unless-stopped `
    "${ImageName}:${ImageTag}"

Write-Host "Container '$ContainerName' started."
Write-Host "Logs: podman logs -f $ContainerName"
Write-Host "Log files: $LogDir"
Write-Host "Stop: podman stop $ContainerName"