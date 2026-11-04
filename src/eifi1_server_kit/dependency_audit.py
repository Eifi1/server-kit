"""The dependency audit for every backend's gate: known vulnerabilities in the lock BLOCK on
high and critical, as the ui-kit's ``npm audit --audit-level=high`` does; below that they are
printed and pass. Standard library only.

Marcel, 2026-11-04: "Same as ui-kit would be good for harmonization", and what a second app
will need ships in the kit. A bare ``uv audit`` fails on every finding, whatever its
severity, and has no level option, and ``pip-audit`` (the apps' ``check:security:backend``)
neither: a dev-only moderate advisory with no fix yet (the minimatch one that blocked a
ui-kit push) would stop every push and every release. Run it from the directory that holds
the app's ``uv.lock``::

    uv run --no-sync python -I -m eifi1_server_kit.dependency_audit
    uv run --no-sync python -I -m eifi1_server_kit.dependency_audit --ignore-until-fixed GHSA-xxxx-xxxx-xxxx="dev only, no fix"

**Exit 0**: nothing high or critical. **Exit 1**: a finding blocks. **Exit 2**: the audit
could not be read, or the tool is broken (a bad flag included) — never a pass.

1. **The findings** are ``uv audit --locked`` over the whole lock (runtime, extras, the dev
   groups), as JSON. ``uv audit`` and its JSON are uv previews (uv 0.12), so the report is
   read strictly: a missing subcommand, a preview flag uv no longer knows, an exit code
   other than 0 or 1, a schema other than :data:`KNOWN_SCHEMAS` or a shape this module
   cannot read fails the step with what went wrong — it never passes silently.
2. **The severity** is not in uv's report, so each finding is looked up where it is
   published, OSV (:data:`OSV_URL`), under its id and its aliases: GitHub's rating first
   (``database_specific.severity`` of a ``GHSA-`` record: ``LOW``, ``MODERATE``, ``HIGH``,
   ``CRITICAL``), which is what ``npm audit`` reads; else the highest CVSS v3 base score of
   the records (7.0 and up is high, 9.0 and up critical). uv lists one advisory under each
   of its ids (a ``PYSEC-`` and its ``GHSA-``), and every record names the others, so the
   GitHub rating is found from either.
3. **The rule**: high and critical BLOCK; moderate and low print one line each (id,
   package, severity, fixed-in) and pass. **No severity anywhere** — a record with only a
   CVSS v4 vector, an unreachable OSV — counts as high (fail closed), its id printed so a
   deliberate exception can be made.
4. **An exception** is the app's, never an edit of the kit: ``--ignore-until-fixed
   ID=reason``, repeatable, with its reason; uv leaves the advisory out while no fixed
   version exists and reports it again the day one ships. The reasons are printed on every
   run.

A project's adverse status (PEP 792: archived, deprecated, quarantined) is in the same
report: quarantined blocks, archived and deprecated print a line, and an entry this module
cannot read blocks with its JSON printed (none has been seen yet; extend :func:`_status`).
"""

from __future__ import annotations

import argparse
import functools
import json
import math
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "BLOCKING_RANK",
    "KNOWN_SCHEMAS",
    "OSV_URL",
    "PREVIEW_FEATURES",
    "RANKS",
    "AuditUnreadableError",
    "Fetcher",
    "Finding",
    "Runner",
    "Verdict",
    "audit",
    "command",
    "cvss3_base_score",
    "cvss_rating",
    "fetch_osv",
    "judge",
    "main",
    "parse_report",
    "run_uv",
]

#: The report schemas this module reads; uv's is ``"preview"`` (uv 0.12.22). Another one is a
#: changed format: a kit release reads it first.
KNOWN_SCHEMAS = frozenset({"preview"})

#: The previews the command needs (uv 0.12): the subcommand and its JSON output.
PREVIEW_FEATURES = "audit-command,json-output"

#: The severities, ranked; ``MEDIUM`` is CVSS's word for GitHub's ``MODERATE``.
RANKS: Mapping[str, int] = {"LOW": 1, "MODERATE": 2, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
#: The rank from which a finding blocks: high, as ``npm audit --audit-level=high``.
BLOCKING_RANK = RANKS["HIGH"]

#: Where an advisory's severity is published: its OSV record, by id.
OSV_URL = "https://api.osv.dev/v1/vulns/{id}"

#: The ``uv audit`` run: argv in, (exit code, stdout, stderr) out — a seam for the tests.
type Runner = Callable[[Sequence[str]], tuple[int, str, str]]
#: An OSV record by id, or ``None`` when it cannot be had — a seam for the tests.
type Fetcher = Callable[[str], Mapping[str, Any] | None]


class AuditUnreadableError(Exception):
    """The audit's own failure: uv could not run it, or its report is not one this module
    reads. Exit 2, never a pass."""


@dataclass(frozen=True)
class Finding:
    """One advisory against one locked package, as uv reports it."""

    id: str
    aliases: tuple[str, ...]
    package: str
    version: str
    fixed_in: tuple[str, ...]


@dataclass(frozen=True)
class Verdict:
    """A finding with the severity found for it (``None``: none anywhere) and where from."""

    finding: Finding
    severity: str | None
    source: str

    @property
    def blocks(self) -> bool:
        return self.severity is None or RANKS[self.severity] >= BLOCKING_RANK

    def line(self) -> str:
        fixed = ", ".join(self.finding.fixed_in) or "no fix yet"
        severity = self.severity or "UNKNOWN (counted as high)"
        return (
            f"{self.finding.id} {self.finding.package} {self.finding.version}: {severity} ({self.source}); "
            f"fixed in {fixed}"
        )


# --- the report ----------------------------------------------------------------------------


def _str(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise AuditUnreadableError(f"uv audit's report changed: {where} is not a string")
    return value


def _strs(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AuditUnreadableError(f"uv audit's report changed: {where} is not a list of strings")
    return tuple(value)


def parse_report(stdout: str) -> tuple[list[Finding], list[object]]:
    """uv's JSON report → its findings and its adverse statuses, or
    :class:`AuditUnreadableError` for anything but the shape :data:`KNOWN_SCHEMAS` names."""
    try:
        report = json.loads(stdout)
    except ValueError as exc:
        raise AuditUnreadableError(f"uv audit printed no JSON report: {stdout[:200]!r}") from exc
    if not isinstance(report, dict):
        raise AuditUnreadableError("uv audit's report is not a JSON object")
    schema = report.get("schema")
    version = schema.get("version") if isinstance(schema, dict) else None
    if version not in KNOWN_SCHEMAS:
        raise AuditUnreadableError(
            f"uv audit's report schema is {version!r}, not one of {sorted(KNOWN_SCHEMAS)}: "
            "this uv writes a format this eifi1-server-kit does not read; update the kit"
        )
    entries, statuses, summary = report.get("vulnerabilities"), report.get("adverse_statuses"), report.get("summary")
    if not isinstance(entries, list) or not isinstance(statuses, list) or not isinstance(summary, dict):
        raise AuditUnreadableError("uv audit's report changed: no vulnerabilities, adverse_statuses or summary list")
    findings: list[Finding] = []
    for index, entry in enumerate(entries):
        where = f"vulnerabilities[{index}]"
        if not isinstance(entry, dict) or not isinstance(entry.get("dependency"), dict):
            raise AuditUnreadableError(f"uv audit's report changed: {where} has no dependency")
        dependency = entry["dependency"]
        findings.append(
            Finding(
                id=_str(entry.get("id"), f"{where}.id"),
                aliases=_strs(entry.get("aliases", []), f"{where}.aliases"),
                package=_str(dependency.get("name"), f"{where}.dependency.name"),
                version=_str(dependency.get("version"), f"{where}.dependency.version"),
                fixed_in=_strs(entry.get("fix_versions", []), f"{where}.fix_versions"),
            )
        )
    if summary.get("vulnerabilities") != len(findings) or summary.get("adverse_statuses") != len(statuses):
        raise AuditUnreadableError("uv audit's summary does not count what its report lists")
    return findings, statuses


# --- the severity --------------------------------------------------------------------------

_AV = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}
_AC = {"L": 0.77, "H": 0.44}
_UI = {"N": 0.85, "R": 0.62}
_CIA = {"H": 0.56, "L": 0.22, "N": 0.0}
#: Privileges required: (scope unchanged, scope changed).
_PR = {"N": (0.85, 0.85), "L": (0.62, 0.68), "H": (0.27, 0.5)}


def _roundup(value: float) -> float:
    """CVSS 3.1's Roundup (its specification, Appendix A): up to one decimal, exactly."""
    whole = round(value * 100_000)
    return whole / 100_000.0 if whole % 10_000 == 0 else (math.floor(whole / 10_000) + 1) / 10.0


def cvss3_base_score(vector: str) -> float | None:
    """The base score of a CVSS v3.0 or v3.1 vector (``CVSS:3.1/AV:N/AC:L/…``), or ``None``
    for anything else — a v4 or v2 vector, a malformed one."""
    head, _, rest = vector.partition("/")
    if head not in ("CVSS:3.0", "CVSS:3.1"):
        return None
    metrics = {name: value for name, _, value in (part.partition(":") for part in rest.split("/"))}
    try:
        changed = {"U": False, "C": True}[metrics["S"]]
        av, ac, ui = _AV[metrics["AV"]], _AC[metrics["AC"]], _UI[metrics["UI"]]
        pr = _PR[metrics["PR"]][changed]
        c, i, a = (_CIA[metrics[name]] for name in ("C", "I", "A"))
    except KeyError:
        return None
    iss = 1 - (1 - c) * (1 - i) * (1 - a)
    impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if changed else 6.42 * iss
    if impact <= 0:
        return 0.0
    exploitability = 8.22 * av * ac * pr * ui
    total = 1.08 * (impact + exploitability) if changed else impact + exploitability
    return _roundup(min(total, 10.0))


def cvss_rating(score: float) -> str | None:
    """CVSS's qualitative rating of a base score; ``None`` for 0.0 ("none")."""
    if score >= 9.0:
        return "CRITICAL"
    if score >= 7.0:
        return "HIGH"
    if score >= 4.0:
        return "MEDIUM"
    return "LOW" if score > 0 else None


def judge(finding: Finding, fetch: Fetcher) -> Verdict:
    """The finding's severity from OSV: GitHub's rating under any of its ids, else the
    highest CVSS v3 base score of its records, else none (which blocks)."""
    ids = list(dict.fromkeys((finding.id, *finding.aliases)))
    ids.sort(key=lambda advisory: not advisory.startswith("GHSA-"))  # GitHub's records first
    records: list[Mapping[str, Any]] = []
    for advisory in ids:
        record = fetch(advisory)
        if record is None:
            continue
        records.append(record)
        specific = record.get("database_specific")
        rating = specific.get("severity") if isinstance(specific, dict) else None
        if isinstance(rating, str) and rating.upper() in RANKS:
            return Verdict(finding, rating.upper(), f"GitHub's rating, {advisory}")
    scores = [
        score
        for record in records
        for entry in record.get("severity") or []
        if isinstance(entry, dict) and isinstance(entry.get("score"), str)
        if (score := cvss3_base_score(entry["score"])) is not None
    ]
    if scores and (rating := cvss_rating(max(scores))) is not None:
        return Verdict(finding, rating, f"CVSS v3 {max(scores)}")
    source = "no severity in OSV" if records else "OSV unreachable"
    return Verdict(finding, None, source)


def _status(entry: object) -> tuple[bool, str]:
    """An adverse status → (blocks, its line). Quarantined blocks; archived and deprecated
    print; an entry this module cannot read blocks, printed whole."""
    if isinstance(entry, dict):
        status = entry.get("status")
        dependency = entry.get("dependency")
        name = dependency.get("name") if isinstance(dependency, dict) else entry.get("name")
        if isinstance(status, str) and status.lower() in ("archived", "deprecated", "quarantined"):
            return status.lower() == "quarantined", f"{name or '?'}: {status.lower()}"
    return True, f"an adverse status this module cannot read: {json.dumps(entry)[:300]}"


# --- the run ---------------------------------------------------------------------------------


def command(ignore_until_fixed: Iterable[str] = ()) -> list[str]:
    """``uv audit`` as the gate runs it, with the app's accepted advisories."""
    argv = ["uv", "audit", "--locked", "--preview-features", PREVIEW_FEATURES, "--output-format", "json"]
    for advisory in ignore_until_fixed:
        argv += ["--ignore-until-fixed", advisory]
    return argv


def run_uv(argv: Sequence[str], *, cwd: Path | None = None) -> tuple[int, str, str]:
    """Run ``argv`` in ``cwd`` (the directory of the app's ``uv.lock``; ``None``: here)."""
    try:
        done = subprocess.run(
            list(argv), cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300, check=False
        )
    except FileNotFoundError as exc:
        raise AuditUnreadableError("uv is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise AuditUnreadableError("uv audit did not answer within 5 minutes") from exc
    return done.returncode, done.stdout, done.stderr


def fetch_osv(advisory: str) -> Mapping[str, Any] | None:
    """OSV's record of ``advisory``; ``None`` when it cannot be had (it then counts as high)."""
    request = urllib.request.Request(OSV_URL.format(id=advisory), headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            record = json.load(response)
    except urllib.error.URLError, TimeoutError, ValueError, OSError:
        return None
    return record if isinstance(record, dict) else None


def audit(
    *,
    ignore_until_fixed: Mapping[str, str] | None = None,
    project: Path | None = None,
    run: Runner | None = None,
    fetch: Fetcher = fetch_osv,
    out: Callable[[str], None] = print,
) -> int:
    """The step: 0 nothing blocks, 1 a finding blocks, 2 the audit could not be read.

    ``ignore_until_fixed`` maps each advisory the app accepts while it has no fix to its
    reason; ``project`` is the directory of its ``uv.lock`` (``None``: the working one).
    """
    accepted = dict(ignore_until_fixed or {})
    for advisory, reason in accepted.items():
        out(f"audit: accepted while unfixed {advisory} — {reason}")
    try:
        argv = command(accepted)
        code, stdout, stderr = run(argv) if run is not None else run_uv(argv, cwd=project)
        if "unrecognized subcommand" in stderr:
            raise AuditUnreadableError("this uv has no `uv audit` (uv 0.12 or later): update uv")
        if "Unknown preview feature" in stderr:
            raise AuditUnreadableError(
                f"uv no longer knows a preview feature of {PREVIEW_FEATURES!r} — `uv audit` may be stable now: "
                "a kit release reads its output and drops the flag"
            )
        if code not in (0, 1):
            raise AuditUnreadableError(f"uv audit failed (exit {code}): {stderr.strip()[-500:]}")
        findings, statuses = parse_report(stdout)
        if (code == 0) != (not findings and not statuses):
            raise AuditUnreadableError(f"uv audit's exit {code} does not match its report")
    except AuditUnreadableError as exc:
        out(f"audit: the dependency audit could not be read — {exc}")
        return 2
    lookup = functools.cache(fetch)  # an advisory is listed under each of its ids: one lookup each
    verdicts = [judge(finding, lookup) for finding in findings]
    blocking = [verdict.line() for verdict in verdicts if verdict.blocks]
    for verdict in verdicts:
        if not verdict.blocks:
            out(f"audit: warning {verdict.line()}")
    for entry in statuses:
        blocks, line = _status(entry)
        if blocks:
            blocking.append(line)
        else:
            out(f"audit: warning {line}")
    if not blocking:
        out(f"audit: nothing high or critical ({len(verdicts)} below it, {len(accepted)} accepted)")
        return 0
    for line in blocking:
        out(f"audit: BLOCKS {line}")
    out("audit: upgrade the package, or accept an advisory without a fix: --ignore-until-fixed ID=reason")
    return 1


def _exception(text: str) -> tuple[str, str]:
    advisory, sep, reason = text.partition("=")
    if not sep or not advisory.strip() or not reason.strip():
        raise argparse.ArgumentTypeError(f"{text!r} is not ID=reason: an accepted advisory says why")
    return advisory.strip(), reason.strip()


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m eifi1_server_kit.dependency_audit [--ignore-until-fixed ID=reason]… [--project DIR]``.
    A bad flag exits 2, as a broken tool does."""
    parser = argparse.ArgumentParser(
        prog="python -m eifi1_server_kit.dependency_audit",
        description="Audit the uv.lock: high and critical block, as npm audit --audit-level=high.",
    )
    parser.add_argument(
        "--ignore-until-fixed",
        action="append",
        type=_exception,
        default=[],
        metavar="ID=reason",
        help="accept an advisory while no fixed version exists, saying why (repeatable)",
    )
    parser.add_argument(
        "--project", type=Path, default=None, help="the directory of the uv.lock (default: the working one)"
    )
    args = parser.parse_args(argv)
    return audit(ignore_until_fixed=dict(args.ignore_until_fixed), project=args.project)


if __name__ == "__main__":  # pragma: no cover - the command line's entry point
    raise SystemExit(main())
