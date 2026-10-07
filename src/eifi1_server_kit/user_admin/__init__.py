"""User administration and the account's own settings, as code
(``docs/user-admin-harmonization.md`` §7 in ``Eifi1/ui-kit``).

Layer 1, like the rest of the kit: pure functions, Pydantic models and small classes — no
database, no app ``User``, no routes. The tables (``admin_actions``, the token rows), the
queries, RLS and company scoping, what an erasure deletes and what an export holds stay in
each app. keksdose and Kurvenschmiede are the reference, cited per rule.

* :mod:`~eifi1_server_kit.user_admin.rules` — never yourself, never the last admin, the
  typed address;
* :mod:`~eifi1_server_kit.user_admin.actions` — :class:`AdminAction` and each action's
  :class:`ConfirmationLevel`;
* :mod:`~eifi1_server_kit.user_admin.roster` — the user list's query;
* :mod:`~eifi1_server_kit.user_admin.audit` — the ``admin_actions`` row, and a detail
  that never holds content;
* :mod:`~eifi1_server_kit.user_admin.schemas` — the wire shapes;
* :mod:`~eifi1_server_kit.user_admin.deletion` — the two-stage deletion's dates;
* :mod:`~eifi1_server_kit.user_admin.export` — the export envelope and the secret check;
* :mod:`~eifi1_server_kit.user_admin.sensitive` — what looks like a secret;
* :mod:`~eifi1_server_kit.user_admin.errors` — :class:`AccountError` and its codes.

The email change's token lifetime and kind are the one-time token recipe's, in
:mod:`eifi1_server_kit.auth`: ``EMAIL_CHANGE_TTL``, ``EMAIL_CHANGE_TOKEN_KIND``.
"""

from __future__ import annotations

from eifi1_server_kit.translation_review.scope import LIKE_ESCAPE
from eifi1_server_kit.user_admin.actions import (
    CONFIRMATION_LEVELS,
    AdminAction,
    ConfirmationLevel,
    confirmation_level,
)
from eifi1_server_kit.user_admin.audit import (
    DETAIL_MAX_BYTES,
    DETAIL_MAX_DEPTH,
    DETAIL_TOKEN_MAX_LENGTH,
    AuditDetailError,
    admin_action_record,
    audit_detail,
)
from eifi1_server_kit.user_admin.deletion import (
    BACKUP_RETENTION_DAYS,
    DEFAULT_DELETION_DAYS,
    LOG_RETENTION_DAYS,
    DeletionMode,
    RetentionNote,
    deletion_due,
    deletion_mail_retention_note,
    deletion_schedule,
)
from eifi1_server_kit.user_admin.errors import (
    ACCOUNT_ERROR_DETAIL,
    ACCOUNT_ERROR_STATUS,
    AccountError,
    AccountErrorCode,
)
from eifi1_server_kit.user_admin.export import (
    EXPORT_FORMAT,
    EXPORT_PER_USER,
    EXPORT_VERSION,
    NEVER_EXPORT,
    ExportSecretError,
    assert_no_secrets,
    export_envelope,
    export_filename,
    secret_paths,
)
from eifi1_server_kit.user_admin.roster import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_ROSTER_SORT,
    MAX_PAGE_SIZE,
    MAX_SEARCH_LENGTH,
    SORT_KEYS,
    STATE_TOKENS,
    RosterQueryError,
    RosterSort,
    UserListQuery,
    parse_roster_query,
    parse_sort,
    parse_tokens,
)
from eifi1_server_kit.user_admin.rules import (
    confirm_email_matches,
    refuse_last_admin,
    refuse_self,
    require_confirmation,
)
from eifi1_server_kit.user_admin.schemas import (
    MAIL_BACKEND_CONSOLE,
    MAX_NOTE_LENGTH,
    MAX_REVIEWER_ENTRIES,
    MAX_ROLE_LENGTH,
    SUMMARY_ACTIVE_ADMINS,
    ActionConfirmation,
    ActiveChange,
    AdminActionRow,
    AdminUserRow,
    DeletionRequest,
    EmailChangeConfirm,
    EmailChangeRequest,
    InvitationCreate,
    InvitationRow,
    InvitationStatus,
    MailKind,
    MailRequest,
    MailResult,
    PersonRef,
    ReviewerUpdate,
    RoleChange,
    RolesChange,
    UserListResponse,
    invitation_status,
)
from eifi1_server_kit.user_admin.sensitive import (
    FACT_SUFFIXES,
    SECRET_KEY_TERMS,
    SECRET_VALUE_PATTERNS,
    SECRET_WHOLE_KEYS,
    looks_secret,
    looks_secret_key,
    looks_secret_value,
)

__all__ = [
    "ACCOUNT_ERROR_DETAIL",
    "ACCOUNT_ERROR_STATUS",
    "BACKUP_RETENTION_DAYS",
    "CONFIRMATION_LEVELS",
    "DEFAULT_DELETION_DAYS",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_ROSTER_SORT",
    "DETAIL_MAX_BYTES",
    "DETAIL_MAX_DEPTH",
    "DETAIL_TOKEN_MAX_LENGTH",
    "EXPORT_FORMAT",
    "EXPORT_PER_USER",
    "EXPORT_VERSION",
    "FACT_SUFFIXES",
    "LIKE_ESCAPE",
    "LOG_RETENTION_DAYS",
    "MAIL_BACKEND_CONSOLE",
    "MAX_NOTE_LENGTH",
    "MAX_PAGE_SIZE",
    "MAX_REVIEWER_ENTRIES",
    "MAX_ROLE_LENGTH",
    "MAX_SEARCH_LENGTH",
    "NEVER_EXPORT",
    "SECRET_KEY_TERMS",
    "SECRET_VALUE_PATTERNS",
    "SECRET_WHOLE_KEYS",
    "SORT_KEYS",
    "STATE_TOKENS",
    "SUMMARY_ACTIVE_ADMINS",
    "AccountError",
    "AccountErrorCode",
    "ActionConfirmation",
    "ActiveChange",
    "AdminAction",
    "AdminActionRow",
    "AdminUserRow",
    "AuditDetailError",
    "ConfirmationLevel",
    "DeletionMode",
    "DeletionRequest",
    "EmailChangeConfirm",
    "EmailChangeRequest",
    "ExportSecretError",
    "InvitationCreate",
    "InvitationRow",
    "InvitationStatus",
    "MailKind",
    "MailRequest",
    "MailResult",
    "PersonRef",
    "RetentionNote",
    "ReviewerUpdate",
    "RoleChange",
    "RolesChange",
    "RosterQueryError",
    "RosterSort",
    "UserListQuery",
    "UserListResponse",
    "admin_action_record",
    "assert_no_secrets",
    "audit_detail",
    "confirm_email_matches",
    "confirmation_level",
    "deletion_due",
    "deletion_mail_retention_note",
    "deletion_schedule",
    "export_envelope",
    "export_filename",
    "invitation_status",
    "looks_secret",
    "looks_secret_key",
    "looks_secret_value",
    "parse_roster_query",
    "parse_sort",
    "parse_tokens",
    "refuse_last_admin",
    "refuse_self",
    "require_confirmation",
    "secret_paths",
]
