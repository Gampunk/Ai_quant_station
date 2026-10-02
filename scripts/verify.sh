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

run "backend tests" \
  bash -c "cd '$ROOT/backend' && .venv/bin/python -m pytest tests -q -o timeout=120 -p no:cacheprovider -rs"

run "connector tests, Python 3.14, fake MT5" \
  bash -c "cd '$ROOT/mt5_connector' && .venv/bin/python -m pytest tests -q -p no:cacheprovider"

run "frontend unit tests" \
  bash -c "cd '$ROOT/frontend' && npx vitest run"

run "frontend lint" \
  bash -c "cd '$ROOT/frontend' && npm run --silent lint && echo 'no lint problems'"

run "frontend type check" \
  bash -c "cd '$ROOT/frontend' && ./node_modules/.bin/tsc --noEmit && echo 'no type errors'"

# Build into a temp dir: a frontend/dist folder changes how the backend serves "/".
run "frontend build" \
  bash -c "out=\$(mktemp -d) && cd '$ROOT/frontend' && npx vite build --outDir \"\$out\" --emptyOutDir >/dev/null && echo 'build ok' && rm -rf \"\$out\""

printf '\n\033[1m== summary ==\033[0m\n'
printf '%s\n' "${results[@]}"
printf '%s\n' "${results[@]}" | grep -q '^FAIL' && exit 1 || exit 0
