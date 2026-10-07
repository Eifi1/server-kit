# Changelog

All notable changes to `eifi1-server-kit` — the backend sibling of `@eifi1/ui-kit`.

The contract until 1.0 is the kit's: a **minor** (`0.x.0`) may remove or rename a public
name or change a rule's behaviour, and says so under **⚠ BREAKING CHANGES**; a **patch**
(`0.x.y`) fixes behaviour without changing the surface. Apps pin an exact tag (its commit
hash lands in their `uv.lock`), so nothing reaches an app until it asks for it. Write the
entry in the Conventional Commit; this file is assembled from them at release.

## [Unreleased]

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
