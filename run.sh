#!/bin/bash
# Task implementations; the Makefile targets are one-line wrappers (same convention as cookiecutter-dbxops).
set -e

THIS_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
MINIMUM_TEST_COVERAGE_PERCENT=90

function install {
    python -m pip install --upgrade pip
    python -m pip install --editable "$THIS_DIR/[dev]"
}

function lint {
    pre-commit run --all-files
}

function lint:ci {
    SKIP=no-commit-to-branch pre-commit run --all-files
}

# pytest with coverage; fails below MINIMUM_TEST_COVERAGE_PERCENT (raise it, never lower it)
function test {
    rm -rf "$THIS_DIR/test-reports" && mkdir -p "$THIS_DIR/test-reports"
    python -m pytest "${@:-$THIS_DIR/tests/}" \
        --cov --cov-report term --cov-report "xml:$THIS_DIR/test-reports/coverage.xml" \
        --junit-xml "$THIS_DIR/test-reports/report.xml" \
        --cov-fail-under "$MINIMUM_TEST_COVERAGE_PERCENT"
}

function build {
    python -m build --wheel
}

# list PLACEHOLDER-<what> values; --strict fails while any remain (every deploy runs it)
function check-placeholders {
    local found
    found="$(grep -rnoE 'PLACEHOLDER-[a-z0-9-]+' "$THIS_DIR/databricks.yml" "$THIS_DIR/resources" | sed "s|^$THIS_DIR/||" || true)"
    if [ -z "$found" ]; then
        echo "no placeholders found"
        return 0
    fi
    echo "$found"
    echo "$(echo "$found" | wc -l) placeholder(s) found"
    if [ "${1:-}" == "--strict" ]; then
        return 1
    fi
}

function validate-bundle {
    databricks bundle validate -t "${1:?usage: ./run.sh validate-bundle <target>}"
}

function help {
    echo "$0 <task> <args>"
    compgen -A function | grep -v '^_' | cat -n
}

TIMEFORMAT="Task completed in %3lR"
time "${@:-help}"
