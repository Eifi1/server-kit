#!/usr/bin/env bash
# The whole gate — what CI runs, and what runs on this machine before anything is pushed.
# One script, called by both, so the local gate and CI cannot drift (keksdose's
# scripts/check-backend.sh, same idea). Every step blocks.
set -euo pipefail
cd "$(dirname "$0")/.."

# `--locked`, not `--frozen`: fail when pyproject.toml and uv.lock disagree instead of
# silently installing from a stale lock.
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q --cov --cov-report=term-missing
# The artefact an app installs: build it, so a packaging mistake fails here and not in an app.
rm -rf dist
uv build --quiet
