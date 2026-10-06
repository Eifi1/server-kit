"""Sign-in, sign-up and the account, as code (``docs/auth-harmonization.md`` §8 in ``Eifi1/ui-kit``).

Layer 1, like the rest of the kit: pure functions, Pydantic models and small classes —
no database, no app ``User``, no routes, and no JWT signing (each app keeps its JWT
library; :mod:`~eifi1_server_kit.auth.tokens` builds and checks the claims). keksdose is
the reference, cited ``file`` per rule.

* :mod:`~eifi1_server_kit.auth.accounts` — the address and its tag, invitations, the
  registration gate, names, erasure identifiers;
* :mod:`~eifi1_server_kit.auth.tokens` — one-time tokens (reset, verification,
  invitation) and the session's claims;
* :mod:`~eifi1_server_kit.auth.limits` — :class:`AuthLimiters`, with the per-address
  delay that never locks out;
* :mod:`~eifi1_server_kit.auth.schemas` — the wire shapes;
* :mod:`~eifi1_server_kit.auth.errors` — :class:`AuthError` and its codes.
"""

from __future__ import annotations

from eifi1_server_kit.auth.accounts import (
    FAMILY_NAME_FIRST,
    FAMILY_NAME_FIRST_NO_SPACE,
    TAG_SEPARATOR,
    RegistrationDecision,
    addresses_for_reset,
    env_list_match,
    erasure_identifiers,
    full_name,
    invitation_accepts,
    name_incomplete,
    normalise_email,
    parse_env_list,
    registration_decision,
    tagged_variant,
    verified_by_invitation,
)
from eifi1_server_kit.auth.tokens import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    ACCESS_TOKEN_LIFETIME,
    ACCESS_TOKEN_TYPE,
    CHALLENGE_LIFETIMES,
    INVITE_TTL,
    ONE_TIME_TOKEN_BYTES,
    PROFILE_CLAIMS,
    REFRESH_TOKEN_EXPIRE_MINUTES,
    REFRESH_TOKEN_LIFETIME,
    REFRESH_TOKEN_TYPE,
    RESERVED_CLAIMS,
    RESET_TTL,
    VERIFY_TTL,
    ChallengeKind,
    MintedToken,
    access_claims,
    challenge_claims,
    hash_token,
    is_expired,
    mint,
    refresh_claims,
    token_is_revoked,
)

__all__ = [
    "ACCESS_TOKEN_EXPIRE_MINUTES",
    "ACCESS_TOKEN_LIFETIME",
    "ACCESS_TOKEN_TYPE",
    "CHALLENGE_LIFETIMES",
    "FAMILY_NAME_FIRST",
    "FAMILY_NAME_FIRST_NO_SPACE",
    "INVITE_TTL",
    "ONE_TIME_TOKEN_BYTES",
    "PROFILE_CLAIMS",
    "REFRESH_TOKEN_EXPIRE_MINUTES",
    "REFRESH_TOKEN_LIFETIME",
    "REFRESH_TOKEN_TYPE",
    "RESERVED_CLAIMS",
    "RESET_TTL",
    "TAG_SEPARATOR",
    "VERIFY_TTL",
    "ChallengeKind",
    "MintedToken",
    "RegistrationDecision",
    "access_claims",
    "addresses_for_reset",
    "challenge_claims",
    "env_list_match",
    "erasure_identifiers",
    "full_name",
    "hash_token",
    "invitation_accepts",
    "is_expired",
    "mint",
    "name_incomplete",
    "normalise_email",
    "parse_env_list",
    "refresh_claims",
    "registration_decision",
    "tagged_variant",
    "token_is_revoked",
    "verified_by_invitation",
]
