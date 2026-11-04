"""The dependency audit (``eifi1_server_kit.dependency_audit``, every backend's gate): high and critical
block, as the ui-kit's ``npm audit --audit-level=high``; below that a line each; no severity
is high; a report this module cannot read fails the step. Recorded from a probe project
pinning urllib3 1.26.4 (uv 0.12.22, OSV, 2026-11-04), trimmed."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from eifi1_server_kit import dependency_audit as kit_audit

URLLIB3 = {"name": "urllib3", "version": "1.26.4"}

#: uv audit's findings, as recorded (the description dropped).
HIGH = {
    "dependency": URLLIB3,
    "id": "GHSA-2xpw-w6gg-jr37",
    "display_id": "GHSA-2xpw-w6gg-jr37",
    "aliases": ["CVE-2025-66471", "PYSEC-2026-1994"],
    "summary": "urllib3 streaming API improperly handles highly compressed data",
    "link": "https://nvd.nist.gov/vuln/detail/CVE-2025-66471",
    "fix_versions": ["2.6.0"],
    "published": "2025-12-05T18:15:54Z",
    "modified": "2026-09-25T17:15:05.968189421Z",
}
MODERATE = {
    "dependency": URLLIB3,
    "id": "GHSA-34jh-p97f-mpxf",
    "display_id": "GHSA-34jh-p97f-mpxf",
    "aliases": ["CVE-2024-37891", "PYSEC-2026-1995"],
    "summary": "urllib3's Proxy-Authorization request header isn't stripped during cross-origin redirects",
    "link": "https://nvd.nist.gov/vuln/detail/CVE-2024-37891",
    "fix_versions": ["1.26.19", "2.2.2"],
    "published": "2024-06-17T21:37:20Z",
    "modified": "2026-09-10T03:50:14.811193644Z",
}
#: The same advisory as GHSA-g4mx-q9vg-27p4, listed again under its PYSEC id.
PYSEC = {
    "dependency": URLLIB3,
    "id": "PYSEC-2023-212",
    "display_id": "PYSEC-2023-212",
    "aliases": ["CVE-2023-45803", "GHSA-g4mx-q9vg-27p4"],
    "summary": None,
    "link": "https://github.com/urllib3/urllib3/security/advisories/GHSA-g4mx-q9vg-27p4",
    "fix_versions": ["2.0.7", "1.26.18"],
    "published": "2023-10-17T20:15:00Z",
    "modified": "2023-11-08T04:13:39.16545Z",
}

#: OSV's records, as recorded (``id``, ``aliases``, ``severity``, ``database_specific.severity``).
OSV: dict[str, dict[str, Any]] = {
    "GHSA-2xpw-w6gg-jr37": {
        "id": "GHSA-2xpw-w6gg-jr37",
        "aliases": ["CVE-2025-66471", "PYSEC-2026-1994"],
        "severity": [{"type": "CVSS_V4", "score": "CVSS:4.0/AV:N/AC:L/AT:P/PR:N/UI:N/VC:N/VI:N/VA:H/SC:N/SI:N/SA:H"}],
        "database_specific": {"severity": "HIGH"},
    },
    "GHSA-34jh-p97f-mpxf": {
        "id": "GHSA-34jh-p97f-mpxf",
        "aliases": ["CVE-2024-37891", "PYSEC-2026-1995"],
        "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:H/PR:H/UI:N/S:U/C:H/I:N/A:N"}],
        "database_specific": {"severity": "MODERATE"},
    },
    "GHSA-g4mx-q9vg-27p4": {
        "id": "GHSA-g4mx-q9vg-27p4",
        "aliases": ["CVE-2023-45803", "PYSEC-2023-212"],
        "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:A/AC:H/PR:H/UI:N/S:U/C:H/I:N/A:N"}],
        "database_specific": {"severity": "MODERATE"},
    },
    "PYSEC-2023-212": {
        "id": "PYSEC-2023-212",
        "aliases": ["CVE-2023-45803", "GHSA-g4mx-q9vg-27p4"],
        "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:A/AC:H/PR:H/UI:N/S:U/C:H/I:N/A:N"}],
    },
}


def _report(*findings: Mapping[str, Any], statuses: Sequence[object] = (), schema: object = "preview") -> str:
    return json.dumps(
        {
            "schema": {"version": schema},
            "summary": {"audited_packages": 35, "vulnerabilities": len(findings), "adverse_statuses": len(statuses)},
            "vulnerabilities": list(findings),
            "adverse_statuses": list(statuses),
        }
    )


class Uv:
    """A recorded ``uv audit`` run; keeps the argv it was given."""

    def __init__(self, stdout: str, code: int | None = None, stderr: str = "") -> None:
        self.stdout, self.stderr = stdout, stderr
        self.code = code if code is not None else (1 if json.loads(stdout)["summary"]["vulnerabilities"] else 0)
        self.argv: list[str] = []

    def __call__(self, argv: Sequence[str]) -> tuple[int, str, str]:
        self.argv = list(argv)
        return self.code, self.stdout, self.stderr


def _osv(records: Mapping[str, Mapping[str, Any]] = OSV) -> tuple[list[str], Any]:
    asked: list[str] = []

    def fetch(advisory: str) -> Mapping[str, Any] | None:
        asked.append(advisory)
        return records.get(advisory)

    return asked, fetch


def _run(uv: Uv, records: Mapping[str, Mapping[str, Any]] = OSV) -> tuple[int, list[str]]:
    lines: list[str] = []
    _, fetch = _osv(records)
    code = kit_audit.audit(run=uv, fetch=fetch, out=lines.append)
    return code, lines


def test_a_clean_lock_passes() -> None:
    code, lines = _run(Uv(_report()))
    assert code == 0 and lines == ["audit: nothing high or critical (0 below it, 0 accepted)"]


def test_a_high_finding_blocks() -> None:
    code, lines = _run(Uv(_report(HIGH)))
    assert code == 1
    assert (
        "audit: BLOCKS GHSA-2xpw-w6gg-jr37 urllib3 1.26.4: HIGH (GitHub's rating, GHSA-2xpw-w6gg-jr37); fixed in 2.6.0"
    ) in lines


def test_a_critical_finding_blocks() -> None:
    critical = {
        **OSV,
        "GHSA-2xpw-w6gg-jr37": {**OSV["GHSA-2xpw-w6gg-jr37"], "database_specific": {"severity": "CRITICAL"}},
    }
    assert _run(Uv(_report(HIGH)), critical)[0] == 1


def test_a_moderate_finding_warns_and_passes() -> None:
    code, lines = _run(Uv(_report(MODERATE)))
    assert code == 0
    assert lines == [
        "audit: warning GHSA-34jh-p97f-mpxf urllib3 1.26.4: MODERATE (GitHub's rating, GHSA-34jh-p97f-mpxf); "
        "fixed in 1.26.19, 2.2.2",
        "audit: nothing high or critical (1 below it, 0 accepted)",
    ]


def test_a_pysec_finding_takes_its_ghsa_aliass_rating() -> None:
    asked, fetch = _osv()
    verdict = kit_audit.judge(kit_audit.parse_report(_report(PYSEC))[0][0], fetch)
    assert (verdict.severity, verdict.source) == ("MODERATE", "GitHub's rating, GHSA-g4mx-q9vg-27p4")
    assert asked == ["GHSA-g4mx-q9vg-27p4"], "GitHub's record first, one lookup"


def test_without_a_github_rating_the_cvss_v3_score_decides() -> None:
    """PYSEC-2023-212's own record: CVSS 3.1 4.2, medium (NVD's score for CVE-2023-45803)."""
    no_github = {"PYSEC-2023-212": OSV["PYSEC-2023-212"]}
    code, lines = _run(Uv(_report(PYSEC)), no_github)
    assert code == 0 and lines[0] == (
        "audit: warning PYSEC-2023-212 urllib3 1.26.4: MEDIUM (CVSS v3 4.2); fixed in 2.0.7, 1.26.18"
    )
    dos = {"PYSEC-2023-212": {**OSV["PYSEC-2023-212"], "severity": [
        {"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H"}
    ]}}  # fmt: skip
    assert _run(Uv(_report(PYSEC)), dos)[0] == 1, "7.5 is high"


@pytest.mark.parametrize(
    "records",
    [
        {},  # OSV unreachable for every id
        {"GHSA-2xpw-w6gg-jr37": {**OSV["GHSA-2xpw-w6gg-jr37"], "database_specific": {}}},  # a CVSS v4 vector only
        {"GHSA-2xpw-w6gg-jr37": {"id": "GHSA-2xpw-w6gg-jr37", "database_specific": {"severity": "UNKNOWN"}}},
        {
            "GHSA-2xpw-w6gg-jr37": {
                "id": "GHSA-2xpw-w6gg-jr37",
                "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N"}],
            }
        },
    ],
)
def test_no_severity_anywhere_counts_as_high(records: Mapping[str, Mapping[str, Any]]) -> None:
    code, lines = _run(Uv(_report(HIGH)), records)
    assert code == 1
    blocked = [line for line in lines if line.startswith("audit: BLOCKS GHSA-2xpw-w6gg-jr37 ")]
    assert blocked and "UNKNOWN (counted as high)" in blocked[0], "its id printed, for a deliberate exception"


def test_below_high_never_hides_a_high_beside_it() -> None:
    code, lines = _run(Uv(_report(MODERATE, HIGH, PYSEC)))
    assert code == 1
    assert sum(line.startswith("audit: warning ") for line in lines) == 2
    assert sum(line.startswith("audit: BLOCKS ") for line in lines) == 1


@pytest.mark.parametrize(
    ("stdout", "code", "stderr", "said"),
    [
        ("not json at all", 1, "", "printed no JSON report"),
        ("[]", 0, "", "not a JSON object"),
        (_report(schema="1"), 0, "", "schema is '1'"),
        (json.dumps({"schema": {"version": "preview"}}), 0, "", "no vulnerabilities, adverse_statuses or summary"),
        (_report({**HIGH, "id": None}), 1, "", "vulnerabilities[0].id is not a string"),
        (_report({**HIGH, "dependency": "urllib3"}), 1, "", "vulnerabilities[0] has no dependency"),
        (_report({**HIGH, "aliases": "CVE-1"}), 1, "", "vulnerabilities[0].aliases is not a list"),
        (_report(HIGH).replace('"vulnerabilities": 1', '"vulnerabilities": 3'), 1, "", "does not count"),
        (_report(HIGH), 0, "", "exit 0 does not match its report"),
        (_report(), 1, "", "exit 1 does not match its report"),
        ("", 2, "error: unrecognized subcommand 'audit'", "no `uv audit`"),
        (_report(), 0, "warning: Unknown preview feature: `audit-command`", "no longer knows a preview feature"),
        ("", 2, "error: Failed to query the vulnerability service", "failed (exit 2)"),
    ],
)
def test_a_report_this_script_cannot_read_fails_the_step(stdout: str, code: int, stderr: str, said: str) -> None:
    exit_code, lines = _run(Uv(stdout, code, stderr))
    assert exit_code == 2
    assert len(lines) == 1 and lines[0].startswith("audit: the dependency audit could not be read") and said in lines[0]


def test_uv_missing_or_hanging_fails_the_step(monkeypatch: pytest.MonkeyPatch) -> None:

    def missing(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("uv")

    def hangs(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired("uv", 300)

    for broken, said in ((missing, "uv is not installed"), (hangs, "within 5 minutes")):
        monkeypatch.setattr(subprocess, "run", broken)
        lines: list[str] = []
        assert kit_audit.audit(fetch=lambda _id: None, out=lines.append) == 2
        assert said in lines[0]


def test_the_command_is_the_locked_audit_with_its_previews_and_exceptions() -> None:
    uv = Uv(_report())
    _run(uv)
    assert uv.argv == [
        "uv", "audit", "--locked", "--preview-features", "audit-command,json-output", "--output-format", "json"
    ]  # fmt: skip
    lines: list[str] = []
    accepted = {"GHSA-2xpw-w6gg-jr37": "dev only, no fix"}
    assert kit_audit.audit(ignore_until_fixed=accepted, run=uv, fetch=lambda _id: None, out=lines.append) == 0
    assert uv.argv[-2:] == ["--ignore-until-fixed", "GHSA-2xpw-w6gg-jr37"]
    assert lines == [
        "audit: accepted while unfixed GHSA-2xpw-w6gg-jr37 — dev only, no fix",
        "audit: nothing high or critical (0 below it, 1 accepted)",
    ], "an exception says why on every run"


@pytest.mark.parametrize(
    ("status", "blocks"),
    [
        ({"dependency": {"name": "leftpad"}, "status": "quarantined"}, True),
        ({"dependency": {"name": "leftpad"}, "status": "archived"}, False),
        ({"name": "leftpad", "status": "Deprecated"}, False),
        ({"dependency": {"name": "leftpad"}, "status": "gone"}, True),
        ("archived", True),
    ],
)
def test_an_adverse_status_blocks_only_when_quarantined_or_unreadable(status: object, blocks: bool) -> None:
    code, lines = _run(Uv(_report(statuses=[status]), 1))
    assert code == (1 if blocks else 0)
    assert any(("BLOCKS" if blocks else "warning") in line for line in lines)


@pytest.mark.parametrize(
    ("vector", "score"),
    [
        # NVD's scores for these vectors (CVSS 3.1 specification, Appendix A's rounding)
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", 9.8),
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H", 10.0),
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N", 6.1),
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H", 7.5),
        ("CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:N/A:N", 5.9),
        ("CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H", 7.8),
        ("CVSS:3.0/AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N", 5.3),
        ("CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:C/C:H/I:H/A:H", 9.9),
        ("CVSS:3.1/AV:N/AC:H/PR:H/UI:N/S:U/C:H/I:N/A:N", 4.4),  # CVE-2024-37891
        ("CVSS:3.1/AV:A/AC:H/PR:H/UI:N/S:U/C:H/I:N/A:N", 4.2),  # CVE-2023-45803
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N", 0.0),
    ],
)
def test_the_cvss_v3_base_score(vector: str, score: float) -> None:
    assert kit_audit.cvss3_base_score(vector) == score


@pytest.mark.parametrize(
    "vector",
    [
        "CVSS:4.0/AV:N/AC:L/AT:P/PR:N/UI:N/VC:N/VI:N/VA:H/SC:N/SI:N/SA:H",
        "AV:N/AC:L/Au:N/C:P/I:P/A:P",
        "CVSS:3.1/AV:N/AC:L",
        "CVSS:3.1/AV:X/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
    ],
)
def test_a_vector_that_is_not_v3_has_no_score(vector: str) -> None:
    assert kit_audit.cvss3_base_score(vector) is None


@pytest.mark.parametrize(
    ("score", "rating"), [(9.0, "CRITICAL"), (7.0, "HIGH"), (6.9, "MEDIUM"), (4.0, "MEDIUM"), (3.9, "LOW"), (0.0, None)]
)
def test_the_rating_bands(score: float, rating: str | None) -> None:
    assert kit_audit.cvss_rating(score) == rating


def test_osv_unreachable_or_garbled_is_no_record(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import urllib.error
    import urllib.request

    def unreachable(*_args: object, **_kwargs: object) -> None:
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", unreachable)
    assert kit_audit.fetch_osv("GHSA-2xpw-w6gg-jr37") is None
    for body, expected in ((b"[1]", None), (b"{oops", None), (b'{"id": "GHSA-x"}', {"id": "GHSA-x"})):
        monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, body=body, **_k: io.BytesIO(body))
        assert kit_audit.fetch_osv("GHSA-x") == expected


def test_each_id_is_looked_up_once() -> None:
    """uv lists one advisory under its GHSA and its PYSEC id; the lookups are cached."""
    asked: list[str] = []

    def fetch(advisory: str) -> Mapping[str, Any] | None:
        asked.append(advisory)
        return OSV.get(advisory)

    kit_audit.audit(run=Uv(_report(PYSEC, PYSEC)), fetch=fetch, out=lambda _line: None)
    assert asked == ["GHSA-g4mx-q9vg-27p4"]


def test_the_blocking_run_says_how_to_accept_an_advisory() -> None:
    lines = _run(Uv(_report(HIGH)))[1]
    assert (
        lines[-1] == "audit: upgrade the package, or accept an advisory without a fix: --ignore-until-fixed ID=reason"
    )


def test_a_v3_score_of_zero_is_no_severity() -> None:
    nothing = {"GHSA-2xpw-w6gg-jr37": {"id": "GHSA-2xpw-w6gg-jr37", "severity": [
        {"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N"}
    ]}}  # fmt: skip
    code, lines = _run(Uv(_report(HIGH)), nothing)
    assert code == 1 and "UNKNOWN (counted as high) (no severity in OSV)" in lines[0]


def test_an_adverse_status_without_a_name() -> None:
    code, lines = _run(Uv(_report(statuses=[{"status": "archived"}]), 1))
    assert code == 0 and lines[0] == "audit: warning ?: archived"


def test_uv_runs_in_the_projects_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[list[str], object]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs["cwd"]))
        return subprocess.CompletedProcess(argv, 0, _report(), "")

    monkeypatch.setattr(subprocess, "run", run)
    lines: list[str] = []
    assert kit_audit.audit(project=tmp_path, fetch=lambda _id: None, out=lines.append) == 0
    assert calls == [(kit_audit.command(), tmp_path)]
    assert kit_audit.run_uv(["uv", "--version"]) == (0, _report(), "")
    assert calls[-1][1] is None, "no project: the working directory"


def test_the_command_line_passes_its_exceptions_and_project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def fake_audit(**kwargs: Any) -> int:
        seen.update(kwargs)
        return 1

    monkeypatch.setattr(kit_audit, "audit", fake_audit)
    argv = [
        "--ignore-until-fixed", "GHSA-aaaa-bbbb-cccc=dev only, no fix",
        "--ignore-until-fixed", " PYSEC-2026-1 = the CLI's own = sign ",
        "--project", str(tmp_path),
    ]  # fmt: skip
    assert kit_audit.main(argv) == 1
    assert seen == {
        "ignore_until_fixed": {"GHSA-aaaa-bbbb-cccc": "dev only, no fix", "PYSEC-2026-1": "the CLI's own = sign"},
        "project": tmp_path,
    }
    assert kit_audit.main([]) == 1 and seen == {"ignore_until_fixed": {}, "project": None}


@pytest.mark.parametrize("flag", ["GHSA-aaaa-bbbb-cccc", "GHSA-aaaa-bbbb-cccc=", "=reason", " = "])
def test_an_exception_without_its_reason_is_a_broken_call(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        kit_audit.main(["--ignore-until-fixed", flag])
    assert stopped.value.code == 2, "exit 2, as a broken tool"
    assert "is not ID=reason" in capsys.readouterr().err
