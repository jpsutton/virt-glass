#!/usr/bin/env bash
# Prints the release version: pkgver from packaging/arch/PKGBUILD.
set -euo pipefail
cd "$(dirname "$0")/../.."
sed -n 's/^pkgver=//p' packaging/arch/PKGBUILD
