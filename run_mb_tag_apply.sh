#!/bin/sh
set -eu

# ============================================================
# MusicBrainz Tag Apply Runner (WSL-safe, user-friendly)
#
# What it does:
# - Ensures required system dependencies exist (auto-installs on Debian/Ubuntu/WSL)
# - Creates/uses a venv in Linux filesystem (~/.venvs/mb_tag_apply/venv)
# - Installs required Python packages (mutagen, tqdm) into the venv
# - Runs mb_tag_apply.py on a target folder (recursive)
#
# Guarantees:
# - NO moving/renaming/copying
# - NO audio re-encoding (tags + optional cover only)
# ============================================================

# ---------- helpers ----------
have_cmd() { command -v "$1" >/dev/null 2>&1; }

is_debian_like() {
  [ -f /etc/debian_version ] && have_cmd apt-get
}

need_sudo() {
  # true if we are not root
  [ "$(id -u)" -ne 0 ]
}

apt_install_if_missing() {
  # Usage: apt_install_if_missing <pkg> <check_cmd>
  PKG="$1"
  CHECK="$2"

  if have_cmd "$CHECK"; then
    return 0
  fi

  if ! is_debian_like; then
    echo "ERROR: Missing required command '$CHECK' and this system is not Debian/Ubuntu with apt-get."
    echo "Please install package '$PKG' manually."
    exit 1
  fi

  echo "== Installing missing dependency: $PKG (for '$CHECK') =="

  if need_sudo; then
    if ! have_cmd sudo; then
      echo "ERROR: 'sudo' is not available but root privileges are needed to install packages."
      echo "Run this script as root or install '$PKG' manually."
      exit 1
    fi
    sudo apt-get update -y
    sudo apt-get install -y "$PKG"
  else
    apt-get update -y
    apt-get install -y "$PKG"
  fi
}

# ---------- locate script dir ----------
SCRIPT_PATH="$0"
case "$SCRIPT_PATH" in
  /*) ;;
  *) SCRIPT_PATH="$(pwd)/$SCRIPT_PATH" ;;
esac
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"

PY_SCRIPT="$SCRIPT_DIR/mb_tag_apply.py"

if [ ! -f "$PY_SCRIPT" ]; then
  echo "ERROR: Cannot find mb_tag_apply.py in:"
  echo "  $SCRIPT_DIR"
  echo ""
  echo "Fix: Put run_mb_tag_apply.sh in the same folder as mb_tag_apply.py"
  exit 1
fi

# ---------- choose target folder ----------
TARGET_ROOT="${1:-}"

if [ -z "$TARGET_ROOT" ]; then
  echo ""
  echo "Where are your MP3 album folders?"
  echo "Example: /mnt/c/Users/user/Music/MP3"
  printf "Enter path: "
  # shellcheck disable=SC2162
  read TARGET_ROOT
fi

# Expand ~ manually (POSIX sh friendly)
case "$TARGET_ROOT" in
  "~" ) TARGET_ROOT="$HOME" ;;
  "~/"*) TARGET_ROOT="$HOME/${TARGET_ROOT#~/}" ;;
esac

if [ ! -d "$TARGET_ROOT" ]; then
  echo ""
  echo "ERROR: That folder does not exist:"
  echo "  $TARGET_ROOT"
  exit 1
fi

# ---------- user agent ----------
DEFAULT_UA="mb-tag-apply/1.0 (local-script)"
USER_AGENT="${USER_AGENT:-$DEFAULT_UA}"

# ---------- system dependency checks + auto-install ----------
# python3
apt_install_if_missing "python3" "python3"

# venv support (python3 -m venv)
# Debian/Ubuntu package is python3-venv
# We'll check by attempting to import venv module quickly.
if ! python3 -c "import venv" >/dev/null 2>&1; then
  apt_install_if_missing "python3-venv" "python3"
fi

# pip support (ensurepip)
if ! python3 -c "import ensurepip" >/dev/null 2>&1; then
  apt_install_if_missing "python3-venv" "python3"
fi

# ca-certificates helps TLS downloads (common on minimal installs)
apt_install_if_missing "ca-certificates" "update-ca-certificates"

# ---------- venv location (WSL-safe, not in /mnt/c) ----------
VENV_BASE="$HOME/.venvs/mb_tag_apply"
VENV_DIR="$VENV_BASE/venv"
ACTIVATE="$VENV_DIR/bin/activate"
PY_BIN="$VENV_DIR/bin/python3"

echo ""
echo "== MusicBrainz Tagger Runner =="
echo "Script:  $PY_SCRIPT"
echo "Target:  $TARGET_ROOT"
echo "UA:      $USER_AGENT"
echo "Venv:    $VENV_DIR"
echo ""

# ---------- create venv if missing ----------
if [ ! -f "$ACTIVATE" ]; then
  echo "== Creating Python virtual environment (WSL-safe location) =="
  mkdir -p "$VENV_BASE"

  python3 -m venv "$VENV_DIR" || {
    echo ""
    echo "ERROR: Failed to create venv at:"
    echo "  $VENV_DIR"
    echo ""
    echo "Try running manually:"
    echo "  python3 -m venv \"$VENV_DIR\""
    echo ""
    echo "If it still fails, your Python install may be broken."
    exit 1
  }
fi

# ---------- activate venv ----------
# shellcheck disable=SC1090
. "$ACTIVATE"

# ---------- ensure python packages ----------
echo "== Ensuring Python packages (mutagen, tqdm, requests) in venv =="
# Upgrade pip quietly; if it fails, continue (some environments restrict it)
"$PY_BIN" -m pip install --upgrade pip >/dev/null 2>&1 || true

# Install requirements
"$PY_BIN" -m pip install -q mutagen tqdm requests || {
  echo ""
  echo "ERROR: Failed to install Python dependencies in the venv."
  echo "Try manually:"
  echo "  $PY_BIN -m pip install mutagen tqdm"
  deactivate || true
  exit 1
}

# ---------- run the python script ----------
echo ""
echo "== Running mb_tag_apply.py =="
echo "Tip: it will process folders one-by-one and prompt you each time."
echo ""

"$PY_BIN" "$PY_SCRIPT" "$TARGET_ROOT" --user-agent "$USER_AGENT"
RETVAL=$?

# ---------- deactivate + exit ----------
deactivate || true
echo ""
echo "== Finished (exit code $RETVAL) =="
exit $RETVAL
