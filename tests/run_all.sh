#!/usr/bin/env bash
# Прогон всех тестов проекта. Каждый тест — самостоятельный скрипт
# (python tests/x_test.py), без pytest. PY= позволяет подменить интерпретатор
# (в CI нет .venv): PY=python tests/run_all.sh
set -uo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
fail=0
for t in tests/*_test.py; do
    echo "=== $t"
    "$PY" "$t" || { echo "FAIL $t"; fail=1; }
done
[ "$fail" = 0 ] && echo "ALL TESTS OK"
exit $fail
