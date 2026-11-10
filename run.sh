#!/usr/bin/env bash
# Runs the tool with the Python of the runner. Nothing is installed: the tool uses only the
# standard library. Usage: run.sh <command> [args]
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

candidates=(python3 python)
case "${RUNNER_OS:-$(uname -s)}" in
  Windows | MINGW* | MSYS* | CYGWIN*) candidates=(python python3) ;;
esac

for candidate in "${candidates[@]}"; do
  if command -v "$candidate" > /dev/null 2>&1 \
    && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' > /dev/null 2>&1; then
    # -I: ignore PYTHON* variables and the user site, and keep the working directory out of the
    # import path, so files in the checked out repository can't shadow modules
    exec "$candidate" -I -X utf8 "$root/goproxy_gh_pages" "$@"
  fi
done

echo "::error::goproxy-gh-pages needs Python 3.10 or newer. Use actions/setup-python on runners without it." >&2
exit 1
