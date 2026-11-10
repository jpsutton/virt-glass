#!/usr/bin/env bash
# Static checks that need no build: shell scripts, Python, the desktop entry and
# the PKGBUILD. CI runs this on every pull request.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 1
failures=0
check() {
  local label=$1 out
  shift
  if ! out=$("$@" 2>&1); then
    echo "FAIL $label"
    printf '    %s\n' "${out//$'\n'/$'\n'    }"
    failures=$((failures + 1))
  else
    echo "ok   $label"
  fi
}
for f in $(git ls-files '*.sh') virt-glass; do
  check "bash -n $f" bash -n "$f"
  check "shellcheck $f" shellcheck -x "$f"
done
check "ruff (pyflakes rules)" ruff check --select F virtglass tools
check "compileall" python -m compileall -q virtglass tools
check "desktop-file-validate" desktop-file-validate virt-glass.desktop
check "PKGBUILD" bash -c 'cd packaging/arch && makepkg --printsrcinfo >/dev/null'
exit $((failures > 0))
