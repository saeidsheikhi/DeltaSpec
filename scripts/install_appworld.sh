#!/usr/bin/env bash
# Install AppWorld for Phase B.
#
# Two things this script must get right, both learned the hard way
# (see notes/FAILURES.md, 2026-08-12):
#
#   1. AppWorld gets its OWN virtualenv. It pins pydantic 1.x, and installing it
#      into the project venv would downgrade pydantic, rich, typer and pytest,
#      putting the environment that produced our results into an unreproducible
#      state.
#   2. `appworld download data` writes to <root>/data and DELETES an existing
#      data/ directory at that root without prompting. `--root` defaults to the
#      current directory, so running it from the repository destroys repo files.
#      The root is therefore always passed explicitly and always lives outside
#      the repository.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APPWORLD_ROOT="${APPWORLD_ROOT:-$HOME/appworld-root}"
APPWORLD_VENV="$REPO_ROOT/.venv-appworld"

case "$APPWORLD_ROOT" in
  "$REPO_ROOT"|"$REPO_ROOT"/*)
    echo "REFUSING: APPWORLD_ROOT ($APPWORLD_ROOT) is inside the repository." >&2
    echo "The downloader deletes <root>/data without asking. Pick a path outside." >&2
    exit 2
    ;;
esac

# AppWorld's data is ~200 MB and unpacking needs headroom.
avail_kb="$(df -Pk "$(dirname "$APPWORLD_ROOT")" | awk 'NR==2 {print $4}')"
if [ "$avail_kb" -lt 2097152 ]; then
  echo "REFUSING: less than 2 GB free at $APPWORLD_ROOT ($((avail_kb / 1024)) MB)." >&2
  exit 2
fi

echo "AppWorld venv : $APPWORLD_VENV"
echo "AppWorld root : $APPWORLD_ROOT"

python3 -m venv "$APPWORLD_VENV"
"$APPWORLD_VENV/bin/pip" install --upgrade pip wheel setuptools
"$APPWORLD_VENV/bin/pip" install appworld

mkdir -p "$APPWORLD_ROOT"
"$APPWORLD_VENV/bin/appworld" install
"$APPWORLD_VENV/bin/appworld" download data --root "$APPWORLD_ROOT"

# `appworld verify` requires an entity argument; bare `appworld verify` is invalid.
( cd "$APPWORLD_ROOT" && "$APPWORLD_VENV/bin/appworld" verify tests ) || {
  echo "WARNING: 'appworld verify tests' did not pass. Investigate before trusting" >&2
  echo "any Phase B number: the official evaluator is our independent ground truth." >&2
}

cat <<EOF

AppWorld installed.
  venv : $APPWORLD_VENV
  root : $APPWORLD_ROOT

Record APPWORLD_ROOT=$APPWORLD_ROOT in .env.

Before writing adapter code, inspect the CURRENT public API rather than assuming it:
  $APPWORLD_VENV/bin/python -c "import appworld; print(dir(appworld))"
Relevant entry points as of 0.1.3.post1: AppWorld, load_task_ids, evaluate_task,
evaluate_tasks, evaluate_dataset, ground_truth, Metric.
EOF
