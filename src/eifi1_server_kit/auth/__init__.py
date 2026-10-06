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

__all__ = [
    "FAMILY_NAME_FIRST",
    "FAMILY_NAME_FIRST_NO_SPACE",
    "TAG_SEPARATOR",
    "RegistrationDecision",
    "addresses_for_reset",
    "env_list_match",
    "erasure_identifiers",
    "full_name",
    "invitation_accepts",
    "name_incomplete",
    "normalise_email",
    "parse_env_list",
    "registration_decision",
    "tagged_variant",
    "verified_by_invitation",
]
