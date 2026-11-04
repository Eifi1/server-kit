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
# prints one line and passes. The kit's own audit (`eifi1_server_kit.dependency_audit`, 0.21), the same
# command every backend's gate runs: `uv audit` read as JSON, each severity looked up on OSV
# (no severity anywhere counts as high), and the step fails whenever uv's report is not one
# it can read. An advisory accepted on purpose while it has no fix is one more
# `--ignore-until-fixed ID="reason"` on the line below. None today. Dependabot's open alerts
# are scripts/check-dependencies.py's, before a push; this runs on every check, CI's too.
uv run --no-sync python -I -m eifi1_server_kit.dependency_audit
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
