"""0.30 adds to 0.29's names and changes none: everything 0.30 grew is built here with 0.29's
arguments only, as an app on 0.29 builds it (Staunlabor adopts 0.30 right after its pilot).
A field added without a default, a Protocol method added, a keyword made required, fails here."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

from eifi1_server_kit.auth import AuthLimiters, RegisterRequest
from eifi1_server_kit.auth.ports import InvitationStore, UserStore
from eifi1_server_kit.auth.router import AuthDeps
from eifi1_server_kit.auth.service import AccountLinks, AccountPolicy, AuthHooks, SessionsEndedReason
from eifi1_server_kit.auth.testing import FakePasswordHasher, HmacTokenSigner
from eifi1_server_kit.mail import ConsoleMailer
from eifi1_server_kit.user_admin import AccountErrorCode, AdminActionRow, InvitationCreate

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def test_the_models_and_dataclasses_build_with_0_29_s_arguments() -> None:
    row = AdminActionRow(id=1, at=NOW, action="deactivate", actor_id=2, target_user_id=3, target_email="a@b.example")
    assert row.target is None
    AuthLimiters()
    assert AuthLimiters().mail_test.max_hits == 3
    hooks = AuthHooks()
    assert hooks.erase is None and hooks.purge_files is None
    policy = AccountPolicy(
        app="staunlabor", app_name="Staunlabor", contact="s@x.example", links=AccountLinks(base_url="https://x.example")
    )
    assert policy.invitable_roles is None
    deps = AuthDeps(
        users=_dep,
        sessions=_dep,
        tokens=_dep,
        invitations=_dep,
        log=_dep,
        after_commit=_dep,
        signer=HmacTokenSigner(),
        hasher=FakePasswordHasher(),
        mailer=ConsoleMailer(),
        mail_backend="console",
        policy=policy,
    )
    assert deps.offered_locales is None
    body = RegisterRequest(
        email="ada@x.example", password="correct horse battery", first_name="Ada", last_name="L", locale="pt-BR"
    )
    assert body.locale == "pt-BR", "unchecked without offered_locales, as in 0.29"
    assert InvitationCreate(email="b@x.example", role="ANY", locale="pt-BR").locale == "pt-BR"


def test_the_enums_only_grow() -> None:
    assert {
        "logout_everywhere",
        "password_changed",
        "password_reset",
        "forced_change",
        "deactivated",
        "deletion_requested",
    } < {str(reason) for reason in SessionsEndedReason}
    assert {
        "last_admin",
        "self_action",
        "other_companies",
        "household_has_members",
        "confirmation_required",
        "confirmation_mismatch",
        "password_incorrect",
    } < {str(code) for code in AccountErrorCode}


def test_no_0_29_port_grew_a_method() -> None:
    """A Protocol method added is a breaking change for every app's adapter (0.30's rule): the
    admin's reads are new protocols (``user_admin.ports``)."""
    assert _methods(UserStore) == {
        "get",
        "by_email",
        "by_emails",
        "count_people",
        "count_active",
        "add",
        "apply",
        "delete",
    }
    assert _methods(InvitationStore) == {
        "get",
        "by_token_hash",
        "open_for",
        "listing",
        "add",
        "remint",
        "accept",
        "revoke",
    }


def _methods(protocol: type) -> set[str]:
    return {name for name, _ in inspect.getmembers(protocol, inspect.isfunction) if not name.startswith("_")}


async def _dep() -> None:
    return None
