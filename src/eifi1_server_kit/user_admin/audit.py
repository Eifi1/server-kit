"""The ``admin_actions`` row: what an admin did, to whom, when — never what it said.

``docs/user-admin-harmonization.md`` §2.3 and §4.3 in ``Eifi1/ui-kit``. Every app keeps a
small table of admin actions and shows it on the admin page (kastlan keeps its HTTP
``audit_logs`` on top). The table, its migration and its insert are the app's; the kit
builds the row's fields (:func:`admin_action_record`) and holds the vocabulary
(:class:`~eifi1_server_kit.user_admin.AdminAction`).

**Written in the same transaction as the action it records**, so a rolled-back action
leaves no row and a committed one never lacks it — which is what an HTTP log written after
the response cannot promise (kastlan's ``audit_logs``, §9.3)::

    target.is_active = False
    session.add(AdminActionModel(**kit.admin_action_record(
        kit.AdminAction.DEACTIVATE, actor_id=actor.id, target_user_id=target.id,
        target_email=target.email, detail={"sessions_ended": True}, now=now)))
    await session.flush()

**The detail holds ids, roles, flags and counts — never content and never a secret**
(§4.3, §9.6). Kurvenschmiede encrypts notes, titles and bodies at rest; a plaintext copy
in ``detail`` would carry them past the encryption into a table every admin reads, and
past the erasure scrub, which knows the row's ``target_email`` and nothing inside
``detail``. So :func:`audit_detail` refuses, as a programming error, what could carry
either:

* a string that is not a short token — longer than :data:`DETAIL_TOKEN_MAX_LENGTH`, or
  holding a space, ``@`` or anything outside ``A–Z a–z 0–9 _ . : + -``. A role
  (``ADMIN``), a locale (``de-CH``), an ISO date, a kind pass; a sentence, a note, a
  title, an address do not (the address belongs in ``target_email``, where the scrub
  finds it);
* a key that looks like a secret's, or a value with a secret's shape
  (:func:`~eifi1_server_kit.user_admin.looks_secret`);
* anything but ``None``, booleans, finite numbers, those strings, and lists and objects of
  them — at most :data:`DETAIL_MAX_DEPTH` deep and :data:`DETAIL_MAX_BYTES` as JSON.

Dates and enums are written as their ISO string and value, so the result is ready for a
JSON column.
"""

from __future__ import annotations

import enum
import json
import math
import re
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any, cast

from eifi1_server_kit.auth.accounts import normalise_email
from eifi1_server_kit.user_admin.actions import AdminAction
from eifi1_server_kit.user_admin.sensitive import looks_secret

__all__ = [
    "DETAIL_MAX_BYTES",
    "DETAIL_MAX_DEPTH",
    "DETAIL_TOKEN_MAX_LENGTH",
    "AuditDetailError",
    "admin_action_record",
    "audit_detail",
]

#: The longest string a detail holds: a role, a locale, an ISO timestamp (32), a kind.
DETAIL_TOKEN_MAX_LENGTH = 64
#: The most a detail weighs as JSON — ids and counts, not a payload.
DETAIL_MAX_BYTES = 2048
#: How many objects and lists may enclose a value, ``detail`` itself included:
#: ``{"counts": {"projects": 3}}`` encloses the 3 in two, ``{"before": {"roles":
#: ["ADMIN"]}}`` the role in three.
DETAIL_MAX_DEPTH = 3

_TOKEN = re.compile(r"^[A-Za-z0-9_.:+\-]*$")

type _Json = bool | int | float | str | list[_Json] | dict[str, _Json] | None


class AuditDetailError(ValueError):
    """A ``detail`` that could carry content or a secret — a programming error in the app,
    caught by its tests, never a user's (no ``status_code``)."""


def _token(value: str, path: str) -> str:
    if len(value) > DETAIL_TOKEN_MAX_LENGTH or not _TOKEN.match(value):
        raise AuditDetailError(
            f"{path}: a detail holds ids, roles, flags and counts — a string is a short token "
            f"(at most {DETAIL_TOKEN_MAX_LENGTH} of A–Z a–z 0–9 _ . : + -), never content: {value[:24]!r}"
        )
    return value


def _clean(value: object, path: str, key: str, depth: int) -> _Json:
    if depth > DETAIL_MAX_DEPTH:
        raise AuditDetailError(f"{path}: nested deeper than {DETAIL_MAX_DEPTH}")
    if isinstance(value, enum.Enum):
        value = value.value
    if isinstance(value, datetime | date):
        value = value.isoformat()
    if isinstance(value, Mapping):
        out: dict[str, _Json] = {}
        for name, item in value.items():
            if not isinstance(name, str):
                raise AuditDetailError(f"{path}: keys are strings, not {name!r}")
            out[_token(name, f"{path}.{name}")] = _clean(item, f"{path}.{name}", name, depth + 1)
        return out
    if isinstance(value, list | tuple):
        return [_clean(item, f"{path}[{index}]", key, depth + 1) for index, item in enumerate(value)]
    if isinstance(value, float) and not math.isfinite(value):
        raise AuditDetailError(f"{path}: not a finite number")
    if not (value is None or isinstance(value, bool | int | float | str)):
        raise AuditDetailError(f"{path}: ids, roles, flags and counts only, not {type(value).__name__}")
    if isinstance(value, str):
        _token(value, path)
    if looks_secret(key, value):
        raise AuditDetailError(f"{path}: looks like a secret, which a detail never holds")
    return value


def audit_detail(detail: Mapping[str, object] | None) -> dict[str, Any]:
    """``detail`` checked and made JSON-ready (see the module docstring), or an
    :class:`AuditDetailError`. ``None`` is ``{}``."""
    if detail is None:
        return {}
    if not isinstance(detail, Mapping):
        raise AuditDetailError(f"detail is an object, not {type(detail).__name__}")
    cleaned = cast(dict[str, Any], _clean(detail, "detail", "detail", 0))
    if len(json.dumps(cleaned, separators=(",", ":")).encode()) > DETAIL_MAX_BYTES:
        raise AuditDetailError(f"detail is larger than {DETAIL_MAX_BYTES} bytes as JSON")
    return cleaned


def admin_action_record(
    action: AdminAction | str,
    *,
    actor_id: int | None,
    target_user_id: int | None,
    target_email: str | None,
    detail: Mapping[str, object] | None = None,
    company_id: int | None = None,
    now: datetime,
) -> dict[str, Any]:
    """The fields of one ``admin_actions`` row (§4.3), for the app's model:
    ``AdminActionModel(**record)``.

    ``{"at", "actor_id", "action", "target_user_id", "target_email", "detail"}`` — plus
    ``"company_id"`` when one is given (kastlan's company admins; its platform actions and
    every other app leave it out, so a table without the column takes the record as it
    is). No ``id``: the database assigns it.

    * ``action`` — an :class:`~eifi1_server_kit.user_admin.AdminAction` or its value;
      stored as the plain string.
    * ``actor_id`` — who acted; ``None`` for the erasure job (its ``erase`` and
      Kurvenschmiede's day-30 ``transfer``). A ``deletion_request`` names the user
      themselves (§6.4).
    * ``target_user_id`` — ``None`` for an invitation to an address with no account.
      After an erasure the app nulls it and scrubs ``target_email``; the row stays.
    * ``target_email`` — the address as it was at the time, normalised; kept so an
      invitation's row names its invitee.
    * ``detail`` — checked by :func:`audit_detail`.
    * ``now`` — stored as given, so an app with naive UTC columns (kastlan) passes a
      naive one.
    """
    record: dict[str, Any] = {
        "at": now,
        "actor_id": actor_id,
        "action": AdminAction(action).value,
        "target_user_id": target_user_id,
        "target_email": None if target_email is None else normalise_email(target_email),
        "detail": audit_detail(detail),
    }
    if company_id is not None:
        record["company_id"] = company_id
    return record
