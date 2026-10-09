# Changelog

All notable changes to `eifi1-server-kit` — the backend sibling of `@eifi1/ui-kit`.

The contract until 1.0 is the kit's: a **minor** (`0.x.0`) may remove or rename a public
name or change a rule's behaviour, and says so under **⚠ BREAKING CHANGES**; a **patch**
(`0.x.y`) fixes behaviour without changing the surface. Apps pin an exact tag (its commit
hash lands in their `uv.lock`), so nothing reaches an app until it asks for it. Write the
entry in the Conventional Commit; this file is assembled from them at release.

## [Unreleased]

## [0.7.0] (2026-10-10)

server-kit 0.7.0: the backend half of billing round 0.33 (ui-kit 0.33,
`docs/billing-harmonization.md` §14, decisions 18–25; §14.16 wins). One shared Paddle
client behind a provider-neutral port, the `app` tag for one account and three apps, the
codes the routes answer before and around the provider, the notice decision, the
deletion's cancellation and its undoing, and the test helpers that replace each app's
fixtures. Breaking for an app that switches billing on with Paddle (below).

### ⚠ BREAKING CHANGES

* **billing:** switching on with `billing_provider = paddle` also needs `billing_app` (the
  app's tag, set in code: `billing_app: str | None = "keksdose"` in the app's `Settings`)
  and an API environment that resolves and agrees with the key: `billing_environment`, or
  — unset — the key's prefix (`pdl_sdbx_apikey_`, `pdl_live_apikey_`). A legacy key without
  a prefix needs `billing_environment`; a sandbox key with `billing_environment=live` (or
  the reverse) fails at start. An app's test settings that switch billing on with Paddle
  add `billing_app` (§14.2, §14.3).
* **billing:** the `SubscriptionRow` protocol reads `provider_customer_id` (for
  `at_provider`, §14.5). Every app's row has the column; a hand-made fake without it no
  longer satisfies the protocol.
* **billing:** `PlanChangeResponse` gains `usage` (required: `of(previous_plan, plan,
  usage)` fills it from its third argument, as every app builds it), and `previous_plan`
  may be `None` — an account that had no subscription (§14.12).

### Added

* **billing:** the provider's port and the Paddle client (§14.2), lifted from kastlan
  c0b1a24. httpx is the new **`billing` extra** (`billing = ["httpx>=0.28.0"]`), imported
  lazily, so `import eifi1_server_kit.billing` never needs it.
  * **provider:** `BillingProviderClient` — `checkout_url(*, price_id, custom_data,
    customer_id=None, locale=None)`, `portal_url(*, customer_id, subscription_id=None,
    target="overview")` (`PortalTarget`: `overview`, `cancel`, `payment_method`),
    `cancel(*, subscription_id, immediately=False)`, `remove_scheduled_cancel(*,
    subscription_id)`; `NoProviderClient` (every call 503 `billing_not_configured`);
    `billing_provider_client(settings, *, client=None)`, which never raises for the
    settings; `sold_plan(catalogue, request, *, sold=None)` (422
    `billing_plan_not_sold`; kastlan answered 503); `require_new_checkout(row, now)` (409
    `billing_already_subscribed`, a payer who bought under a grant included);
    `cancel_for_deletion(row, client)` → `CancelOutcome` and
    `resume_after_withdrawal(row, client)` → `ResumeOutcome`, which never raise (§12.37,
    §14.8: at the period's end, a paused subscription at once; undone through Paddle's
    `PATCH /subscriptions/{id} {"scheduled_change": null}`); `CHECKOUT_RETURN_PARAM`,
    `CHECKOUT_RETURN_VALUE`, `checkout_return_url(app_base_url, path)` (§14.4).
  * **paddle:** `PaddleClient(api_key, *, environment, app=None, checkout_page_url=None,
    hosted_checkout_url=None, timeout=15.0, client=None)`: the transaction with the app's
    tag added by the client and the pay page as `checkout.url`, or the hosted checkout's
    `?transaction_id=…&locale=…`; the portal's `cancel` and `payment_method` links; one
    injected `httpx.AsyncClient` (else one per call); no retry; one log line per failure,
    never the body or the key. `PaddleError(BillingError)` with `status`, `paddle_code`,
    `paddle_detail`, `request_id`: 502 `billing_provider_unavailable`, or 503
    `billing_not_configured` for a 401/403 (logged at ERROR); a 2xx without the fields
    read is a 502, not kastlan's `KeyError`. `paddle_environment_of(api_key)`,
    `PADDLE_API_BASES` (the kit's constant: no base URL setting), `PADDLE_API_VERSION`,
    `PADDLE_TIMEOUT_SECONDS`.
* **billing:** four codes, each with the `billing_` prefix (decision 22), in
  `BILLING_ERROR_STATUS` and `BILLING_ERROR_DETAIL`: `billing_provider_unavailable` 502,
  `billing_not_at_provider` 409, `billing_already_subscribed` 409, `billing_plan_not_sold`
  422. ui-kit 0.33's `BillingErrorCode` reads the same words.
* **billing:** settings (§14.2): `PaddleEnvironment` (`sandbox`, `live`), `billing_app`,
  `billing_environment`, `billing_checkout_page_url` (the pay page on the app's pay host)
  and `billing_hosted_checkout_url` (kastlan's, same variable name; wins over the pay
  page), the URLs `https://` or `http://` on localhost; `billing_api_environment()` (503
  when unknown or disagreeing with the key) and `verify_billing_webhook(provider, raw_body,
  headers, *, now=None)`, the signature check with the deployment's secret AND its
  tolerance — kastlan and Kurvenschmiede called it without `tolerance=`.
* **billing:** the `app` tag (decision 18, §14.3): `APP_KEY = "app"`,
  `checkout_custom_data(payer_ref, *, app=None)`, `NormalisedEvent.app`, and
  `parse_webhook_event(provider, raw_body, *, app=None)`: given the app, another app's
  event and an untagged one are `None` (200, never recorded or dispatched), logged — the
  untagged at WARNING — and never matched by the provider's customer id.
* **billing:** `BillingOverview.at_provider`, from the row's `provider_customer_id`
  (§14.5), and `PortalRequest {target}` (`extra="forbid"`), the portal's optional body.
* **billing:** `PlanChangeResponse.kept_beta` and `comped_until`, and `of(…, *,
  kept_beta=False, comped_until=None)` (§14.12): what ui-kit's `usePlanChangeResult`
  reads.
* **billing:** notices (§12.22, §14.7): `billing_notice_due(row, now, *, launch, sent,
  ahead=NOTICE_AHEAD, late_limit=NOTICE_LATE_LIMIT)` → `BillingNotice(kind, ends_at,
  days_left)` with `.key`, the row's `billing_notice_sent` marker; `BillingNoticeKind`,
  `NOTICE_AHEAD` (7 days), `NOTICE_LATE_LIMIT` (3 days), `NOTICE_STATUSES`. A beta row's end
  is the launch plus 12 months (kastlan's `due_notices` read the stored end only); no grant
  notice while a paid period runs past it. `billing_notices_owed(candidates, now, *,
  settings)` → `OwedNotice(payer, kind, ends_at, days_left)` over `NoticeCandidate`s:
  nothing while billing is off, no deactivated account, none with a deletion request.
* **billing.testing** (§14.9), in the wheel, not re-exported: `paddle_event(step, *,
  payer_ref, price_id, app=None, at=None, subscription_id, customer_id, event_id=None,
  period_end=None)` with `PADDLE_STEPS` and `PADDLE_STEP_PERIODS` — keksdose's and
  Kurvenschmiede's five steps and the scheduled cancellation, the cancelled step without a
  period; `sign_paddle`, `signed_paddle_event`; `post_signed` (localhost only);
  `FakeBillingProvider` (records `checkouts`, `portals`, `cancels`, `resumes`; `fail=True`);
  `PaddleApiFake` (an `httpx.MockTransport` handler with `fail_with` and `unreachable`) and
  `PADDLE_API_EXAMPLES`, Paddle's documented answers.
* The API export lists `eifi1_server_kit.billing` against §10 and §14.

### Deprecated

* **billing:** Lemon Squeezy (decision 25, §14.13): `BillingProvider.LEMONSQUEEZY`,
  `map_lemonsqueezy_event`, `LEMONSQUEEZY_EVENT_KINDS`, `LEMONSQUEEZY_STATUSES`,
  `verify_lemonsqueezy_signature`, `LEMONSQUEEZY_SIGNATURE_HEADER`, and the numeric variant
  ids `billing_price_ids` accepts. Marked in the docstrings; still working, with no
  runtime warning (the apps run with `error::DeprecationWarning`); removed in 0.8.
  Kurvenschmiede's `/webhooks/lemonsqueezy` test answers 422 then.

### Docs

* The billing package's docstring: "one request each for checkout, portal, cancel and
  undoing a cancel, through `billing.paddle`, only with the `billing` extra". The webhook
  recipe uses `verify_billing_webhook` and `parse_webhook_event(…, app=…)`.

## [0.6.1] (2026-10-09)

From the apps' 0.32 adoptions (kastlan, keksdose, Kurvenschmiede). Additive: no public
name is removed or renamed, and every new argument and setting defaults to 0.6.0's
behaviour.

### Added

* **billing:** `PlanOut`, a plan on the wire for `GET /billing/plans` (§4): `{code, prices,
  limits, sort}` with the prices nested currency → interval → gross minor units — ui-kit's
  `PlanPrices`, which `BillingPlan.prices` takes as it is. `PlanSpec.prices`' `(currency,
  interval)` keys can't cross JSON, so each app invented its own list (kastlan sent
  `[{currency, interval, amount}]` and converted it in the page). A combination not sold
  is absent, never `0`; unlimited is `null`; names and feature lines stay the app's i18n
  (§3.1). `PlanOut.from_spec(plan)` nests the prices in a fixed order (CHF, EUR; month,
  year), and `plans_out(catalogue)` answers the route in `sort` order. From kastlan's 0.32
  report. The prices are typed `PlanPrices` and `PlanIntervalPrices`, TypedDicts with a
  key per currency and per period and no others, so openapi-typescript generates
  `{CHF?: {month?: number; year?: number}; EUR?: …}`, ui-kit's type, without a cast: a
  dict keyed by the enums carried them only as `propertyNames`, which it drops. From
  keksdose's 0.32 report.
* **billing:** `effective_comped_until(row, launch)`, when a row's free grant ends as the
  kit reads it — for a banner's "free until …" — and a `launch=None` keyword on
  `grant_holds`, `in_good_standing`, `dispatch` and `BillingOverview.from_row` (see
  Fixed).
* **billing:** `BillingSettings.billing_signature_tolerance`, Paddle's timestamp window in
  seconds, for the route to pass to `verify_webhook_signature(…, tolerance=…)`. The
  default stays Paddle's five seconds (its docs: "Our SDKs have a default tolerance of
  five seconds between the timestamp and the current time"); a scale-to-zero host, whose
  cold start can eat them, widens it in its environment — keksdose runs 60.
  `PADDLE_SIGNATURE_TOLERANCE` is unchanged. From keksdose's 0.32 report.
* **billing:** `DuplicateEventError`: `EventStore.record` may raise it when its insert hits
  the unique `(provider, event_id)` — a delivery that raced another past `seen` — and
  `dispatch` answers `DispatchOutcome.DUPLICATE`, as for an event seen before;
  `webhook_answer` answers it 200 wherever it is raised. It replaces the savepoint recipe,
  which can't nest on SQLite's driver in the apps' tests. The `seen` path is unchanged.
  From keksdose's 0.32 report.

### Fixed

* **billing:** a beta row stored without an end — the apps' beta migrations and every
  registration before the launch date is known write `comped_until = None` — no longer
  reads as free for good. Given the launch date it ends at `beta_comped_until(launch)`,
  resolved at read time, so a moved launch date needs no data change (§3.2).
  `BillingSettings.billing_standing` passes `billing_launch_at` itself, and
  `BillingOverview.from_row(…, launch=…)` shows the effective end. An explicit
  `comped_until` wins over the launch, an operator's grant (`manual`) without an end keeps
  none, and without a launch date — switching billing on does not require one — a beta
  row holds as in 0.6.0. From Kurvenschmiede's 0.32 report.
* **billing:** an empty `<APP>_BILLING_*` variable reads as unset, so an `.env` template
  can list them all empty: `BILLING_LAUNCH_AT=` was no datetime, and `BILLING_PRICE_IDS=`
  failed in pydantic-settings' JSON decoding before any validator ran. The mixin leaves
  its own blank fields at their defaults (an app's own fields are untouched), and the
  price ids carry pydantic-settings' `NoDecode` (imported where it is installed; the kit
  still doesn't depend on it) and are decoded by the mixin. pydantic-settings joins the
  dev dependencies, for the tests. From keksdose's 0.32 report.

### Docs

* README and `BillingSettings`: a test that monkeypatches `billing_price_ids` passes the
  parsed shape, lists of ids (`{"pro": {"CHF": {"year": ["pri_test"]}}}`). The string →
  list step, the keys' normalisation and the one-id check are the field's validation,
  which runs when the settings are built, never on assignment; a bare string is read
  character by character (`billing_price_id` answers `"p"`). From kastlan's 0.32 report.
* `row_changes` hands `status`, `source` and `provider` back as the kit's `StrEnum`s; they
  compare equal to and store as their string values, so an app writing `.value` sees no
  difference. From Kurvenschmiede's 0.32 report.
* `in_good_standing(None)` is true, so with RLS on the subscription table a guest who
  reads the payer's row back as nothing makes the gate FAIL OPEN: read the payer's row
  with the bypass, or treat a missing row as an error where one must exist. From
  keksdose's 0.32 report.
* `PlanChangeRequest`: a running beta grant given no `comped_until` keeps its beta — only
  the plan moves, and the beta still ends at the launch plus 12 months — or a pre-launch
  plan move would be free for good (keksdose's `kept_beta`). The kit writes no row, so the
  rule is the app's. From keksdose's 0.32 report.

## [0.6.0] (2026-10-08)

The backend half of the billing round and of the text-size round (ui-kit 0.32,
`docs/billing-harmonization.md` §10 with §12, and `docs/text-size-harmonization.md` §6
with §10.6). Additive but for the two notes under Changed.

### Added

* **billing:** a new subpackage, Layer 1 like the rest: no tables, no routes, no provider
  SDK, no request to a provider.
  * **settings:** `BillingSettings`, a mixin: `billing_enabled` off by default;
    `billing_provider` (`BillingProvider`: `paddle`, `lemonsqueezy`), `billing_api_key` and
    `billing_webhook_secret`, all three required to switch on; `billing_price_ids` keyed by
    plan, currency and interval (a list keeps retired price ids findable);
    `billing_launch_at`. `require_billing_enabled()` (404 `billing_disabled`),
    `billing_price_id()` (503 `billing_not_configured`), `billing_price_ref()` →
    `PriceRef`, `billing_plan_for_price()`, `billing_webhook_key()`, `billing_standing()`
    (everyone is in good standing while billing is off).
  * **plans:** `PlanSpec(code, limits, prices, sort)` with gross prices in strict integer
    minor units keyed by `(currency, interval)`; `plan_catalogue()` (no duplicate codes,
    the same dimensions in every plan); `normalize_plan()` (lowercase, so keksdose's
    uppercase history reads the same); `check_limit(plan, dimension, used, *, adding=1)`
    raising `PlanLimitError`, 402 `{detail, code: "plan_limit", dimension, plan, limit,
    used}`; `dimensions_over_limit()`; `BillingCurrency` (CHF, EUR), `BillingInterval`,
    `CURRENCY_EXPONENTS`, `minor_to_decimal()`.
  * **standing:** `SubscriptionStatus`, `SubscriptionSource`, the `SubscriptionRow`
    protocol; `in_good_standing(row, now, *, retry_grace=None)` per §3.3 with §12.7 (no
    row: always), §12.9 (an empty trial end waits) and §12.11 (after a grant, the
    provider's paid period); `grant_holds()`; `trial_ends_at()` (30 days),
    `beta_comped_until()` (12 calendar months), `is_beta()` by the invitation's date.
  * **gate:** `billing_write_allowed(method, path, *, standing, allow=frozenset())`, the
    demo's shape with the payer's standing as an input, and `refuse_billing_read_only(in_good_standing,
    what)` for an app's own choke points (§12.2).
  * **webhooks:** `verify_webhook_signature()` with `verify_paddle_signature()` (`ts`,
    `h1`, 5 s tolerance, secret rotation) and `verify_lemonsqueezy_signature()`, both with
    `hmac`; the normalised vocabulary `EventKind`, `NormalisedEvent`,
    `parse_webhook_event()` with `map_paddle_event()` and `map_lemonsqueezy_event()`,
    `PoisonEventError`, `checkout_custom_data()`; the `EventStore` port and
    `dispatch(event, store, apply, *, load, plan_for_price, now=None)` with the ordering
    guard, an operator's grant beating provider events (§12.11) and a replaced
    subscription's late end skipped; `row_changes()`; `webhook_answer()` (unknown and
    duplicate 200, poison 200 and logged, transient 500).
  * **schemas:** `BillingStatus`, `BillingOverview` (`from_row`), `CheckoutRequest`,
    `CheckoutAnswer`, `PlanChangeRequest`, `PlanChangeResponse` (`of`), `SyncRefusal`.
  * **errors:** `BillingError` (`billing_disabled` 404, `billing_read_only` 402,
    `billing_not_configured` 503, `invalid_signature` 400) and `PlanLimitError` join
    `CONTRACT_ERRORS`, so `install_contract_error_handlers` answers them.
* **user_admin:** `AdminAction.PLAN` (`"plan"`, keksdose's word) at the `acknowledge` level
  (billing §6). keksdose's stored rows read as the kit's action, and its type-ignore on
  `AdminActionRow` can go.
* **auth:** the account's text size and contrast (text-size §6, §10.6): `TEXT_SIZES`
  (`normal`, `large`, `xlarge`), `CONTRAST_MODES` (`system`, `standard`, `more`), their
  types `TextSize` and `ContrastMode`, and optional `text_size` and `contrast` on
  `UserResponse` (`None` = never chosen) and `ProfileUpdate`.
* The API export lists `eifi1_server_kit.billing` against `docs/billing-harmonization.md`
  §10.

### Changed

* **auth:** `PROFILE_NOT_NULLABLE` names `text_size` and `contrast` too, so an explicit
  `null` on them is refused like on a name ("System" is the stored way back). An app that
  passes `PROFILE_NOT_NULLABLE` to `apply_patch` with a model NOT derived from the kit's
  `ProfileUpdate` gets `no such field` until its model has both fields.
* **user_admin:** `AdminActionRow.action` reads `"plan"` as `AdminAction.PLAN` (equal to
  `"plan"` as before).

### Fixed

* **user_admin:** `admin_action_record` keeps an app's own action as written
  (`"reseed_demo"`), as `AdminActionRow` reads it back since 0.5.1; it used to raise
  `'…' is not a valid AdminAction`. A known value is still stored as the enum's value, and
  an empty or blank action is still a `ValueError`. Found by keksdose after adopting 0.5.1.

## [0.5.1] (2026-10-07)

From the apps' 0.31 adoptions (kastlan, keksdose).

### Fixed

* **user_admin:** `AdminActionRow.action` takes the app's own action as a string (keksdose's
  `plan`), as `admin_action_record` already did. `FACT_SUFFIXES` gains `revoked` and
  `valid` (`api_tokens_revoked` is a fact, not a secret), and `SECRET_VALUE_PATTERNS`
  catches a Web Push endpoint under any key (a capability URL). The README now
  recommends a planted-secret byte check in the export test as well.
* **limiter:** `client_ip` logs once per process when an `X-Forwarded-For` is shorter than
  `trusted_hops`, so a mis-measured hop count, which puts every caller into one window,
  shows up in the log.
* **auth:** `Budget.limiter()` defaults its clock to `time.monotonic`, like every other
  limiter in the kit, so a per-demo budget is one call.
* **demo:** `refuse_demo` says "Not possible for a demo account: …" (and `DEMO_REFUSED`'s
  default detail "Not possible for a demo account"). It also refuses an admin acting ON a
  demo account, and "in the demo" read wrongly from that side. The code is unchanged.

## [0.5.0] (2026-10-07)

The backend half of the settings round and of the landing and demo round (ui-kit 0.31,
`docs/settings-harmonization.md` §7.2 and `docs/landing-demo-harmonization.md` §7.2), the
feedback triage step READY (`docs/feedback-harmonization.md` §8.2), and the API export
the ui-kit showcase's "Server kit" group is built from. Additive, apart from the rework
status below: no public name is removed or renamed.

### Changed

* **feedback:** a rework sent by an admin goes back to `READY`, no longer to `OPEN`
  (`plan_update` decides it from `is_admin`); anyone else's still goes to `OPEN`. The
  author's lane is OPEN / READY / IN_PROGRESS, and the refusal says so.
  `AWAITING_STATUSES` gains `OPEN`, as the kit's does.
* **auth:** `TokenResponse`'s docstring no longer describes keksdose's 60-minute demo
  token: a demo's token lives to the account's end (`expires_at`).

### Added

* **user_admin:** `UserListResponse.levels` (action → `ConfirmationLevel`, so a roster
  renders the right confirmation before its first request) and `SUMMARY_ACTIVE_ADMINS`,
  the `summary` key the last-admin lock reads. kastlan's and Kurvenschmiede's list shape.
* **settings:** a new module (settings §6).
  * `apply_patch(obj, update, *, not_nullable, defaults=None)`: §6.1's rules 1–3 over a
    body's `model_fields_set`, for PATCH and PUT bodies alike — an omitted field keeps its
    value, a `null` clears (or resets to `defaults[name]`), a `null` on a `not_nullable`
    field raises `PatchNullError` before anything is written. Writes onto an object or a
    mapping; answers the fields written. A model without `extra="forbid"` and a guard name
    that is no field are programming errors.
  * `PatchNullError`: 422, `code: "not_nullable"`, `fields: [...]`; in `CONTRACT_ERRORS`.
  * `canonical_locale(tag, offered)`: case and `_` normalised, an exact offered tag, else
    the language's offered tag (`de`, `de-DE` → `de-CH`), else `None`.
    `parse_accept_language(header, offered)`: the best offered locale by q-value.
  * `profile_update_model(offered, name="ProfileUpdate")`, and the `offered_locales` knob
    on `auth.ProfileUpdate` it sets: the locale goes through `canonical_locale` before the
    pattern, and an unoffered language is a 422. Unset, nothing changes.
* **demo:** a new module (landing-demo §5–§7).
  * `DemoSettings` (`demo_session_enabled` False, `demo_user_max_age_hours` 24,
    `demo_session_max_live` 500, `demo_session_rate_max` 5,
    `demo_session_rate_window_seconds` 3600), for the app's settings to inherit.
  * `DemoGate(settings, reap_limit=20).admit(ip, *, reap, count_live, is_ready, now=None)`
    → `DemoAdmission(now, cutoff, reaped)`: §5.1's order around the app's async
    callbacks (`DemoReaper`, `DemoCounter`), the cap counting live users only.
  * `DemoErrorCode`, `DemoError(code, detail=None, *, retry_after=None)` with
    `DEMO_ERROR_STATUS` and `DEMO_ERROR_DETAIL`; in `CONTRACT_ERRORS`.
  * `demo_write_allowed(method, path, allow=frozenset())` (`READ_METHODS`; the allow-list
    is the app's, `"METHOD /path"` with `{name}` for a segment) and
    `refuse_demo(user_is_demo, what)`.
  * `demo_address(domain)`, `is_demo_address(email, domain=None)`, `demo_password()`,
    `DEMO_FIRST_NAME`, `demo_expires_at(created_at, settings)`, `stale_cutoff(now,
    settings)`.
* **limiter:** `client_ip(headers, peer, *, trusted_hops)`, counted from the right of
  `X-Forwarded-For` (landing-demo §5.1) — the left-most entry is the client's to write.
  `UNKNOWN_CLIENT`.
* **auth:** `TokenResponse.expires_at` and `UserResponse.demo_expires_at`, optional, of the
  new type `UtcDateTime` (ISO-8601 in UTC; a naive value is read as UTC).
* **errors:** `contract_error_response` puts a refusal's `headers` on the answer
  (`DemoError`'s `Retry-After`).
* **docs:** `scripts/export_api.py` writes `dist/server-kit-api.json` (format
  `eifi1-server-kit-api`, version 1: every module's members, signatures, docstrings,
  fields, enum values and methods, its ui-kit contract, and sample mails in `en` and
  `de-CH`); `check.sh` runs it after `uv build`, so every release attaches it.
  `limiter`, `cors`, `uploads` and `errors` gain an `__all__`.

* **feedback:** `FeedbackStatus.READY` (feedback contract §8.2, keksdose live #396), the
  triage step between `OPEN` and `IN_PROGRESS`: OPEN is "filed, not yet triaged", READY
  "released for implementation". `initial_status(author_is_admin=, crash=False)` → READY
  for an admin's own report, OPEN otherwise and for every crash; `rework_status(
  actor_is_admin=)`. `AUTHOR_EDITABLE_STATUSES` gains READY; the reworkable set is the
  same five. An app with a database enum adds the label (`ALTER TYPE feedbackstatus ADD
  VALUE 'READY' AFTER 'OPEN'`).

## [0.4.0] (2026-10-07)

The user-administration half of the user-management round (ui-kit 0.30.0,
`docs/user-admin-harmonization.md` §7): the admin rules and their confirmation levels,
the user list's query, the audit row, the wire shapes, the two-stage deletion, the
account export and the email change's token. With it, Kurvenschmiede's mail findings: a
whole HTML document, and a Reply-To.

### ⚠ BREAKING CHANGES

* **mail:** `render_message` takes `subject` and `lang` (keyword, required) and returns a
  whole HTML document; `MailText.render` takes `lang` (required), the language its row is
  written in. No app calls the kit's mail module yet. The one-call replacement for
  `pick(locale, texts).render(link=…)` is `render_mail(locale, texts, link=…)`, which
  passes the row's own language.

### Added

* **user_admin:** a new subpackage, Layer 1 like the rest.
  * **rules:** `refuse_self(actor_id, target_id)` and `refuse_last_admin(target_is_admin=,
    active_admins=, change_removes_admin=)` (Kurvenschmiede's rules; kastlan counts per
    company); `confirm_email_matches` (normalised, caseless, a `+tag` kept);
    `require_confirmation(level, target_email=, acknowledged=, confirm_email=)`:
    acknowledge takes the checkbox or the typed address, type_email the address only.
  * **actions:** `AdminAction`, the §4.3 vocabulary; `ConfirmationLevel` and
    `CONFIRMATION_LEVELS`, the §4.2 table: `type_email` for deactivate, erase, transfer
    and the user's own deletion request; `acknowledge` for a role or a forced password
    change. `confirmation_level(action, at_least=)` treats the contract's level as a
    floor, which an app may raise (keksdose's password-wrap rule) and never lower.
  * **roster:** `parse_roster_query(…)` → `UserListQuery`, or `RosterQueryError` (422).
    The sort keys are `SORT_KEYS`; the contract's `-key` and DataTable's `key.desc` both
    work, each key once, and the default is newest first. The states are
    `STATE_TOKENS`: §3.1's plus keksdose's `verified`, comma-separated or repeated, and
    an app may narrow or widen them. Roles can be checked against the app's own; limit
    1–200; `q` is trimmed, with `search_pattern` for `LIKE`.
  * **audit:** `admin_action_record(action, actor_id=, target_user_id=, target_email=,
    detail=, company_id=, now=)` returns the row's fields. `company_id` is included only
    when given, and `now` is stored as given. `audit_detail` refuses whatever could carry
    content or a secret (§9.6, Kurvenschmiede's encrypted columns): a string must be a
    short token, keys may not be named like secrets nor values shaped like them, and the
    rest is JSON scalars, lists and objects at most three deep and 2 KB.
  * **schemas:** `AdminUserRow` (the `UserResponse` core plus `last_login_at`,
    `password_change_required_at`, `deletion_requested_at`, `deletion_scheduled_at`, the
    reviewer scope and `extra`) and `UserListResponse[RowT]`. `ActionConfirmation
    {acknowledged, confirm_email?}` underlies `ActiveChange`, `RoleChange`, `RolesChange`
    and `MailRequest` (`MailKind`). `MailResult` refuses a link unless mail goes to the
    console. Also `InvitationCreate`, `InvitationRow`, `InvitationStatus`,
    `invitation_status(…)`, `ReviewerUpdate`, `PersonRef`, `AdminActionRow`,
    `EmailChangeRequest`, `EmailChangeConfirm` and `DeletionRequest`.
  * **deletion:** `DeletionMode` (`after_days`, `operator`) and
    `deletion_schedule(now, mode, days=30)`. `deletion_due(requested_at, days, now,
    scheduled_at=)` never erases before the promised date.
    `deletion_mail_retention_note(backup_days=7, log_days=30)` gives the mail's numbers.
  * **export:** `export_envelope(app, account, data, now)` (`"eifi1-account-export"`,
    version 1), `export_filename`, `EXPORT_PER_USER` and `NEVER_EXPORT`.
    `assert_no_secrets` / `secret_paths` → `ExportSecretError`, for the app's test over a
    full export; `allow=` covers a false positive.
  * **sensitive:** `looks_secret_key`, `looks_secret_value` and `looks_secret`, the one
    heuristic the audit detail and the export share.
  * **errors:** `AccountErrorCode` (`last_admin`, `self_action`, `other_companies`,
    `household_has_members`, `confirmation_required`, `confirmation_mismatch`, all 409;
    `password_incorrect`, 400, for a wrong current password on a signed-in route, so a
    client never reads it as an ended session) and `AccountError(code, detail=None,
    extra=None)`.
* **auth:** `AuthErrorCode.TOKEN_EXPIRED` (`token_expired`, 400), which the ui-kit has
  since 0.29.1. Also `EMAIL_CHANGE_TTL` (48 h), `OneTimeTokenKind` (`password_reset`,
  `verification`, `invitation`, `email_change`), `ONE_TIME_TOKEN_TTLS` and
  `EMAIL_CHANGE_TOKEN_KIND`.
* **mail:** `render_mail(locale, texts, link=, **values)` and `pick_entry(locale, texts)`
  → `(key, row)`. `ResendClient(reply_to=)` sends `"reply_to": [address]`.
  `support_address(from_address)` gives `support@<domain>`, the default of each app's
  `*_EMAIL_REPLY_TO` (Cloudflare Email Routing is live on all three domains).
* **errors:** `AccountError` and `RosterQueryError` join `CONTRACT_ERRORS`.
  `contract_error_response` writes a refusal's `extra` fields beside `detail` and `code`,
  never in their place.

## [0.3.0] (2026-10-06)

The account half of the sign-in and sign-up harmonisation (ui-kit 0.29.0,
`docs/auth-harmonization.md` §8). Additive: no name is removed or renamed.

### Added

* **auth:** a new subpackage, Layer 1 like the rest — no database, no app `User`, no
  routes, and no JWT signing (each app keeps its library).
  * **accounts:** `normalise_email` (trim + lower-case, never strips a `+tag`);
    `tagged_variant` (keksdose's `taggedEmail`); `invitation_accepts` (the exact address
    or the app's own tag on it); `verified_by_invitation` (the exact address only — a
    tagged sign-up verifies by its own mail); `addresses_for_reset` (the submitted address,
    then its tagged variant); `registration_decision` → `RegistrationDecision`
    (`FIRST_ADMIN` / `INVITED` / `CLOSED`) with `parse_env_list` / `env_list_match` — the
    environment list only guards the bootstrap: when set, the first account must be on it,
    and it grants nothing after; `full_name` ("First Last"; hu "Last First"; zh
    "LastFirst" for a name in CJK script, a Latin name as written); `name_incomplete` (never for a demo); `erasure_identifiers` (the email,
    the old display name, the full name in every order, longest first, never a bare first
    or last name) for `feedback.anonymise_feedback`.
  * **tokens:** `mint` → `MintedToken(raw, digest)`, `hash_token`, `is_expired`;
    `RESET_TTL` 1 h, `VERIFY_TTL` 48 h, `INVITE_TTL` 14 d. `access_claims`,
    `refresh_claims`, `challenge_claims` (`ChallengeKind`) build `{sub, type, iat, exp}`
    with a fractional `iat` and refuse an `extra` that overrides them or carries a name,
    an email or a locale; `token_is_revoked(iat, sessions_invalid_before)` is keksdose's
    exact, fail-closed check. `ACCESS_TOKEN_LIFETIME` 24 h and `REFRESH_TOKEN_LIFETIME`
    30 d as the settings' defaults.
  * **limits:** `AuthLimiters` — login, the 2FA / set-password steps (per IP and per
    challenge subject), registration, the reset request (10 / h per IP, 3 / h per address),
    verification resends — each a `Budget` an app may replace; `LoginFailureThrottle` and
    `login_failure_delay`: failures per address slow the answer down (five free, then 1 s
    doubling to 30 s) and never lock it out, because a lock per address is a
    denial-of-service lever.
  * **schemas:** `RegisterRequest`, `LoginRequest`, `TokenResponse[UserT]`,
    `TwoFactorChallenge`, `PasswordChangeChallenge`, `UserResponse` (computed, read-only
    `display_name` and `name_incomplete`; the `name_completion_exempt()` hook),
    `ProfileUpdate` (`extra="forbid"`, explicit nulls refused); the types `Email` (no
    `email-validator` needed), `NewPassword` (8 characters, 72 bytes), `ExistingPassword`,
    `PersonName` (1–120 after trimming), `LocaleTag`.
  * **errors:** `AuthErrorCode` (`invalid_credentials`, `registration_closed`,
    `email_taken`, `invitation_invalid`, `invitation_expired`, `token_invalid`) and
    `AuthError(code, detail=None)` with `status_code` and `code`, in `CONTRACT_ERRORS`.
* **mail:** a new module. `MailText`, `MailMessage`, `pick(locale, texts)` (the exact tag,
  its language, then `de-CH` → `de` → `en`), `render_message` (keksdose's layout, every
  part escaped, the link required http(s) and escaped in the `href`), the `Mailer`
  protocol, `ConsoleMailer` and `ResendClient` — behind the new optional `mail` extra
  (`httpx>=0.28`): injected key and sender, a timeout per attempt, one retry only on a
  transport error, 429 or 5xx under one `Idempotency-Key`, never raises, never logs the
  recipient. `List-Unsubscribe` waits for the notifications round.
* **errors:** `contract_error_response` adds `"code"` to the body for a refusal that
  carries one; every other refusal answers exactly as before.
* **translation_review:** `area_like_patterns(areas)` and `LIKE_ESCAPE` — `in_areas` as
  SQL `LIKE` patterns, for a listing that filters by area in the query (Kurvenschmiede's
  finding: its SQL filter bypassed the `kit.<area>.` arm).
* **limiter:** `SlidingWindowRateLimiter.count(key)` and `forget(key)`.

## [0.2.1] (2026-10-05)

The legal harmonization's backend half (ui-kit 0.28.0). A patch: no name is added,
removed or renamed.

### Fixed

* **translation_review:** `in_areas(key, areas)` also matches `kit.<area>.` — the kit's
  wording for an area, flattened under `kit.` in every app. ui-kit 0.28 moves the legal
  pages' shared sections into the kit, so a reviewer granted `["legal"]` was refused
  `kit.legal.*` (hidden rows, 403 on a verdict) and the lawyer could not review the text
  all three apps share. The same rule as the kit's `keyInArea`.
* **cors:** the module docstring no longer speaks of the SPA's refresh cookie; no app has
  one (the refresh token travels in the JSON body).

## [0.2.0] (2026-10-04)

What keksdose asked for on switching to 0.1.0. Additive: no name is removed or renamed,
and every new parameter defaults to the canon's behaviour.

### Added

* **errors:** `install_contract_error_handlers(app)` registers `contract_error_response`
  (`{"detail": str(exc)}` at `exc.status_code`) for every class in `CONTRACT_ERRORS`, so a
  kit refusal no endpoint caught keeps its 403 / 413 / 415 / 422 beside an app-wide
  `ValueError` → 400 handler (keksdose's and Kurvenschmiede's). Starlette resolves
  handlers along the MRO, so the order of registration does not matter.
* **uploads:** `read_capped_upload(file, max_bytes)` reads a Starlette `UploadFile` one
  byte past the cap and no further: over it `UploadTooLargeError` (413), empty
  `EmptyUploadError` (400). `check_upload`'s docstring points at it instead of telling
  each app to write the read itself.
* **uploads:** `ensure_decodable_image(data, media_type)` behind the optional `images`
  extra (`pillow>=11`): keksdose's dev#510 guard, lifted as it is — Pillow opens any
  `image/*` and `verify()`s it (no `load()`), every failure is `NotAnImageError` (400), the
  decompression-bomb limit is Pillow's own `MAX_IMAGE_PIXELS`, and non-image types and
  `UNDECODABLE_IMAGE_TYPES` (HEIC/HEIF) pass unparsed. A truncated PNG or WebP is refused;
  a JPEG or GIF cut off mid-raster still passes. Without the extra every call raises
  `ImportError` naming it.
* **translation_review:** `TranslationLocaleError` and `TranslationAreaError` carry
  `status_code = 422`, `TranslationAccessError` `403` (keksdose's mapping), and are in
  `CONTRACT_ERRORS`.

### Fixed

* **uploads:** declared `text/plain` without a NUL byte is text in any encoding again —
  keksdose's policy, which its support chat shares; 0.1.0 took Kurvenschmiede's UTF-8 rule
  and refused a Windows-1252 export with a 415. Text is only ever served as an
  `attachment`, so its encoding is no security property. `is_plain_text`, `sniffed_type`,
  `check_upload` and `store_attachment` take `require_utf8_text=False`;
  **Kurvenschmiede passes `True`** to keep its rule.

### Docs

* `decide_crash`'s candidate is the newest row with the fingerprint whose status is NOT
  terminal — the app's query filters `TERMINAL_STATUSES` (keksdose
  `feedback_service.py:312-318`); the newest row of any status filed a duplicate when an
  older row was re-opened after a newer one was settled (kastlan's finding).
* README: the 50 000-character `body` cap and the 8 KB `context` cap are behaviour changes
  for an app switching over (a rework on a long body answers 422; a huge URL in `context`
  can refuse a report — the ui-kit will cap it client-side); how each app installs the
  error handlers.

## [0.1.0] (2026-10-04)

Layer 1: the cross-app contracts as code (`docs/feedback-harmonization.md` §3 in
`Eifi1/ui-kit`), lifted from keksdose's backend with a `file:line` citation on every rule.

### Added

* **feedback:** the contract's schemas (`FeedbackCreate`, `FeedbackUpdate` with the
  explicit-null guard, `FeedbackResponse`, `FeedbackAttachmentResponse`,
  `CrashReportCreate` with `origin` / `environment`, `CrashReportResponse`), the 7
  statuses and 5 categories, the status sets and limits; `plan_update` (author lanes,
  server-side rework re-open, `resolved_at` enter/leave, CRASH refusal) as a pure
  function over the stored status and body; rework bodies (`append_rework`,
  `rework_count` anchored on `^---\s*REWORK\b`, `split_body_attachments`); crash filing
  (`crash_fingerprint` byte-identical to keksdose's, pinned by golden vectors;
  `decide_crash` fold-or-file; title, body and context builders); erasure
  (`FEEDBACK_CONTEXT_KEEP` with `environment`, `scrub_text`, `anonymise_feedback`,
  `attachment_keys_to_purge`); `AttachmentUrlPolicy` for `screenshot_url` AND
  `attachment_urls`, with each app's key pattern; `stamp_identity` and the 8 KB context cap.
* **uploads:** type detection by magic bytes (Kurvenschmiede's `sniffed_type`, no
  Pillow), `check_upload` with the contract's 400 / 413 / 415 refusals as exceptions,
  `store_attachment` over an app-supplied `save`, `inline_or_attachment`, a header-safe
  `content_disposition`.
* **limiter:** keksdose's `SlidingWindowRateLimiter` and a per-app `FeedbackLimiters`.
* **cors:** `ExtraOriginCorsMiddleware` with per-path methods and headers, never
  `Allow-Credentials`, stripping `CORSMiddleware`'s leak; `assert_outside_cors_middleware`;
  `parse_extra_origins`.
* **translation_review:** the review wire shapes, the `kit.` key scope, grants over an
  app's locales and areas, and the review-token format (per-app prefix, SHA-256 only).

### Fixed against keksdose (each app takes it with adoption)

* `screenshot_url` is validated like `attachment_urls` (contract §3.4 MUST).
* Attachment keys and URLs anchor with `\Z`: keksdose's `^…$` patterns accept a trailing
  newline.
* Erasure strips the body's rework `[screenshot]` lines and purges their objects under the
  same shared-key check as the two picture columns.
