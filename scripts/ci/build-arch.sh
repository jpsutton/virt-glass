#!/usr/bin/env bash
# CI (build): Arch package via packaging/arch/PKGBUILD, written to out/.
#   docker run --rm -v "$PWD:/src" -w /src archlinux:latest scripts/ci/build-arch.sh
set -euo pipefail

pacman -Syu --noconfirm --needed base-devel git >/dev/null

# makepkg refuses to run as root.
id builder >/dev/null 2>&1 || useradd -m builder
chown -R builder: "$PWD"
# --nodeps: the runtime dependencies are not needed to compile the C shim.
su builder -c 'git config --global --add safe.directory "*" && cd packaging/arch && makepkg -f --nodeps --noconfirm'
mkdir -p out
mv packaging/arch/*.pkg.tar.zst out/
ls -la out
