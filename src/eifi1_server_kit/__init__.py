"""eifi1-server-kit — the backend sibling of ``@eifi1/ui-kit``.

Layer 1: the cross-app contracts as code. Pure functions, Pydantic models and one
Starlette middleware; no database models, no migrations, no sessions, no repositories,
no routes — those stay in each app. Import from the submodules:

* :mod:`eifi1_server_kit.auth` — sign-in, sign-up and the account (addresses, the gate,
  names, one-time tokens, session claims, limits, schemas, coded refusals);
* :mod:`eifi1_server_kit.user_admin` — user administration and the account's own
  settings (the admin rules and confirmation levels, the user list's query, the audit
  row, the wire shapes, deletion, the account export);
* :mod:`eifi1_server_kit.mail` — mail texts per language, one escaped layout as a whole
  document, Resend and console transports;
* :mod:`eifi1_server_kit.feedback` — the feedback contract (schemas, enums, rules,
  crash filing, erasure, attachment URLs);
* :mod:`eifi1_server_kit.uploads` — what an uploaded file is, by its bytes;
* :mod:`eifi1_server_kit.limiter` — an in-process sliding-window limiter;
* :mod:`eifi1_server_kit.cors` — narrow, credential-free CORS for extra origins;
* :mod:`eifi1_server_kit.translation_review` — the review schemas, key scope and tokens;
* :mod:`eifi1_server_kit.errors` — every kit refusal answered at its contract status.

The contracts are ``docs/feedback-harmonization.md``, ``docs/auth-harmonization.md`` and
``docs/user-admin-harmonization.md`` in ``Eifi1/ui-kit``; the rules are keksdose's, lifted
with a citation on each.
"""

from __future__ import annotations

__version__ = "0.4.0"
