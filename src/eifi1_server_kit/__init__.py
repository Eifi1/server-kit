"""eifi1-server-kit — the backend sibling of ``@eifi1/ui-kit``.

Layer 1: the cross-app contracts as code. Pure functions, Pydantic models and one
Starlette middleware; no database models, no migrations, no sessions, no repositories,
no routes — those stay in each app. Layer 2 (0.9.0, feedback first): services written
once against ports each app implements, with a router factory; still no database
models, sessions or commits. Import from the submodules:

* :mod:`eifi1_server_kit.auth` — sign-in, sign-up and the account (addresses, the gate,
  names, one-time tokens, session claims, limits, schemas, coded refusals);
* :mod:`eifi1_server_kit.user_admin` — user administration and the account's own
  settings (the admin rules and confirmation levels, the user list's query, the audit
  row, the wire shapes, deletion, the account export, what waits on the admin);
* :mod:`eifi1_server_kit.settings` — one PATCH rule for every settings body, and the
  account's language from the app's offered ones;
* :mod:`eifi1_server_kit.demo` — the demo: its settings, the start's gate, the refusals,
  model R's read-only guard, the throwaway user and its one lifetime;
* :mod:`eifi1_server_kit.dev_switch` — the dev switch (local development only): its
  off-by-default flag, the gate that also needs the app's proof of a local run, the
  startup check against production, the body and its validator, and the route's recipe;
* :mod:`eifi1_server_kit.billing` — billing: its settings, the plan catalogue and limits,
  good standing and the read-only gate, the provider's webhooks (signatures, normalised
  events, a pure dispatcher), the wire shapes;
* :mod:`eifi1_server_kit.jobs` — the scheduler's token routes: ``X-Jobs-Token``, its
  setting and its 401;
* :mod:`eifi1_server_kit.mail` — mail texts per language, one escaped layout as a whole
  document, Resend and console transports, a notification mail's one-click unsubscribe;
* :mod:`eifi1_server_kit.notifications` — notifications: the app's types, the settings
  resource and its PATCH, delivery, quiet hours and the digest's period, a job's mail
  owed until it went, Web Push's shapes and outcomes, the signed unsubscribe token, the
  inbox's shapes;
* :mod:`eifi1_server_kit.feedback` — the feedback contract (schemas, enums, rules,
  crash filing, erasure, attachment URLs), and layer 2: the feedback service behind ports
  each app implements, its routes as ``feedback.router.feedback_router`` (the ``fastapi``
  extra) and the fakes and conformance checks in ``feedback.testing``;
* :mod:`eifi1_server_kit.uploads` — what an uploaded file is, by its bytes;
* :mod:`eifi1_server_kit.limiter` — an in-process sliding-window limiter, and the client's
  address counted from the right of ``X-Forwarded-For``;
* :mod:`eifi1_server_kit.cors` — narrow, credential-free CORS for extra origins;
* :mod:`eifi1_server_kit.translation_review` — the review schemas, key scope and tokens,
  and layer 2 (0.10.0): the review service behind a store port each app implements, its
  routes as ``translation_review.router.translation_review_router`` (the ``fastapi``
  extra) and the fake and conformance checks in ``translation_review.testing``;
* :mod:`eifi1_server_kit.tours` — tour progress on the account (0.15): the wire shapes,
  the merge rule (the later completion wins) and its two coded 422s, and layer 2: the
  service behind a store port each app implements, its routes as
  ``tours.router.tour_progress_router`` (the ``fastapi`` extra) and the fake in
  ``tours.testing``;
* :mod:`eifi1_server_kit.sync` — local-first sync (0.15): keksdose's ``/sync`` shapes with
  per-change ``rejections``, the latest edit time wins, a knowledge-counter port, paging,
  named scopes (0.16: ``SyncScope``, ``EntityRule``, ``sort_changes``, ``withhold``), and
  the SQLAlchemy stamping in ``sync.mixin`` (the ``sync`` extra); the fake in ``sync.testing``;
* :mod:`eifi1_server_kit.clock` — layer 2's clock, an aware UTC ``datetime``;
* :mod:`eifi1_server_kit.errors` — every kit refusal answered at its contract status.

The contracts are ``docs/feedback-harmonization.md``, ``docs/auth-harmonization.md``,
``docs/user-admin-harmonization.md``, ``docs/settings-harmonization.md``,
``docs/landing-demo-harmonization.md``, ``docs/billing-harmonization.md``,
``docs/text-size-harmonization.md``, ``docs/notifications-harmonization.md`` and
``docs/top-bar-harmonization.md`` in
``Eifi1/ui-kit``; the rules are keksdose's,
lifted with a citation on each.
"""

from __future__ import annotations

__version__ = "0.18.0"
