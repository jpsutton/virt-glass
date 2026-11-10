#!/usr/bin/env bash
# Set up the venv, build the native shim and install a desktop entry.
set -e
here="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$here"
[ -d .venv ] || python3 -m venv --system-site-packages .venv
.venv/bin/pip install -q -r requirements.txt
make -s -C native -j"$(nproc)"
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$apps"
sed "s|@PREFIX@|$here|" virt-glass.desktop > "$apps/virt-glass.desktop"
echo "Installed. Launch 'Virt Glass' from your menu or run $here/virt-glass"
