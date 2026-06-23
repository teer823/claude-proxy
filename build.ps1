# build.ps1 — Build the proxy container image via Podman
$ErrorActionPreference = "Stop"

$ImageName = "claude-proxy"
$ImageTag  = if ($args[0]) { $args[0] } else { "latest" }

Write-Host "Building image: ${ImageName}:${ImageTag}"

podman build `
    --tag "${ImageName}:${ImageTag}" `
    --file Containerfile `
    .

Write-Host "Build complete: ${ImageName}:${ImageTag}"