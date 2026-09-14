#!/usr/bin/env bash
# One command to verify the refactor/hardening branch.
# Run from anywhere:  ./scripts/verify.sh
# Exits non-zero if any check fails.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
results=()

run() {
  local name="$1"; shift
  printf '\n\033[1m== %s ==\033[0m\n' "$name"
  if "$@"; then results+=("PASS  $name"); else results+=("FAIL  $name"); fi
}

# TEMPORARY: the app currently breaks all auth when SECRET_KEY is unset
# (a new random key is generated on every use). Step 4 fixes that bug and
# removes this line. Until then the test suite needs a key to get past login.
export SECRET_KEY="${SECRET_KEY:-verify-only-test-key-not-for-production}"

run "backend tests" \
  bash -c "cd '$ROOT/backend' && .venv/bin/python -m pytest tests -q -o timeout=120 -p no:cacheprovider -rs"

run "frontend unit tests" \
  bash -c "cd '$ROOT/frontend' && npx vitest run"

run "frontend type check" \
  bash -c "cd '$ROOT/frontend' && ./node_modules/.bin/tsc --noEmit && echo 'no type errors'"

# Build into a temp dir: a frontend/dist folder changes how the backend serves "/".
run "frontend build" \
  bash -c "out=\$(mktemp -d) && cd '$ROOT/frontend' && npx vite build --outDir \"\$out\" --emptyOutDir >/dev/null && echo 'build ok' && rm -rf \"\$out\""

printf '\n\033[1m== summary ==\033[0m\n'
printf '%s\n' "${results[@]}"
printf '%s\n' "${results[@]}" | grep -q '^FAIL' && exit 1 || exit 0
