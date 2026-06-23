# stop.ps1 — Stop the proxy container
$ErrorActionPreference = "Stop"

$ContainerName = "claude-proxy"

podman container exists $ContainerName 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "Stopping container: $ContainerName"
    podman stop $ContainerName
    Write-Host "Container '$ContainerName' stopped."
} else {
    Write-Host "No running container named '$ContainerName' found."
}