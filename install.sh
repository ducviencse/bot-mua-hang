#!/usr/bin/env bash
# install.sh — One-time setup for bot-mua-hang (macOS / Linux)
set -e

# ── Colours ──────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[OK]${NC}  $1"; }
info() { echo -e "${YELLOW}[..]${NC}  $1"; }
fail() { echo -e "${RED}[!!]${NC}  $1"; exit 1; }

echo ""
echo "============================================"
echo "   bot-mua-hang — Install & Setup"
echo "============================================"
echo ""

# ── 1. Check Python ───────────────────────────────────────────────
info "Checking Python version..."
if ! command -v python3 &>/dev/null && ! command -v python &>/dev/null; then
  fail "Python not found. Install Python 3.11+ from https://www.python.org/downloads/ and re-run this script."
fi

PYTHON=$(command -v python3 || command -v python)
PY_VERSION=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$("$PYTHON" -c "import sys; print(sys.version_info.major)")
PY_MINOR=$("$PYTHON" -c "import sys; print(sys.version_info.minor)")

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
  fail "Python $PY_VERSION found, but 3.11+ is required. Download from https://www.python.org/downloads/"
fi
ok "Python $PY_VERSION"

# ── 2. Check pip ──────────────────────────────────────────────────
info "Checking pip..."
if ! "$PYTHON" -m pip --version &>/dev/null; then
  fail "pip not found. Run: $PYTHON -m ensurepip --upgrade"
fi
ok "pip available"

# ── 3. Install Python packages ────────────────────────────────────
info "Installing Python packages from requirements.txt..."
"$PYTHON" -m pip install -r requirements.txt --quiet
ok "Python packages installed"

# ── 4. Copy .env if missing ───────────────────────────────────────
if [ ! -f ".env" ]; then
  if [ -f ".env.example" ]; then
    cp .env.example .env
    info "Created .env from .env.example — the setup wizard will guide you through adding your API key."
  fi
fi

# ── Done ──────────────────────────────────────────────────────────
echo ""
echo "============================================"
ok "Installation complete!"
echo "============================================"
echo ""
echo "Starting bot-mua-hang..."
echo "Open http://localhost:8081 in your browser."
echo "The setup wizard will guide you through the rest."
echo ""
"$PYTHON" main.py
