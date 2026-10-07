"""The self-service account export: one JSON envelope, and a check that no secret is in it.

``docs/user-admin-harmonization.md`` §2.2 and §6.5 in ``Eifi1/ui-kit``.
``GET /auth/me/export`` answers a JSON download (``Content-Disposition: attachment``;
``403`` for a demo account), throttled to once a minute (:data:`EXPORT_PER_USER`) and
logged. What ``account`` and ``data`` hold is each app's (§6.5); the envelope around them
is the kit's (:func:`export_envelope`), so a person with accounts in two apps gets two
files of one format.

**What never leaves** (:data:`NEVER_EXPORT`) is one rule for every app, and the app's
test enforces it: build the export of a fixture account with every table filled — a
password, 2FA, passkeys, API tokens, push subscriptions, key wraps — and run
:func:`assert_no_secrets` over it::

    def test_the_export_holds_no_secret(full_account):
        kit.assert_no_secrets(build_export(full_account))

The check is a heuristic (:mod:`~eifi1_server_kit.user_admin.sensitive`): a key named like
a secret, a value shaped like one. A field it flags wrongly is named in ``allow``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from eifi1_server_kit.auth.limits import Budget
from eifi1_server_kit.auth.tokens import _aware
from eifi1_server_kit.user_admin.sensitive import looks_secret

__all__ = [
    "EXPORT_FORMAT",
    "EXPORT_PER_USER",
    "EXPORT_VERSION",
    "NEVER_EXPORT",
    "ExportSecretError",
    "assert_no_secrets",
    "export_envelope",
    "export_filename",
    "secret_paths",
]

#: The envelope's ``format``: what a reader checks before it trusts the rest.
EXPORT_FORMAT = "eifi1-account-export"
#: The envelope's ``version``; it changes only when the ENVELOPE does, not when an app
#: adds a field to ``account`` or ``data``.
EXPORT_VERSION = 1
#: Once a minute per account (§6.5): an export reads every table the user touches.
EXPORT_PER_USER = Budget(1, 60)

#: What no app's export holds, whatever it chooses for ``data`` (§6.5) — documentation;
#: :func:`assert_no_secrets` catches the secrets among them.
NEVER_EXPORT: tuple[str, ...] = (
    "the password hash",
    "TOTP secrets and backup-code digests",
    "every token and token hash: refresh, API, reset, verification, invitation, email change, translation review",
    "passkey credential ids and public keys (a passkey leaves as its name, created and last used)",
    "push endpoints and their keys (an endpoint is a capability URL)",
    "all E2EE material: key wraps with their Argon2 parameters and salts, recovery verifiers "
    "(at most a public-key fingerprint and the key epoch)",
    "other people's addresses and data: other team members, kastlan's invitees, others' work shared with the user "
    "(a reference at most)",
    "a company's records in kastlan: the company is their controller",
)

_APP = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _check_app(app: str) -> str:
    if not _APP.match(app):
        raise ValueError(f"the app is named in lower case — letters, digits, '.', '_' or '-': {app!r}")
    return app


def export_envelope(
    app: str,
    account: Mapping[str, Any],
    data: Mapping[str, Any],
    now: datetime,
    *,
    version: int = EXPORT_VERSION,
) -> dict[str, Any]:
    """``{"format": "eifi1-account-export", "version": 1, "app", "exported_at", "account",
    "data"}`` (§6.5).

    ``app`` is the app's name (``keksdose``); ``exported_at`` is ``now`` as an ISO 8601
    string in UTC (a naive ``now`` is read as UTC). ``account`` and ``data`` are copied
    in as given — JSON-ready, which the app's encoder (FastAPI's ``jsonable_encoder``)
    sees to for dates.
    """
    return {
        "format": EXPORT_FORMAT,
        "version": version,
        "app": _check_app(app),
        "exported_at": _aware(now).isoformat(),
        "account": dict(account),
        "data": dict(data),
    }


def export_filename(app: str, now: datetime) -> str:
    """``keksdose-account-2026-10-07.json``: the download's name, for
    ``Content-Disposition: attachment; filename=…``."""
    return f"{_check_app(app)}-account-{_aware(now).date().isoformat()}.json"


class ExportSecretError(ValueError):
    """The export holds something that looks like a secret; ``paths`` names each one
    (``account.passkeys[0].public_key``)."""

    def __init__(self, paths: list[str]) -> None:
        self.paths = paths
        super().__init__(f"the export holds what looks like a secret (§6.5): {', '.join(paths)}")


def _walk(value: object, path: str, key: str, allow: frozenset[str], found: list[str]) -> None:
    if isinstance(value, Mapping):
        for name, item in value.items():
            if str(name) not in allow:
                _walk(item, f"{path}.{name}" if path else str(name), str(name), allow, found)
    elif isinstance(value, list | tuple | set | frozenset):
        for index, item in enumerate(value):
            _walk(item, f"{path}[{index}]", key, allow, found)
    elif looks_secret(key, value):
        found.append(path)


def secret_paths(obj: object, *, allow: Iterable[str] = ()) -> list[str]:
    """Every path in ``obj`` whose leaf looks like a secret
    (:func:`~eifi1_server_kit.user_admin.looks_secret`), in document order; ``[]`` when
    none does.

    Objects and lists are walked into, so a container named like a secret is judged by
    what it holds: ``api_tokens: [{"name": …, "scopes": …}]`` passes, ``api_tokens:
    [{"token_hash": …}]`` does not. ``allow`` names keys skipped wherever they occur,
    with everything under them — for a field the heuristic flags wrongly.
    """
    found: list[str] = []
    _walk(obj, "", "", frozenset(allow), found)
    return found


def assert_no_secrets(obj: object, *, allow: Iterable[str] = ()) -> None:
    """Refuse an export that holds what looks like a secret: an
    :class:`ExportSecretError` naming every path (:func:`secret_paths`). Run it in the
    app's test over a fully populated account's export."""
    if found := secret_paths(obj, allow=allow):
        raise ExportSecretError(found)
