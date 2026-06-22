# Claude Code Proxy

An **Anthropic-to-OpenAI proxy server** that accepts requests in the [Anthropic Messages API](https://docs.anthropic.com/en/api/messages) format and forwards them to any **OpenAI-compatible** backend (default: IBM ICA).

This lets you point **Claude Code** (or any Anthropic-compatible client) at your own OpenAI-compatible endpoint — including IBM watsonx / ICA — without modifying the client.

## Features

- Translates Anthropic ↔ OpenAI request/response formats in both directions
- Supports **streaming** (SSE) and **non-streaming** responses
- Handles Anthropic's built-in `web_search` tool via an internal agentic loop (DuckDuckGo or Tavily)
- Supports **extended thinking** (`thinking: {type: "enabled", budget_tokens: N}`)
- XML tool-call mode for upstreams that don't support native OpenAI function calling
- Debug logging mode with daily-rotating log files
- Runs on **port 8082**

---

## Prerequisites

| | Mac | Windows |
|---|---|---|
| Python | 3.11+ via [python.org](https://www.python.org/downloads/) or `brew install python` | 3.11+ from [python.org](https://www.python.org/downloads/) |
| Git | `brew install git` or Xcode CLT | [git-scm.com](https://git-scm.com/download/win) |
| Container runtime *(optional)* | [Podman Desktop](https://podman-desktop.io/) or Docker | [Podman Desktop](https://podman-desktop.io/) or Docker Desktop |

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/teer823/claude-proxy.git
cd claude-proxy
```

### 2. Configure environment variables

Copy the sample env file and fill in your values:

**macOS / Linux:**
```bash
cp .env.sample .env
```

**Windows:**
```cmd
copy .env.sample .env
```

Open `.env` and set the following:

```env
# URL of your OpenAI-compatible upstream (e.g. IBM ICA)
OPENAI_BASE_URL=https://sg.ica.ibm.com/ica/apis/v3

# Bearer token / API key for the upstream
OPENAI_API_KEY=your-api-key-here

# Model name sent to the upstream (overrides whatever the client requests)
DEFAULT_MODEL=global/anthropic.claude-sonnet-4-6

# Web search provider: "duckduckgo" (no key required) or "tavily"
WEB_SEARCH_PROVIDER=duckduckgo

# Required only when WEB_SEARCH_PROVIDER=tavily
TAVILY_API_KEY=

# How long (seconds) to wait for a response from the upstream
UPSTREAM_READ_TIMEOUT=300.0

# Debug logging (set true to write full request/response logs to DEBUG_LOG_DIR)
DEBUG_MODE=false
DEBUG_LOG_DIR=logs
```

---

## Running Locally (Python virtualenv)

### macOS / Linux

```bash
# Create virtual environment
python3 -m venv .venv

# Activate it
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Start the server
python main.py
```

Or use the included debug script (automatically creates the venv, enables hot-reload and debug logging):

```bash
bash start_debug.sh
```

### Windows — Command Prompt

```cmd
:: Create virtual environment
python -m venv .venv

:: Activate it
.venv\Scripts\activate.bat

:: Install dependencies
pip install -r requirements.txt

:: Start the server
python main.py
```

### Windows — PowerShell

```powershell
# Create virtual environment
python -m venv .venv

# Activate it
.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt

# Start the server
python main.py
```

> **PowerShell note:** If you see a script execution error, allow local scripts once with:
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```

The server starts on **http://localhost:8082**. Verify it is running:

```bash
curl http://localhost:8082/health
# {"status":"ok","default_model":"global/anthropic.claude-sonnet-4-6"}
```

---

## Running in a Container (Podman / Docker)

The helper scripts use **Podman**. Substitute `docker` for `podman` if you are using Docker.

### Build the image

```bash
bash build.sh
# equivalent: podman build -t claude-proxy .
```

### Start the container

```bash
bash start_proxy.sh
```

Or run it manually:

```bash
podman run -d \
  --name claude-proxy \
  --env-file .env \
  -p 8082:8082 \
  --restart unless-stopped \
  claude-proxy:latest
```

### View logs

```bash
podman logs -f claude-proxy
```

### Stop the container

```bash
bash stop.sh
# equivalent: podman stop claude-proxy
```

### Windows — Docker Desktop

```powershell
# Build
docker build -t claude-proxy .

# Start
docker run -d `
  --name claude-proxy `
  --env-file .env `
  -p 8082:8082 `
  --restart unless-stopped `
  claude-proxy:latest

# View logs
docker logs -f claude-proxy

# Stop
docker stop claude-proxy
```

---

## Connecting Claude Code

Once the proxy is running on port 8082, tell Claude Code to send its requests there instead of the official Anthropic API.

### Option 1: Environment variable (recommended)

**macOS / Linux — one-time:**

```bash
export ANTHROPIC_BASE_URL=http://localhost:8082
claude
```

**macOS / Linux — permanent** (add to `~/.zshrc` or `~/.bashrc`):

```bash
echo 'export ANTHROPIC_BASE_URL=http://localhost:8082' >> ~/.zshrc
source ~/.zshrc
```

**Windows — Command Prompt (session only):**

```cmd
set ANTHROPIC_BASE_URL=http://localhost:8082
claude
```

**Windows — PowerShell (session only):**

```powershell
$env:ANTHROPIC_BASE_URL = "http://localhost:8082"
claude
```

**Windows — permanent:** Open *System Properties → Environment Variables* and add a new user variable `ANTHROPIC_BASE_URL` with value `http://localhost:8082`.

---

### Option 2: Claude Code `--api-url` flag

Pass the proxy URL directly when launching Claude Code:

```bash
claude --api-url http://localhost:8082
```

---

### Option 3: Claude Code settings file

Create or edit `.claude/settings.json` in your home directory or project root:

```json
{
  "apiBaseUrl": "http://localhost:8082"
}
```

---

> **API key & model:** Claude Code requires `ANTHROPIC_API_KEY` to be set to a non-empty value, but the proxy does not validate it. You can also pin the model via `ANTHROPIC_MODEL` so Claude Code uses the same model configured in the proxy's `DEFAULT_MODEL`:
>
> **macOS / Linux:**
> ```bash
> export ANTHROPIC_API_KEY="any-key"
> export ANTHROPIC_MODEL="global/anthropic.claude-sonnet-4-6"
> ```
>
> **Windows — Command Prompt:**
> ```cmd
> set ANTHROPIC_API_KEY=any-key
> set ANTHROPIC_MODEL=global/anthropic.claude-sonnet-4-6
> ```
>
> **Windows — PowerShell:**
> ```powershell
> $env:ANTHROPIC_API_KEY = "any-key"
> $env:ANTHROPIC_MODEL = "global/anthropic.claude-sonnet-4-6"
> ```

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `OPENAI_BASE_URL` | `https://sg.ica.ibm.com/ica/apis/v3` | OpenAI-compatible upstream base URL |
| `OPENAI_API_KEY` | *(required)* | Bearer token for the upstream |
| `DEFAULT_MODEL` | `global/anthropic.claude-sonnet-4-6` | Model name sent to upstream; overrides the client's requested model |
| `WEB_SEARCH_PROVIDER` | `duckduckgo` | `duckduckgo` (no key needed) or `tavily` |
| `TAVILY_API_KEY` | *(empty)* | Required when `WEB_SEARCH_PROVIDER=tavily` |
| `UPSTREAM_READ_TIMEOUT` | `300.0` | Seconds to wait for upstream response/stream |
| `DEBUG_MODE` | `false` | Set `true` to write full request/response logs to files |
| `DEBUG_LOG_DIR` | `logs` | Directory for daily-rotating debug log files |

---

## Project Structure

```
main.py                  # FastAPI app, Settings, health endpoint
routers/
  messages.py            # POST /v1/messages — main handler, agentic loop, streaming
schemas/
  anthropic.py           # Pydantic models for Anthropic API
  openai.py              # Pydantic models for OpenAI API
services/
  proxy.py               # forward_request() and stream_request() — raw HTTP to upstream
  translator.py          # Request/response translation between Anthropic and OpenAI formats
  web_search.py          # perform_web_search() — DuckDuckGo and Tavily providers
  debug_logger.py        # Debug request/response file logging
```

---

## Troubleshooting

**`curl http://localhost:8082/health` fails**
- Confirm the server is running (`python main.py` output should show `Uvicorn running on http://0.0.0.0:8082`)
- Check that no other process is using port 8082: `lsof -i :8082` (Mac/Linux) or `netstat -ano | findstr 8082` (Windows)

**Claude Code says "connection refused"**
- Verify `ANTHROPIC_BASE_URL` is set correctly in the shell where `claude` is launched
- If the proxy is running in a container, make sure port 8082 is published (`-p 8082:8082`)

**Upstream returns 401 / authentication errors**
- Double-check `OPENAI_API_KEY` in your `.env` file
- Ensure the key has access to the model specified in `DEFAULT_MODEL`

**Requests time out on large documents or long responses**
- Increase `UPSTREAM_READ_TIMEOUT` in `.env` (e.g. `UPSTREAM_READ_TIMEOUT=600`)

**Enable debug logging to inspect raw requests and responses**
- Set `DEBUG_MODE=true` in `.env` and restart the server
- Logs are written to `logs/` (or the directory set in `DEBUG_LOG_DIR`) as daily-rotating files
