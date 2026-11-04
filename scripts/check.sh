#!/usr/bin/env bash
# The whole gate — what CI runs, and what runs on this machine before anything is pushed.
# One script, called by both, so the local gate and CI cannot drift (keksdose's
# scripts/check-backend.sh, same idea). Every step blocks.
set -euo pipefail
cd "$(dirname "$0")/.."

# `--locked`, not `--frozen`: fail when pyproject.toml and uv.lock disagree instead of
# silently installing from a stale lock.
uv sync --locked
# Known vulnerabilities in the lock — every package, the extras and the dev group — block on
# high and critical, as the ui-kit's `npm audit --audit-level=high` does; below that each
# prints one line and passes. `uv audit` (OSV; uv 0.12 and later, a preview) has no level
# option, so scripts/audit_dependencies.py reads its JSON, looks each severity up on OSV
# (no severity anywhere counts as high) and fails the step whenever uv's report is not one
# it can read. An advisory accepted on purpose while it has no fix goes in that script's
# IGNORE_UNTIL_FIXED (uv's --ignore-until-fixed), with its reason. Dependabot's open alerts
# are scripts/check-dependencies.py's, before a push; this runs on every check, CI's too.
uv run --no-sync python -I scripts/audit_dependencies.py
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
