#!/usr/bin/env bash
set -e

VENV="./ConversionTemp/venvs/gapless-mp3"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$SCRIPT_DIR/gapless_mp3_reencode.py"

# Create venv if missing
if [ ! -d "$VENV" ]; then
    echo "Creating virtual environment at $VENV"
    python3 -m venv "$VENV"
fi

# Activate venv
source "$VENV/bin/activate"

# Ask if first time (default yes)
echo "Is it the first time you are running this script? [y/N]"
read -r answer
if [ -z "$answer" ]; then
    answer="n"
fi
answer=$(echo "$answer" | tr 'A-Z' 'a-z')

if [ "$answer" = "y" ] || [ "$answer" = "yes" ]; then
    # First time: upgrade pip if needed and install dependencies if missing
    if ! pip list --outdated | grep -q pip; then
        :  # Pip is up to date, skip
    else
        python -m pip install --upgrade pip >/dev/null
    fi

    # Install dependencies only if either is missing
    if ! pip show tqdm >/dev/null 2>&1 || ! pip show mutagen >/dev/null 2>&1; then
        python -m pip install tqdm mutagen >/dev/null
    fi
fi
# If no (not first time), skip the above and just run

# Run the script (pass all args through)
python "$SCRIPT" "$@"

# Deactivate explicitly (optional, happens anyway)
deactivate