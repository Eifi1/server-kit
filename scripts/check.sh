#!/usr/bin/env bash
# The whole gate — what CI runs, and what runs on this machine before anything is pushed.
# One script, called by both, so the local gate and CI cannot drift (keksdose's
# scripts/check-backend.sh, same idea). Every step blocks.
set -euo pipefail
cd "$(dirname "$0")/.."

# `--locked`, not `--frozen`: fail when pyproject.toml and uv.lock disagree instead of
# silently installing from a stale lock.
uv sync --locked
# Known vulnerabilities in the lock — every package, the extras and the dev group — fail
# the gate, as the ui-kit's `npm audit --audit-level=high` does. uv's own audit (OSV; uv
# 0.12 and later, still a preview, hence the flag): no new dependency, and it reads the
# lock as it is. A vulnerability with no fix yet that is accepted on purpose goes behind
# `--ignore-until-fixed <ID>` here, with the reason beside it. Dependabot's open alerts are
# scripts/check-dependencies.py's, before a push; this runs on every check, CI's too.
uv audit --locked --preview-features audit-command
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q --cov --cov-report=term-missing
# The artefact an app installs: build it, so a packaging mistake fails here and not in an app.
rm -rf dist
uv build --quiet
# The API for the ui-kit showcase's "Server kit" group, beside the wheel: release.yml
# uploads dist/*, so every release carries the api.json the showcase pins.
uv run python scripts/export_api.py
