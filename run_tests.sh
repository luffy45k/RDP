#!/usr/bin/env bash
# ============================================================================
# mytool test runner — pure stdlib, no pip install needed.
#
#   ./run_tests.sh                 # whole suite
#   ./run_tests.sh -v              # verbose (test names)
#   ./run_tests.sh tests.test_vault              # one module
#   ./run_tests.sh tests.test_healer.TestHealing # one class
#   MYTOOL_TEST_VERBOSE=1 ./run_tests.sh         # also show the tool's output
#
# Tests never touch your real ~/.my_ai_tool: every case runs against a
# throw-away data dir (and a throw-away copy of the repo where code is
# patched by the self-healer).
# ============================================================================
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"

echo "== mytool test suite ($("$PY" -V 2>&1)) =="
if [ $# -gt 0 ]; then
    "$PY" -m unittest "$@"
else
    "$PY" -m unittest discover -s tests -t . "$@"
fi

echo
echo "== core selftest (isolated data dir) =="
TMP_HOME="$(mktemp -d)"
trap 'rm -rf "$TMP_HOME"' EXIT
MYTOOL_DATA_DIR="$TMP_HOME" "$PY" -m my_ai_tool selftest --core

echo
echo "all green ✅"
