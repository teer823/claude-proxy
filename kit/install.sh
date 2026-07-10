#!/bin/bash
# ---------------------------------------------------------------
#  Claude ICA — Designer Kit installer (macOS)
#
#  One command sets up everything:
#    proxy server + claude-ica launcher + Claude Code + MCP servers
#
#  Usage:
#    bash kit/install.sh
#  or remotely:
#    curl -fsSL <raw-url>/kit/install.sh | bash
#
#  Safe to re-run: every step checks before it changes anything.
# ---------------------------------------------------------------

REPO_URL="${CLAUDE_ICA_REPO:-https://github.com/teer823/claude-proxy.git}"
INSTALL_DIR="${CLAUDE_ICA_HOME:-$HOME/claude-proxy}"
BIN_DIR="$HOME/.local/bin"
PROXY_URL="http://localhost:8082"

# When run via `curl | bash`, stdin is the download pipe — questions must be
# answered from the keyboard instead. KIT_FORCE_STDIN=1 overrides for tests.
if [ -n "$KIT_FORCE_STDIN" ] || [ -t 0 ]; then INPUT=/dev/stdin; else INPUT=/dev/tty; fi

BOLD=$(tput bold 2>/dev/null || true)
DIM=$(tput dim 2>/dev/null || true)
RESET=$(tput sgr0 2>/dev/null || true)

STEP=0
step() {
  STEP=$((STEP + 1))
  echo ""
  echo "${BOLD}[$STEP/9] $1${RESET}"
}
ok()   { echo "  ✅ $1"; }
skip() { echo "  ⏭️  $1"; }
warn() { echo "  ⚠️  $1"; }
fail() { echo ""; echo "  ❌ $1"; echo "     $2"; exit 1; }

echo ""
echo "${BOLD}🌞 Claude ICA — Designer Kit${RESET}"
echo "${DIM}   Claude Code ที่วิ่งบน ICA ของบริษัท — ติดตั้งครั้งเดียว ใช้ได้ตลอด${RESET}"
echo "${DIM}   Takes about 3-5 minutes. You only need your ICA API key.${RESET}"

# ---------------------------------------------------------------
step "Checking your Mac has the basics (เช็คของพื้นฐานในเครื่อง)"

if ! command -v git >/dev/null 2>&1; then
  fail "Git is not installed yet." \
       "Run:  xcode-select --install   then click Install, wait, and run this installer again."
fi
ok "git"

if ! command -v python3 >/dev/null 2>&1; then
  fail "Python 3 is not installed yet." \
       "Run:  xcode-select --install   then run this installer again."
fi
ok "python3 ($(python3 -V 2>&1 | cut -d' ' -f2))"

# ---------------------------------------------------------------
step "Getting the proxy code (ดาวน์โหลดตัวแปลภาษา)"

if [ -d "$INSTALL_DIR/.git" ]; then
  skip "already downloaded at $INSTALL_DIR — updating instead"
  git -C "$INSTALL_DIR" pull --ff-only 2>/dev/null || warn "couldn't auto-update (that's fine, continuing)"
else
  git clone --quiet "$REPO_URL" "$INSTALL_DIR" || fail "Couldn't download the code." \
    "Check your internet/VPN connection and try again."
  ok "downloaded to $INSTALL_DIR"
fi

cd "$INSTALL_DIR" || exit 1

# ---------------------------------------------------------------
step "Preparing the engine (ติดตั้ง engine ภายใน)"

if [ -x .venv/bin/python ]; then
  skip "engine already prepared"
else
  python3 -m venv .venv || fail "Couldn't create the Python environment." "Try running: python3 -m venv $INSTALL_DIR/.venv"
  ok "environment created"
fi
.venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt \
  || fail "Couldn't install dependencies." "Check your internet/VPN and re-run this installer."
ok "dependencies installed"

# ---------------------------------------------------------------
step "Your ICA key (กุญแจ ICA ของคุณ)"

if [ -f .env ] && ! grep -q "your-api-key-here" .env; then
  skip ".env already configured — keeping your existing key"
else
  echo ""
  echo "  Everyone with an IBMDT email has ICA access:"
  echo "  เปิดเว็บ ICA → login ด้วย IBMDT email → copy API key ของคุณมาได้เลย"
  echo ""
  printf "  Paste your ICA API key here (จะไม่โชว์บนจอ): "
  read -rs ICA_KEY < "$INPUT"
  echo ""
  if [ -z "$ICA_KEY" ]; then
    fail "No key entered." "Re-run this installer when you have your ICA key ready."
  fi
  cat > .env <<ENVEOF
OPENAI_BASE_URL=https://sg.ica.ibm.com/ica/apis/v3
OPENAI_API_KEY=$ICA_KEY
DEFAULT_MODEL=global/anthropic.claude-sonnet-4-6

# Cheap model for background chores (auto-selected, saves company quota)
SMALL_MODEL=global/anthropic.claude-haiku-4-5-20251001-v1:0

WEB_SEARCH_PROVIDER=duckduckgo
TAVILY_API_KEY=
PROXY_API_KEY=
DEBUG_MODE=false
DEBUG_LOG_DIR=logs
ENVEOF
  chmod 600 .env
  ok "key saved (only on this machine, never leaves it)"
fi

# ---------------------------------------------------------------
step "Installing the claude-ica command (ติดตั้งคำสั่ง claude-ica)"

mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/claude-ica" <<'LAUNCHEOF'
#!/bin/zsh
# claude-ica — launch Claude Code through the local ICA proxy.
#
# Usage:
#   claude-ica              normal launch (auto-starts proxy if down)
#   claude-ica restart      force-restart the proxy first, then launch
#   claude-ica restart -q   restart the proxy only

PROXY_DIR="__INSTALL_DIR__"
PROXY_URL="http://localhost:8082"

proxy_up() {
  curl -s -m 2 "$PROXY_URL/health" >/dev/null 2>&1
}

start_proxy() {
  if [ ! -x "$PROXY_DIR/.venv/bin/python" ]; then
    echo "claude-ica: proxy not found at $PROXY_DIR — re-run the installer" >&2
    exit 1
  fi
  cd "$PROXY_DIR" || exit 1
  mkdir -p logs
  nohup .venv/bin/python main.py >> logs/proxy-launch.log 2>&1 &
  disown
  for i in {1..10}; do
    sleep 1
    proxy_up && break
  done
  if ! proxy_up; then
    echo "claude-ica: proxy failed to start — check $PROXY_DIR/logs/proxy-launch.log" >&2
    exit 1
  fi
  echo "claude-ica: proxy is up at $PROXY_URL"
}

if [ "$1" = "restart" ]; then
  shift
  echo "claude-ica: restarting proxy..."
  pkill -if "python main.py" 2>/dev/null
  sleep 1
  start_proxy
  if [ "$1" = "-q" ]; then
    exit 0
  fi
elif ! proxy_up; then
  echo "claude-ica: proxy not running — starting it..."
  start_proxy
fi

export ANTHROPIC_BASE_URL="$PROXY_URL"
export ANTHROPIC_API_KEY="any-key"
export ANTHROPIC_MODEL="global/anthropic.claude-sonnet-4-6"
exec claude "$@"
LAUNCHEOF
# Point the launcher at wherever this install actually lives
sed -i '' "s|__INSTALL_DIR__|$INSTALL_DIR|" "$BIN_DIR/claude-ica"
chmod +x "$BIN_DIR/claude-ica"
ok "claude-ica command installed"

# ---------------------------------------------------------------
step "Making the command findable (สอน Terminal ให้รู้จักคำสั่งใหม่)"

ZSHRC="$HOME/.zshrc"
touch "$ZSHRC"
if grep -q '\.local/bin' "$ZSHRC"; then
  skip "Terminal already knows where to look"
else
  printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$ZSHRC"
  ok "added to $ZSHRC (new Terminal windows will pick this up automatically)"
fi
export PATH="$BIN_DIR:$PATH"

# ---------------------------------------------------------------
step "Claude Code itself (ตัวแอป Claude Code)"

if command -v claude >/dev/null 2>&1 || [ -x "$BIN_DIR/claude" ]; then
  skip "already installed ($("$BIN_DIR/claude" --version 2>/dev/null || claude --version 2>/dev/null))"
else
  echo "  ${DIM}downloading Claude Code (official installer)...${RESET}"
  curl -fsSL https://claude.ai/install.sh | bash >/dev/null 2>&1 \
    || fail "Couldn't install Claude Code." "Check internet/VPN, or install manually from https://claude.com/claude-code"
  ok "Claude Code installed"
fi

# ---------------------------------------------------------------
step "Connecting design tools — Figma & Atlassian (ต่อ Figma กับ Jira/Confluence)"

CLAUDE_BIN="$BIN_DIR/claude"
command -v claude >/dev/null 2>&1 && CLAUDE_BIN="$(command -v claude)"

register_mcp() {
  # $1 = name, $2 = transport, $3 = url
  if "$CLAUDE_BIN" mcp get "$1" >/dev/null 2>&1; then
    skip "$1 already connected"
  else
    "$CLAUDE_BIN" mcp add --transport "$2" --scope user "$1" "$3" >/dev/null 2>&1 \
      && ok "$1 registered" \
      || warn "$1 registration failed — you can add it later"
  fi
}

register_mcp figma                    http https://mcp.figma.com/mcp
register_mcp atlassian-ktbinnovation  http https://mcp.atlassian.com/v1/mcp/authv2
register_mcp atlassian-krungthaibank  http https://mcp.atlassian.com/v1/mcp/authv2

echo "  ${DIM}(you'll log into Figma/Atlassian once, in your browser — see final steps)${RESET}"

# ---------------------------------------------------------------
step "Test drive (ลองสตาร์ทเครื่อง)"

if curl -s -m 2 "$PROXY_URL/health" >/dev/null 2>&1; then
  skip "proxy already running"
else
  cd "$INSTALL_DIR" && mkdir -p logs
  nohup .venv/bin/python main.py >> logs/proxy-launch.log 2>&1 &
  disown
  sleep 3
fi

if curl -s -m 5 "$PROXY_URL/health" >/dev/null 2>&1; then
  ok "proxy is alive"
  REPLY=$(curl -s -m 45 "$PROXY_URL/v1/messages" \
    -H "Content-Type: application/json" \
    -d '{"model":"m","max_tokens":25,"messages":[{"role":"user","content":"Say exactly: WELCOME ABOARD"}]}' \
    | python3 -c "import sys,json
try: print(json.load(sys.stdin)['content'][0]['text'][:40])
except Exception: print('')" 2>/dev/null)
  if [ -n "$REPLY" ]; then
    ok "ICA answered: \"$REPLY\" 🎉"
  else
    warn "Proxy runs, but ICA didn't answer. Usually this means:"
    echo "     • not on the company network/VPN, or"
    echo "     • the API key isn't right (edit $INSTALL_DIR/.env and re-run)"
  fi
else
  warn "Proxy didn't start — check $INSTALL_DIR/logs/proxy-launch.log"
fi

# ---------------------------------------------------------------
echo ""
echo "${BOLD}🎨 Last step — create your AI buddy${RESET}"
echo ""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
BUDDY_SCRIPT="$SCRIPT_DIR/setup-buddy.sh"
[ -f "$BUDDY_SCRIPT" ] || BUDDY_SCRIPT="$INSTALL_DIR/kit/setup-buddy.sh"
if [ -f "$BUDDY_SCRIPT" ]; then
  printf "  Design your buddy's personality now? (สร้างเพื่อน AI ของคุณเลยมั้ย) [Y/n] "
  read -r DO_BUDDY < "$INPUT"
  case "$DO_BUDDY" in
    [Nn]*) echo "  no problem — run it anytime:  bash $BUDDY_SCRIPT" ;;
    *)     bash "$BUDDY_SCRIPT" ;;
  esac
fi

# ---------------------------------------------------------------
echo ""
echo "${BOLD}✨ Done! Here's how to start:${RESET}"
echo ""
echo "  1. Open a ${BOLD}new${RESET} Terminal window  ${DIM}(สำคัญ! หน้าต่างใหม่เท่านั้น)${RESET}"
echo "  2. Type:  ${BOLD}claude-ica${RESET}"
echo "  3. First time only — type ${BOLD}/mcp${RESET} inside, then Authenticate:"
echo "       • figma                   → log in with your Figma account"
echo "       • atlassian-ktbinnovation → log in, pick ${BOLD}ktbinnovation${RESET}"
echo "       • atlassian-krungthaibank → log in, pick ${BOLD}krungthaibank${RESET}"
echo "  4. Say hi to your new buddy~ 🌞"
echo ""
echo "  ${DIM}Something weird later? Try:  claude-ica restart${RESET}"
echo ""
