# start_debug.ps1 — Start the proxy in debug/dev mode (with --reload)
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

# Create virtualenv if it doesn't exist
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..."
    python -m venv .venv
}

# Install / sync dependencies
Write-Host "Installing dependencies..."
.\.venv\Scripts\pip.exe install --quiet -r requirements.txt

# Load .env if present
if (Test-Path ".env") {
    Write-Host "Loading .env..."
    Get-Content ".env" | ForEach-Object {
        if ($_ -match "^\s*([^#][^=]+)=(.*)$") {
            $name  = $Matches[1].Trim()
            $value = $Matches[2].Trim()
            [System.Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

Write-Host "Starting Claude Code Proxy (debug) on port 8082..."
$env:DEBUG_MODE = "false"
if (-not $env:DEBUG_LOG_DIR) { $env:DEBUG_LOG_DIR = "logs" }

.\.venv\Scripts\uvicorn.exe main:app `
    --host 0.0.0.0 `
    --port 8082 `
    --reload `
    --log-level debug