#!/usr/bin/env bash
# Sync this worktree + its two editable deps to a worker VM and `uv sync`
# there. Captures the e2e-v6 setup deviations from the CLAUDE.md baseline
# so a fresh worker is one command:
#
#   scripts/sync-worker.sh <vm>
#
# Deviations from a plain `rsync ./ <vm>:~/GitHub/<repo>/`:
#
# - rsync `src/` as a directory (not flattened) plus the handful of
#   packaging files, instead of the whole tree with excludes. The full-
#   tree rsync drags `frontend-wb/node_modules`, `.venv`, `logs/` etc.
#   and — for the deps — `.git/` (which we deliberately omit, see next).
# - ship `pyproject.toml` / `README.md` / `requirements*.txt` for all
#   three repos: the editable installs read them at build time.
# - `SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0`: `inspect_ai` uses
#   setuptools-scm; without `.git/` on the worker it can't derive a
#   version and the editable build fails.
# - `uv sync --frozen`: honour the shipped `uv.lock` instead of
#   re-resolving. A re-resolve on the worker previously failed against
#   the editable inspect_ai fork's `0.1.dev1+…` scm version (see
#   `pyproject.toml` note re: `exclude-newer-package`).
set -euo pipefail

vm=${1:?usage: scripts/sync-worker.sh <vm>}

here=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
inspect=$HOME/GitHub/inspect_ai
petri=$HOME/GitHub/petri-meridian

# Remote layout mirrors [tool.uv.sources]: collaborative-auditor sits next
# to `../inspect_ai` and `../petri-meridian`.
r_root='~/GitHub'
r_here=$r_root/collaborative-auditor
r_inspect=$r_root/inspect_ai
r_petri=$r_root/petri-meridian

sync_repo() {
  local src=$1 dst=$2
  echo "→ $dst"
  wssh "$vm" "mkdir -p $dst"
  rsync -az --delete "$src/src/" "$vm:$dst/src/"
  # packaging metadata the editable build reads; requirements*.txt may not
  # exist (petri has none) — expand locally and skip if empty.
  local extras=("$src/pyproject.toml" "$src/README.md")
  for f in "$src"/requirements*.txt; do [[ -e $f ]] && extras+=("$f"); done
  rsync -az "${extras[@]}" "$vm:$dst/"
}

sync_repo "$here"    "$r_here"
sync_repo "$inspect" "$r_inspect"
sync_repo "$petri"   "$r_petri"

# uv.lock only for the top-level project (deps are editable, not locked).
rsync -az "$here/uv.lock" "$vm:$r_here/"

echo "→ uv sync --frozen"
wssh "$vm" "cd $r_here && SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 uv sync --frozen"

echo "ok: $vm:$r_here"
