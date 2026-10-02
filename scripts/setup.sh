#!/usr/bin/env bash
set -euo pipefail 2>/dev/null || set -eu

# Resolve project root (one level up from the scripts directory)
script_dir="$(cd "$(dirname "$0")" >/dev/null 2>&1 && pwd)"
root_dir="$(cd "$script_dir/.." >/dev/null 2>&1 && pwd)"
cd "$root_dir"

# This script is for the Linux server. Use scripts/setup.bat for local
# development on Windows.

# Choose Python 3 for virtual environment creation
python_bin="$(command -v python3 || true)"

if [ -z "$python_bin" ]; then
    echo "python3 is required to create the virtual environment."
    echo "Install the required Linux packages first, e.g. sudo apt install python3 python3-venv python3-pip."
    exit 1
fi

# Create a virtual environment in `.venv` if it doesn't already exist
venv_dir="${VENV_DIR:-.venv}"
venv_python="$venv_dir/bin/python"

if [ ! -x "$venv_python" ]; then
    echo "Creating virtual environment..."
    "$python_bin" -m venv "$venv_dir"
fi

# Activate the virtual environment
. "$venv_dir/bin/activate"

# Keep packaging tools up-to-date to avoid resolver and SSL issues
echo "Upgrading pip and setuptools..."
"$venv_python" -m pip install --upgrade pip "setuptools<82"

# Install runtime dependencies if present
if [ -f requirements.txt ]; then
    echo "Installing runtime dependencies..."
    "$venv_python" -m pip install -r requirements.txt
fi

# Install development dependencies if present
if [ -f requirements_dev.txt ]; then
    echo "Installing development dependencies..."
    "$venv_python" -m pip install -r requirements_dev.txt
fi

# Delete old migrations
rm -f authentication/migrations/0*
rm -f authorization/migrations/0*
rm -f app/migrations/0*
rm -f filesystem/migrations/0*

# Make migrations
echo "Making migrations..."
"$venv_python" manage.py makemigrations authentication authorization app filesystem

# Migrate database
echo "Migrating database..."
"$venv_python" manage.py migrate

# Remove static files
echo "Removing static files..."
rm -rf staticfiles

echo "Reset completed."
