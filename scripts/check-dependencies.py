"""Dependabot, locally: run by .githooks/pre-push (Marcel, 2026-10-16: "Copy the Keksdose
approach"; keksdose's scripts/check-dependencies.py, for uv alone).

Two halves, with different weights:

1. GitHub's open Dependabot ALERTS (a vulnerable dependency) BLOCK the push: one
   `gh api` call. Offline, or without a `gh` login, it warns and lets the push through.
2. Newer minor and patch versions of the DIRECT dependencies (pyproject.toml's runtime
   floors, extras and dev groups) are a REPORT only: something upstream released this
   morning must not stop a push. Majors are left out, as in dependabot.yml.

This replaces Dependabot's uv version-update PRs, switched off in .github/dependabot.yml
(keksdose measured its Dependabot CI at 43 % of its Actions minutes). The GitHub Actions
pins keep their monthly Dependabot PR: no local tool lists those. The hook is enabled per
clone with `git config core.hooksPath .githooks`.

Exit 1 only for an open alert. `SKIP_DEPENDENCY_CHECK=1` skips the whole check.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "Eifi1/server-kit"

#: Held on purpose: name → the first version NOT to propose. None today.
HOLDS: dict[str, str] = {}


def _run(cmd: list[str], cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120, check=False
    )


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", text)[:3])


def _major(text: str) -> int:
    # The first number, as Dependabot's semver-major ignore reads it: a 0.x minor is an
    # update here (FastAPI's own hold is in HOLDS).
    parts = _version(text)
    return parts[0] if parts else 0


def _held(name: str, current: str, latest: str) -> bool:
    hold = HOLDS.get(name)
    if hold is None:
        return False
    if hold == "0.0":
        return True
    return _version(latest) >= _version(hold) > _version(current)


def alerts() -> int:
    result = _run(["gh", "api", f"repos/{REPO}/dependabot/alerts?state=open&per_page=100"])
    if result.returncode != 0:
        print(f"⚠ Dependabot alerts not checked (gh: {result.stderr.strip()[:120] or 'failed'}).")
        return 0
    found = json.loads(result.stdout or "[]")
    if not found:
        print("✓ No open Dependabot alerts.")
        return 0
    print(f"✗ {len(found)} open Dependabot alert(s) — fix before pushing:")
    for a in found:
        dep = a.get("dependency", {}).get("package", {})
        adv = a.get("security_advisory", {})
        fix = (a.get("security_vulnerability") or {}).get("first_patched_version") or {}
        print(
            f"  - {dep.get('ecosystem')} {dep.get('name')}: {adv.get('severity')} — "
            f"{adv.get('summary')} (fixed in {fix.get('identifier', '?')}) {a.get('html_url', '')}"
        )
    return 1


def _direct_python() -> set[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    specs = list(data.get("project", {}).get("dependencies", []))
    for group in data.get("dependency-groups", {}).values():
        specs += [s for s in group if isinstance(s, str)]
    return {re.split(r"[\s\[<>=!~;@]", s, maxsplit=1)[0].lower().replace("_", "-") for s in specs}


def python_updates() -> list[str]:
    result = _run(["uv", "pip", "list", "--outdated", "--format", "json"], cwd=ROOT)
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return ["uv pip list gave no answer"]
    direct = _direct_python()
    lines = []
    for p in sorted(data, key=lambda p: p["name"].lower()):
        name = p["name"].lower().replace("_", "-")
        current, latest = p["version"], p["latest_version"]
        if name not in direct or _major(latest) != _major(current) or _held(name, current, latest):
            continue
        lines.append(f"uv   {name} {current} → {latest}")
    return lines


def main() -> int:
    if os.environ.get("SKIP_DEPENDENCY_CHECK"):
        return 0
    status = alerts()
    try:
        updates = python_updates()
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"⚠ Outdated report skipped ({exc}).")
        updates = []
    if updates:
        print(f"ℹ {len(updates)} minor/patch update(s) available — not blocking; take them in the next round:")
        for line in updates:
            print(f"  {line}")
    else:
        print("✓ Direct dependencies are current (minor/patch).")
    return status


if __name__ == "__main__":
    sys.exit(main())
