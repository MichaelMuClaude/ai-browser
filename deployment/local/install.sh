#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
ai-browser local installer

Usage:
  deployment/local/install.sh [options]

Options:
  --install-system-deps     Install apt packages with sudo.
  --install-service         Write ~/.config/systemd/user/ai-browser.service.
  --start-service           Start the systemd user service after installing it.
  --browser-path PATH       Chrome executable path. Default: auto-detect or /usr/bin/google-chrome.
  --host HOST               HTTP bind host. Default: 127.0.0.1.
  --port PORT               HTTP port. Default: 8888.
  --display DISPLAY         X11 DISPLAY value. Default: current DISPLAY or :1.
  --xauthority PATH         XAUTHORITY path. Default: current XAUTHORITY or /run/user/$UID/gdm/Xauthority.
  --skip-tests              Skip unit test verification.
  -h, --help                Show this help.

Examples:
  deployment/local/install.sh --install-system-deps --install-service --start-service
  deployment/local/install.sh --browser-path /usr/bin/google-chrome --port 8890
USAGE
}

INSTALL_SYSTEM_DEPS=0
INSTALL_SERVICE=0
START_SERVICE=0
SKIP_TESTS=0
HOST="127.0.0.1"
PORT="8888"
BROWSER_PATH=""
DISPLAY_VALUE="${DISPLAY:-:1}"
XAUTHORITY_VALUE="${XAUTHORITY:-/run/user/$(id -u)/gdm/Xauthority}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-system-deps)
      INSTALL_SYSTEM_DEPS=1
      shift
      ;;
    --install-service)
      INSTALL_SERVICE=1
      shift
      ;;
    --start-service)
      START_SERVICE=1
      INSTALL_SERVICE=1
      shift
      ;;
    --browser-path)
      BROWSER_PATH="${2:-}"
      shift 2
      ;;
    --host)
      HOST="${2:-}"
      shift 2
      ;;
    --port)
      PORT="${2:-}"
      shift 2
      ;;
    --display)
      DISPLAY_VALUE="${2:-}"
      shift 2
      ;;
    --xauthority)
      XAUTHORITY_VALUE="${2:-}"
      shift 2
      ;;
    --skip-tests)
      SKIP_TESTS=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
VENV_DIR="$PROJECT_DIR/venv"
PYTHON_BIN="$VENV_DIR/bin/python"
PIP_BIN="$VENV_DIR/bin/pip"
ENV_DIR="$HOME/.config/ai-browser"
ENV_FILE="$ENV_DIR/ai-browser.env"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/ai-browser.service"

log() {
  printf '[ai-browser deploy] %s\n' "$*"
}

detect_browser() {
  if [[ -n "$BROWSER_PATH" ]]; then
    printf '%s\n' "$BROWSER_PATH"
    return
  fi
  if command -v google-chrome >/dev/null 2>&1; then
    command -v google-chrome
    return
  fi
  if command -v google-chrome-stable >/dev/null 2>&1; then
    command -v google-chrome-stable
    return
  fi
  if command -v chromium >/dev/null 2>&1; then
    command -v chromium
    return
  fi
  if command -v microsoft-edge >/dev/null 2>&1; then
    command -v microsoft-edge
    return
  fi
  printf '/usr/bin/google-chrome\n'
}

BROWSER_PATH="$(detect_browser)"

log "Project: $PROJECT_DIR"
log "Python venv: $VENV_DIR"
log "Browser: $BROWSER_PATH"
log "HTTP: $HOST:$PORT"
log "DISPLAY: $DISPLAY_VALUE"
log "XAUTHORITY: $XAUTHORITY_VALUE"

if [[ "$INSTALL_SYSTEM_DEPS" -eq 1 ]]; then
  log "Installing system dependencies with apt."
  sudo apt update
  sudo apt install -y python3 python3-venv xdotool imagemagick x11-apps curl
else
  log "Skipping apt install. Use --install-system-deps on fresh machines."
fi

if [[ ! -x "$BROWSER_PATH" ]]; then
  log "WARNING: Browser path is not executable: $BROWSER_PATH"
  log "Install Chrome or rerun with --browser-path PATH."
fi

if ! command -v xdotool >/dev/null 2>&1; then
  log "WARNING: xdotool not found. Native OS fallback will not work."
fi

log "Creating/updating Python virtualenv."
python3 -m venv "$VENV_DIR"
"$PIP_BIN" install --upgrade pip
"$PIP_BIN" install -r "$PROJECT_DIR/requirements.txt"
"$PIP_BIN" install -e "$PROJECT_DIR[dev]"

log "Writing environment file: $ENV_FILE"
mkdir -p "$ENV_DIR"
cat > "$ENV_FILE" <<EOF
AI_BROWSER_HOST=$HOST
AI_BROWSER_PORT=$PORT
AI_BROWSER_PATH=$BROWSER_PATH
AI_BROWSER_HEADLESS=false
AI_BROWSER_ENGINE=auto
AI_BROWSER_WIDTH=1280
AI_BROWSER_HEIGHT=800
DISPLAY=$DISPLAY_VALUE
XAUTHORITY=$XAUTHORITY_VALUE
EOF

if [[ "$SKIP_TESTS" -eq 0 ]]; then
  log "Running unit tests."
  "$PYTHON_BIN" -m pytest "$PROJECT_DIR/tests/unit" -q
else
  log "Skipping tests."
fi

if [[ "$INSTALL_SERVICE" -eq 1 ]]; then
  log "Installing systemd user service: $SERVICE_FILE"
  mkdir -p "$SERVICE_DIR"
  sed \
    -e "s|{{PROJECT_DIR}}|$PROJECT_DIR|g" \
    -e "s|{{ENV_FILE}}|$ENV_FILE|g" \
    -e "s|{{PYTHON_BIN}}|$PYTHON_BIN|g" \
    "$SCRIPT_DIR/ai-browser.service.template" > "$SERVICE_FILE"

  systemctl --user daemon-reload
  systemctl --user enable ai-browser.service

  if [[ "$START_SERVICE" -eq 1 ]]; then
    log "Starting ai-browser.service."
    systemctl --user restart ai-browser.service
    sleep 2
    systemctl --user --no-pager status ai-browser.service || true
  fi
else
  log "Skipping systemd service install. Use --install-service to enable it."
fi

log "Manual run command:"
log "  set -a && source '$ENV_FILE' && set +a && '$PYTHON_BIN' -m src.main server"
log "Health check:"
log "  curl http://$HOST:$PORT/api/health"

if [[ "$START_SERVICE" -eq 1 ]]; then
  log "Trying health check."
  if command -v curl >/dev/null 2>&1; then
    curl -fsS "http://$HOST:$PORT/api/health" || true
    printf '\n'
  fi
fi

log "Done."
