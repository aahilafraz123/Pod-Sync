#!/bin/bash
set -e

# ─────────────────────────────────────────────────────────────
#  Pod-Sync  ·  One-command installer & setup wizard launcher
# ─────────────────────────────────────────────────────────────

# ── Colors ───────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

info()    { echo -e "${CYAN}[Pod-Sync]${NC} $1"; }
success() { echo -e "${GREEN}[Pod-Sync] ✔${NC} $1"; }
warn()    { echo -e "${YELLOW}[Pod-Sync] ⚠${NC} $1"; }
error()   { echo -e "${RED}[Pod-Sync] ✘${NC} $1"; }

# ── Resolve SCRIPT_DIR (works even through symlinks) ─────────
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
    DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
    SOURCE="$(readlink "$SOURCE")"
    [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"

# ── Cleanup handler ──────────────────────────────────────────
SERVER_PID=""
cleanup() {
    if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    # Remove signal file if it exists
    rm -f "$SCRIPT_DIR/.setup-complete"
}
trap cleanup EXIT INT TERM

echo ""
echo -e "${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}${CYAN}  Pod-Sync Installer${NC}"
echo -e "${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# ─────────────────────────────────────────────────────────────
# 1.  Check Python 3.9+
# ─────────────────────────────────────────────────────────────
info "Checking Python version..."

PYTHON_CMD=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        VERSION_OK=$("$cmd" -c "
import sys
print('yes' if sys.version_info >= (3, 9) else 'no')
" 2>/dev/null || echo "no")
        if [ "$VERSION_OK" = "yes" ]; then
            PYTHON_CMD="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    error "Python 3.9 or higher is required but was not found."
    echo ""
    echo -e "  ${BOLD}Install Python:${NC}"
    echo -e "    macOS:   ${YELLOW}brew install python@3.12${NC}"
    echo -e "    Ubuntu:  ${YELLOW}sudo apt install python3.12 python3.12-venv${NC}"
    echo -e "    Fedora:  ${YELLOW}sudo dnf install python3.12${NC}"
    echo -e "    Windows: ${YELLOW}https://www.python.org/downloads/${NC}"
    echo ""
    exit 1
fi

PY_VERSION=$("$PYTHON_CMD" --version 2>&1)
success "Found $PY_VERSION"

# ─────────────────────────────────────────────────────────────
# 2.  Create virtual environment
# ─────────────────────────────────────────────────────────────
VENV_DIR="$SCRIPT_DIR/.venv"

if [ -d "$VENV_DIR" ]; then
    # Validate existing venv is still functional
    if [ -x "$VENV_DIR/bin/python" ] || [ -x "$VENV_DIR/Scripts/python.exe" ]; then
        warn "Virtual environment already exists at .venv/ — reusing it."
    else
        warn "Existing .venv/ appears broken — recreating..."
        rm -rf "$VENV_DIR"
        info "Creating virtual environment..."
        "$PYTHON_CMD" -m venv "$VENV_DIR"
        success "Virtual environment created at .venv/"
    fi
else
    info "Creating virtual environment..."
    "$PYTHON_CMD" -m venv "$VENV_DIR"
    success "Virtual environment created at .venv/"
fi

# ─────────────────────────────────────────────────────────────
# 3.  Activate & install dependencies
# ─────────────────────────────────────────────────────────────
info "Installing dependencies..."

# Determine the right activation path (Unix vs Windows/Git-Bash)
if [ -f "$VENV_DIR/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    VENV_PYTHON="$VENV_DIR/bin/python"
elif [ -f "$VENV_DIR/Scripts/activate" ]; then
    # shellcheck disable=SC1091
    source "$VENV_DIR/Scripts/activate"
    VENV_PYTHON="$VENV_DIR/Scripts/python"
else
    error "Could not locate virtualenv activate script."
    exit 1
fi

pip install -r "$SCRIPT_DIR/requirements.txt" -q
success "Dependencies installed."

# ─────────────────────────────────────────────────────────────
# 4.  Resolve absolute repo path (already done via SCRIPT_DIR)
# ─────────────────────────────────────────────────────────────
REPO_DIR="$SCRIPT_DIR"
success "Repo path: $REPO_DIR"

# ─────────────────────────────────────────────────────────────
# 4b. Install pod-sync CLI command
# ─────────────────────────────────────────────────────────────
CLI_TARGET="$HOME/.local/bin/pod-sync"
mkdir -p "$HOME/.local/bin"
cat > "$CLI_TARGET" << EOF
#!/bin/bash
"$VENV_PYTHON" "$REPO_DIR/server.py" "\$@"
EOF
chmod +x "$CLI_TARGET"

# Remind user to add ~/.local/bin to PATH if not already there
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    warn "Add ~/.local/bin to your PATH to use the pod-sync command:"
    echo -e "    ${YELLOW}echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc${NC}"
    echo -e "    ${YELLOW}source ~/.zshrc${NC}"
fi
success "pod-sync CLI installed at $CLI_TARGET"

# ─────────────────────────────────────────────────────────────
# 5.  Kill anything already on port 7823 & start server
# ─────────────────────────────────────────────────────────────
info "Starting Pod-Sync setup server on localhost:7823..."

# Check if port 7823 is already in use — only kill it if it's actually
# a Pod-Sync server, never an unrelated process.
EXISTING_PID=""
if command -v lsof &>/dev/null; then
    EXISTING_PID=$(lsof -ti tcp:7823 2>/dev/null | head -n1 || true)
elif command -v ss &>/dev/null; then
    EXISTING_PID=$(ss -tlnp 'sport = :7823' 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -n1 || true)
fi

if [ -n "$EXISTING_PID" ]; then
    EXISTING_CMD=$(ps -p "$EXISTING_PID" -o args= 2>/dev/null || true)
    if echo "$EXISTING_CMD" | grep -q "server.py"; then
        warn "A previous Pod-Sync server is running on port 7823 (PID $EXISTING_PID). Stopping it..."
        kill "$EXISTING_PID" 2>/dev/null || true
        sleep 1
        if kill -0 "$EXISTING_PID" 2>/dev/null; then
            kill -9 "$EXISTING_PID" 2>/dev/null || true
        fi
        success "Cleared port 7823."
    else
        error "Port 7823 is in use by another process (PID $EXISTING_PID):"
        echo "    $EXISTING_CMD"
        error "Stop that process and re-run ./install.sh."
        exit 1
    fi
fi

# Remove stale signal file
rm -f "$SCRIPT_DIR/.setup-complete"

# Start server in background
"$VENV_PYTHON" "$SCRIPT_DIR/server.py" --http &
SERVER_PID=$!

# ─────────────────────────────────────────────────────────────
# 6.  Wait for the server to be ready
# ─────────────────────────────────────────────────────────────
info "Waiting for server to start..."
RETRIES=0
MAX_RETRIES=15
while [ $RETRIES -lt $MAX_RETRIES ]; do
    if curl -s -o /dev/null -w '' http://localhost:7823 2>/dev/null; then
        break
    fi
    # Also bail out early if the server process died
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        error "Server process exited unexpectedly."
        exit 1
    fi
    sleep 1
    RETRIES=$((RETRIES + 1))
done

if [ $RETRIES -ge $MAX_RETRIES ]; then
    error "Server did not start within ${MAX_RETRIES}s. Check server.py for errors."
    exit 1
fi

success "Server is running (PID $SERVER_PID)."

# ─────────────────────────────────────────────────────────────
# 7.  Open the setup wizard in the default browser
# ─────────────────────────────────────────────────────────────
URL="http://localhost:7823"
info "Opening setup wizard in your browser..."

if [[ "$OSTYPE" == "darwin"* ]]; then
    open "$URL" 2>/dev/null || true
elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    if grep -qi microsoft /proc/version 2>/dev/null; then
        # WSL
        if command -v wslview &>/dev/null; then
            wslview "$URL" 2>/dev/null || true
        elif command -v cmd.exe &>/dev/null; then
            cmd.exe /c start "$URL" 2>/dev/null || true
        else
            warn "Could not detect a browser opener. Please open $URL manually."
        fi
    else
        if command -v xdg-open &>/dev/null; then
            xdg-open "$URL" 2>/dev/null || true
        else
            warn "xdg-open not found. Please open $URL manually."
        fi
    fi
elif [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" ]]; then
    start "$URL" 2>/dev/null || cmd.exe /c start "$URL" 2>/dev/null || true
else
    warn "Unrecognized OS. Please open $URL manually."
fi

# ─────────────────────────────────────────────────────────────
# 8.  Inform the user
# ─────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}  Setup wizard is open in your browser.${NC}"
echo -e "${BOLD}  Complete the setup there.${NC}"
echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
info "Waiting for setup to complete... (Ctrl+C to abort)"

# ─────────────────────────────────────────────────────────────
# 9.  Poll for completion signal
# ─────────────────────────────────────────────────────────────
while true; do
    if [ -f "$SCRIPT_DIR/.setup-complete" ]; then
        break
    fi
    # If the server died unexpectedly, bail out
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        error "Server process exited before setup was completed."
        exit 1
    fi
    sleep 2
done

# ─────────────────────────────────────────────────────────────
# 10. Cleanup & success
# ─────────────────────────────────────────────────────────────
rm -f "$SCRIPT_DIR/.setup-complete"

kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true
SERVER_PID=""  # Prevent double-kill in trap

echo ""
echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}${GREEN}  ✔  Pod-Sync setup complete!${NC}"
echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
success "You're all set. Happy syncing!"
echo ""
