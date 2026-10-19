"""The commit-message rules are the SHARED ones (ui-kit's scripts/sync-commit-rules.mjs and
check-commit-rules.mjs, for Python): scripts/check-commit-msg.cjs and
scripts/check-pushed-commits.cjs are byte-for-byte copies of Eifi1/shared-workflows'
actions/commit-check/ files, pinned to the commit in scripts/shared-commit-rules.json.
Copies rather than the action because the pre-push hook checks LOCALLY, before CI.

    uv run python -I scripts/sync-commit-rules.py v1.3.0     # take a tag: copy + move the pin
    uv run python -I scripts/sync-commit-rules.py --from ../shared-workflows
                                                             # copy a local checkout (pin untouched)
    uv run python -I scripts/sync-commit-rules.py --check    # the copies match the pin?

`--check` fails when a copy differs from the pinned upstream file (edited here, or the pin
moved without a sync). Offline, or while the pin is still pending a tag, it says so and
passes. Change the rules in shared-workflows, never here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PIN = ROOT / "scripts" / "shared-commit-rules.json"


def _raw(repo: str, commit: str, path: str) -> bytes:
    url = f"https://raw.githubusercontent.com/{repo}/{commit}/{path}"
    with urllib.request.urlopen(url, timeout=8) as res:
        body: bytes = res.read()
        return body


def _tag_commit(repo: str, tag: str) -> str | None:
    out = subprocess.run(
        ["git", "ls-remote", "--tags", f"https://github.com/{repo}.git", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.split("\n")
    refs = [line for line in out if line.strip()]
    if not refs:
        return None
    # An annotated tag lists its object and then `^{}`, the commit; prefer the commit.
    return next((r for r in refs if r.endswith("^{}")), refs[0]).split()[0]


def _is_sha(value: str) -> bool:
    return len(value) == 40 and all(c in "0123456789abcdef" for c in value)


def main(argv: list[str]) -> int:
    pin: dict[str, Any] = json.loads(PIN.read_text())
    files: list[dict[str, str]] = pin["files"]

    if argv[:1] == ["--from"] and len(argv) == 2:
        source = Path(argv[1]).resolve()
        for f in files:
            shutil.copyfile(source / f["path"], ROOT / f["vendoredAs"])
        print(f"✓ copied {len(files)} file(s) from {source}; the pin still says {pin['tag']}")
        return 0

    if argv == ["--check"]:
        if not _is_sha(pin["commit"]):
            print(f"• shared commit rules not verified: the pin is {pin['tag']}, no commit yet")
            return 0
        try:
            upstream = {f["vendoredAs"]: _raw(pin["repo"], pin["commit"], f["path"]) for f in files}
        except OSError as error:
            print(f"• shared commit rules not verified (offline: {error}); CI verifies them")
            return 0
        differ = [name for name, body in upstream.items() if (ROOT / name).read_bytes() != body]
        if differ:
            print(
                f"✖ {', '.join(differ)} differ(s) from {pin['repo']}@{pin['tag']} ({pin['commit'][:8]}).\n"
                "  The commit rules are shared: change them in shared-workflows, then run\n"
                "  uv run python -I scripts/sync-commit-rules.py <tag>",
                file=sys.stderr,
            )
            return 1
        print(f"✓ commit rules match {pin['repo']}@{pin['tag']}")
        return 0

    if len(argv) == 1 and not argv[0].startswith("-"):
        tag = argv[0]
        commit = _tag_commit(pin["repo"], tag)
        if commit is None:
            print(f"✖ no tag {tag} in {pin['repo']}", file=sys.stderr)
            return 1
        for f in files:
            (ROOT / f["vendoredAs"]).write_bytes(_raw(pin["repo"], commit, f["path"]))
        PIN.write_text(json.dumps({**pin, "tag": tag, "commit": commit}, indent=2, ensure_ascii=False) + "\n")
        print(f"✓ the commit rules are now {pin['repo']}@{tag} ({commit[:8]})")
        return 0

    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
