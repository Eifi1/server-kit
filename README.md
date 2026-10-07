# eifi1-server-kit

The backend sibling of [`@eifi1/ui-kit`](https://github.com/Eifi1/ui-kit): the contracts
the three apps — keksdose, kastlan, Kurvenschmiede — share, as Python code, so each rule
is written once instead of three times.

The kit owns every visible word and every client part; this package owns the server side
of the same contracts. The source of every rule is keksdose's backend (the canon), cited
`file:line` in the docstrings; the contracts are
[`docs/feedback-harmonization.md`](https://github.com/Eifi1/ui-kit/blob/main/docs/feedback-harmonization.md)
§3, [`docs/auth-harmonization.md`](https://github.com/Eifi1/ui-kit/blob/main/docs/auth-harmonization.md)
§8 and [`docs/user-admin-harmonization.md`](https://github.com/Eifi1/ui-kit/blob/main/docs/user-admin-harmonization.md)
§7 in the ui-kit.

- Distribution `eifi1-server-kit`, import package `eifi1_server_kit`, Python ≥ 3.14.
- Dependencies: `pydantic>=2.10`, `starlette>=0.40` — both already in every app through
  FastAPI, with no ceilings to fight an app's own pins (keksdose holds FastAPI < 0.137).
  Two optional extras: `images` (`pillow>=11`) for `uploads.ensure_decodable_image` only,
  and `mail` (`httpx>=0.28`) for `mail.ResendClient` only. No JWT library: the kit builds
  and checks claims, each app signs with its own.
- Typed (`py.typed`, mypy `--strict`), 100 % line and branch coverage.

## Layering

| Layer | What | Status |
|---|---|---|
| **1 — contracts as code** | pure functions, Pydantic models, small classes, one Starlette middleware. **No** database models, migrations, sessions, repositories or routes | **0.1.0** |
| 2 — services behind ports | the feedback service written against a small repository / storage / clock port each app implements; router factories (`feedback_router(deps)`) with per-app hooks (`may_file(user)` for keksdose's demo sessions, the 404-vs-403 choice for a foreign row) | later |

Layer 1 never imports an app's `User`: the rules take plain values (`is_admin`,
`is_author`, the row's stored status and body, `is_first_user`, a token's `iat` and the
account's `sessions_invalid_before`), refusals are plain exceptions carrying a
`status_code`, and the queries — RLS, kastlan's `company_id`, the invitation rows — stay in
the app.

## What is in it

### `eifi1_server_kit.feedback`

| Area | Names |
|---|---|
| Enums and sets | `FeedbackStatus` (8; `READY` between `OPEN` and `IN_PROGRESS`), `FeedbackCategory` (5, CRASH first), `PICKABLE_CATEGORIES`, `AUTHOR_EDITABLE_STATUSES`, `TERMINAL_STATUSES`, `REWORKABLE_STATUSES`, `AWAITING_STATUSES` |
| Schemas | `FeedbackCreate`, `FeedbackUpdate`, `FeedbackResponse`, `FeedbackAttachmentResponse`, `CrashReportCreate`, `CrashReportResponse` — subclass them; the knobs are class variables (`attachment_url_policy`, `max_context_size`) |
| Limits | `MAX_ATTACHMENT_URLS` (5), `MAX_ATTACHMENT_BYTES` (10 MB), `ACCEPTED_MEDIA_TYPES`, `MAX_TITLE_LENGTH` (255), `MAX_BODY_LENGTH` (50 000), `MAX_CONTEXT_SIZE` (8 KB), `NOT_NULLABLE_FIELDS` |
| Statuses the server decides | `initial_status(author_is_admin=, crash=False)` (READY for an admin's own report, OPEN otherwise and for every crash); `rework_status(actor_is_admin=)` (a rework goes back to READY from an admin, to OPEN from anyone else) |
| PATCH rules | `plan_update(status=, body=, changes=, is_admin=, is_author=)` → `UpdatePlan(changes, reworking, reopened)`; `reject_manual_crash`, `check_author_edit`, `reopens`, `resolved_at_change` |
| Rework bodies | `append_rework`, `rework_count` (anchored on `^---\s*REWORK\b`), `is_rework_append`, `split_body_attachments`, `body_attachment_urls`, `rework_stamp` |
| Crash filing | `crash_fingerprint(payload, user_id)` (byte-identical to keksdose's), `decide_crash(candidate_status)` → `CrashDecision.FOLD` / `FILE` (the candidate is the newest row with the fingerprint whose status is NOT in `TERMINAL_STATUSES` — filter that in the query, as keksdose does), `fold_crash_context`, `crash_title`, `crash_body`, `crash_context`, `crash_seen_at`, `crash_reference` |
| Erasure | `FEEDBACK_CONTEXT_KEEP`, `ERASED_MARK`, `scrub_text`, `scrub_context`, `anonymise_feedback`, `feedback_attachment_urls`, `attachment_keys_to_purge` |
| Attachment URLs | `AttachmentUrlPolicy(key_pattern, prefix)`, `OPAQUE_KEY_PATTERN` (default), `SHA12_KEY_PATTERN` (keksdose), `UUID32_SHA12_KEY_PATTERN` (kastlan) |
| Context | `stamp_identity` (the server writes `user_*` from the session), `context_size`, `CONTEXT_KEYS` |
| Errors | `FeedbackError` → `FeedbackValidationError` (422: `CrashCategoryNotAssignableError`, `UnknownAttachmentUrlError`), `FeedbackForbiddenError` (403) — all `ValueError`s with a `status_code` |

**READY, the triage step** (feedback contract §8.2): OPEN means "filed, not yet triaged",
and an admin releases a row for implementation by setting READY. The server decides the
first status at submit — `initial_status(author_is_admin=…)` — and the client never sends
one with a new row. A rework sent by an admin goes back to READY, anyone else's to OPEN;
`plan_update` does it from its `is_admin`. The author may edit while OPEN, READY or
IN_PROGRESS. An app whose status column is a database enum adds the label first
(`ALTER TYPE feedbackstatus ADD VALUE 'READY' AFTER 'OPEN'`); existing OPEN rows stay OPEN.

### `eifi1_server_kit.uploads`

`check_upload(data, declared_type)` → `CheckedUpload(media_type, extension, disposition,
size)`, deciding by the bytes (PNG, JPEG, GIF, WebP, `%PDF-`; text = declared `text/plain`
without a NUL byte, in any encoding — keksdose's policy, since text is only ever served as
an `attachment`; `require_utf8_text=True` adds Kurvenschmiede's UTF-8 rule); refusals `EmptyUploadError` (400),
`UploadTooLargeError` (413), `NotAnImageError` (400), `UnsupportedUploadTypeError` (415).
`await read_capped_upload(file, max_bytes)` reads a Starlette `UploadFile` one byte past
the cap and no further, refusing over it (413) or empty (400) — read every upload with it.
`store_attachment(data, declared_type, save=…, mint_key=…)` adds keksdose's
content-addressed key and hands the bytes to the app's `save`. Also `sniffed_type`,
`signature_type`, `inline_or_attachment`, `content_disposition`, `content_addressed_key`,
`media_type_for_extension`.

`ensure_decodable_image(data, media_type)` — the **`images` extra** — is keksdose's
dev#510 guard: after `check_upload`, with the detected type, Pillow opens any `image/*` and
`verify()`s it (no `load()`), refusing what it cannot parse as `NotAnImageError` (400) — a
PNG with only its signature, a truncated PNG or WebP, a decompression bomb past Pillow's own
`MAX_IMAGE_PIXELS`. A JPEG or GIF cut off mid-raster still passes (`verify()` does not
decode). Other types and `UNDECODABLE_IMAGE_TYPES` (HEIC/HEIF) pass unparsed. Pillow is
imported lazily; without the extra every call raises `ImportError` naming it.

### `eifi1_server_kit.limiter`

`SlidingWindowRateLimiter(max_hits=, window_seconds=, clock=)` with `hit(key) -> float |
None` and `reset()`; `FeedbackLimiters` (20 uploads and 20 crashes per user per hour);
`retry_after_header`. **Process-local**: each worker and each Cloud Run instance counts on
its own. Build limiters per app (`app.state`), never as module globals. When to charge —
before or after validation, before or after the crash dedupe — is the caller's choice;
the module docstring records keksdose's order and the contract's recommendation. `count(key)` reads a key's live window without charging it,
`forget(key)` drops one key's window. The sign-in budgets are
[`auth.AuthLimiters`](#eifi1_server_kitauth).

### `eifi1_server_kit.cors`

`ExtraOriginCorsMiddleware(app, origins=, routes={path: ExtraOriginRoute(methods, headers)})`
— exact extra origins on listed paths, preflight 204, never `Allow-Credentials` (and strips
the one Starlette's `CORSMiddleware` adds to every response with an `Origin`).
Add it **after** `CORSMiddleware` and call `assert_outside_cors_middleware(app)`.
`parse_extra_origins(raw, own_origin=)` reads the setting; `TRANSLATION_REVIEW_ROUTES` are
the showcase's three paths.

### `eifi1_server_kit.errors`

`install_contract_error_handlers(app)` registers `contract_error_response` — `{"detail":
str(exc)}` at the exception's `status_code`, plus `"code"` for a refusal that has one — for
each of `CONTRACT_ERRORS`: `FeedbackError`, `UploadRejectedError`, the translation review's
`TranslationLocaleError` (422), `TranslationAreaError` (422) and `TranslationAccessError`
(403), `auth.AuthError` (`{"detail": "Invalid credentials", "code":
"invalid_credentials"}` at 401, …), and user administration's `AccountError` (409) and
`RosterQueryError` (422). An `AccountError`'s `extra` fields go beside `detail` and `code`,
as in kastlan's `{"detail": …, "code": "last_admin", "companies": [...]}`. See
[the refusals](#the-refusals).

### `eifi1_server_kit.translation_review`

The wire shapes (`TranslationReviewsResponse`, `TranslationReviewBatch`,
`TranslationReviewClear`, `TranslationReviewOut`, `TranslationVerdict`, the token
responses), the `kit.` scope (`is_kit_key`, `require_kit_keys`), grants over the app's own
locales and areas (`review_grant`, `can_review_kit`, `assert_allowed`, `normalize_locales`,
`normalize_areas`, `ReviewerProfile`), and the review token format (`new_review_token(prefix)`
= prefix + 43 URL-safe characters, `hash_review_token` = SHA-256, `tokens_to_retire`,
`review_token_is_live`, `review_token_expiry`).

**An app that filters a listing by area in SQL** builds the filter from
`area_like_patterns(grant.areas)` — the three arms of `in_areas` per area (`["legal"]` →
`["legal", "legal.%", "kit.legal.%"]`, `None` = no filter) — and never by hand:

```python
from eifi1_server_kit.translation_review import LIKE_ESCAPE, area_like_patterns

patterns = area_like_patterns(grant.areas)
if patterns is not None:
    query = query.where(or_(*(Row.key.like(p, escape=LIKE_ESCAPE) for p in patterns)))
```

Kurvenschmiede found its listing narrowing with `key LIKE 'legal.%'` beside the kit's
`in_areas`, so 0.2.1's `kit.<area>.` arm reached the verdict check and not the list. Name
`LIKE_ESCAPE` in the query (SQLite has no default escape); `LIKE` is case-sensitive in
PostgreSQL, as `in_areas` is.

### `eifi1_server_kit.auth`

Sign-in, sign-up and the account (auth contract §3–§6, §8). keksdose is the reference.

| Area | Names |
|---|---|
| Addresses | `normalise_email` (trim + lower-case, never strips a `+tag`); `tagged_variant(email, tag)` (keksdose's `taggedEmail`: `None` when there is already a `+` or nothing to split); `invitation_accepts(invited, registered, tag)` (the exact address or the app's own tag on it); `verified_by_invitation(invited, registered)` (the exact address only — a tagged sign-up verifies by its own mail); `addresses_for_reset(email, tag)` (the submitted address, then its tagged variant) |
| The gate | `registration_decision(is_first_user=, on_env_list=, has_valid_invitation=)` → `RegistrationDecision.FIRST_ADMIN` / `INVITED` / `CLOSED` (`.allowed`, `.first_admin`); `parse_env_list`, `env_list_match` (`None` = no list set) |
| Names | `full_name(first, last, locale)` ("First Last"; hu "Last First"; zh "LastFirst" for a CJK name, a Latin one as written; a missing part leaves the other), `name_incomplete(first, last, is_demo=)`, `erasure_identifiers(email, first, last, old_display_name)` |
| One-time tokens | `mint(prefix="")` → `MintedToken(raw, digest)`, `hash_token` (SHA-256 hex), `is_expired(issued_at, ttl, now)`; `RESET_TTL` 1 h, `VERIFY_TTL` 48 h, `INVITE_TTL` 14 d, `EMAIL_CHANGE_TTL` 48 h; `OneTimeTokenKind` (`password_reset`, `verification`, `invitation`, `email_change` — the mail kinds too) with `ONE_TIME_TOKEN_TTLS`, `EMAIL_CHANGE_TOKEN_KIND` |
| Session claims | `access_claims(sub, now=, lifetime=, extra=)`, `refresh_claims(…)`, `challenge_claims(sub, ChallengeKind, now=)` (`{sub, type, iat, exp}`, `iat` fractional; `extra` may not override those nor carry a name, email or locale); `token_is_revoked(iat, sessions_invalid_before)`; `ACCESS_TOKEN_LIFETIME` 24 h, `REFRESH_TOKEN_LIFETIME` 30 d (also `*_EXPIRE_MINUTES`) |
| Limits | `AuthLimiters(clock=)`: `login` (30 / 5 min per IP), `challenge` (20 / 5 min per IP), `challenge_subject` (10 / 15 min per user), `register` (5 / 5 min per IP), `reset_ip` (10 / h), `reset_address` (3 / h), `verification_resend` (10 / h per recipient), `login_failures` (`LoginFailureThrottle`); each a `Budget` an app may replace |
| Schemas | `RegisterRequest`, `LoginRequest`, `TokenResponse[UserT]`, `TwoFactorChallenge`, `PasswordChangeChallenge`, `UserResponse` (computed `display_name`, `name_incomplete`, the `name_completion_exempt()` hook), `ProfileUpdate`; the types `Email`, `NewPassword` (8 characters, 72 bytes), `ExistingPassword`, `PersonName` (1–120 trimmed), `LocaleTag` |
| Refusals | `AuthErrorCode` (`invalid_credentials` 401, `registration_closed` 403, `email_taken` 409, `invitation_invalid` / `invitation_expired` / `token_invalid` / `token_expired` 400), `AuthError(code, detail=None)` |

**The kit signs no JWT.** Each app keeps its library and secret and hands the claim dicts
to it: `jwt.encode(kit.access_claims(user.id, now=now, extra={"role": user.role}), …)`.
On every request and on refresh: `if kit.token_is_revoked(claims.get("iat"),
user.sessions_invalid_before): …401`. The comparison is exact, so a token minted after a
cut-off in the same second survives.

**One-time tokens are stored hashed** — reset, verification and invitation alike (keksdose
kept its verification token in plaintext). Mail `raw`, store `digest`, look up by
`hash_token(submitted)`, delete the row when it is redeemed, and delete the account's
earlier rows when minting a new one.

**Per IP a hard limit, per address only a delay** (§5.2). A lock per address would let
anyone lock a known user out by failing on purpose, so `LoginFailureThrottle` counts
failures per submitted address — known or not — and slows the answer down: five free,
then 1 s doubling to 30 s, cleared by a success. Wait BEFORE the password check on every
attempt, or a fast "right" answer tells a guesser that the slow ones were wrong:

```python
limiters = app.state.auth_limiters  # AuthLimiters(), built in create_app
if (wait := limiters.login.hit(client_ip(request))) is not None:
    raise HTTPException(429, headers={"Retry-After": retry_after_header(wait)})
await asyncio.sleep(limiters.login_failures.delay(payload.email))
user = await authenticate(session, payload.email, payload.password)  # dummy hash for unknown addresses
if user is None:  # unknown, wrong password, deactivated — one answer
    limiters.login_failures.record_failure(payload.email)
    raise kit.AuthError(kit.AuthErrorCode.INVALID_CREDENTIALS)
limiters.login_failures.clear(payload.email)
```

**The gate.** `registration_decision` makes the first account the admin and then lets in
only a valid invitation — an allow-list entry is one (§2.12). The environment list does
one job now: when it is set, the first account must be on it, so a stranger cannot claim a
fresh deployment; after that it grants nothing. Pass `on_env_list=env_list_match(email,
settings_list)`; `None` (no list) keeps keksdose's open first registration.

**Erasure.** `anonymise_feedback(…, identifiers=kit.erasure_identifiers(user.email,
user.first_name, user.last_name, old_display_name))`: the email, the old display name and
the full name in every order `full_name` writes — never a bare first or last name, which
would shred a report mentioning "Mai" or "Bank" — longest first, so `scrub_text` cannot
leave half a name behind.

**Subclass the schemas**: the app's fields (kastlan's company, keksdose's currency and its
challenge's `encrypted`), its `role` enum, and `name_completion_exempt()` returning
`self.is_demo` for keksdose's demo. `TokenResponse[MyUserResponse]` types the user.

### `eifi1_server_kit.user_admin`

User administration and the account's own settings (user-admin contract §3–§7).
keksdose and Kurvenschmiede are the reference. The tables (`admin_actions`, the token
rows), the queries, RLS and company scoping, what an erasure deletes and what an export
holds stay in the app.

| Area | Names |
|---|---|
| Rules | `refuse_self(actor_id, target_id)`; `refuse_last_admin(target_is_admin=, active_admins=, change_removes_admin=)` (kastlan counts per company); `confirm_email_matches(target_email, typed)` (normalised, so case and spaces never fail it and a `+tag` is part of it); `require_confirmation(level, target_email=, acknowledged=, confirm_email=)` |
| Actions and levels | `AdminAction` (the §4.3 list: `deactivate`, `reactivate`, `role`, `membership_remove`, `password_change_require` / `_withdraw`, `mail_verification` / `mail_reset`, `reviewer`, `invite`, `invite_resend`, `invite_revoke`, `transfer`, `deletion_request`, `deletion_cancel`, `erase`); `ConfirmationLevel` (`none` / `acknowledge` / `type_email`), `CONFIRMATION_LEVELS`, `confirmation_level(action, at_least=)` |
| User list | `parse_roster_query(limit=, offset=, sort=, q=, role=, state=, sort_keys=, accepted_states=, roles=)` → `UserListQuery` (`.search_pattern` for `LIKE`), `parse_sort`, `parse_tokens`, `RosterSort`, `SORT_KEYS`, `STATE_TOKENS`, `DEFAULT_ROSTER_SORT` (newest first), `DEFAULT_PAGE_SIZE` 25, `MAX_PAGE_SIZE` 200, `RosterQueryError` (422), `LIKE_ESCAPE` |
| Audit | `admin_action_record(action, actor_id=, target_user_id=, target_email=, detail=, company_id=, now=)` → the row's fields; `audit_detail` (ids, roles, flags and counts only), `AuditDetailError`, `DETAIL_TOKEN_MAX_LENGTH` 64, `DETAIL_MAX_BYTES` 2048, `DETAIL_MAX_DEPTH` 3 |
| Schemas | `AdminUserRow`, `UserListResponse[RowT]`, `ActionConfirmation` → `ActiveChange`, `RoleChange`, `RolesChange`, `MailRequest` (`MailKind`); `MailResult` (a link only on the console, `MAIL_BACKEND_CONSOLE`); `InvitationCreate`, `InvitationRow`, `InvitationStatus`, `invitation_status(…)`; `ReviewerUpdate`; `PersonRef`; `AdminActionRow`; `EmailChangeRequest`, `EmailChangeConfirm`, `DeletionRequest` |
| Deletion | `DeletionMode` (`after_days`, `operator`), `deletion_schedule(now, mode, days=30)`, `deletion_due(requested_at, days, now, scheduled_at=)`, `deletion_mail_retention_note(backup_days=7, log_days=30)` → `RetentionNote` |
| Export | `export_envelope(app, account, data, now)` (`"format": "eifi1-account-export"`, `"version": 1`), `export_filename`, `EXPORT_PER_USER` (once a minute), `NEVER_EXPORT`; `assert_no_secrets(obj, allow=)` / `secret_paths` → `ExportSecretError`; `looks_secret`, `looks_secret_key`, `looks_secret_value` |
| Refusals | `AccountErrorCode` (`last_admin`, `self_action`, `other_companies`, `household_has_members`, `confirmation_required`, `confirmation_mismatch`, all 409; `password_incorrect` 400, so a wrong current password never reads as an ended session), `AccountError(code, detail=None, extra=None)` |

**An admin action, in order**: the guards, the confirmation the server decided, the change,
and its `admin_actions` row in the same transaction:

```python
from eifi1_server_kit import user_admin as kit

kit.refuse_self(actor.id, target.id)
kit.refuse_last_admin(
    target_is_admin=target.role is Role.ADMIN and target.is_active,
    active_admins=await count_active_admins(session),  # kastlan: in this company
    change_removes_admin=not body.active,
)
kit.require_confirmation(
    kit.confirmation_level(kit.AdminAction.DEACTIVATE),
    target_email=target.email,
    acknowledged=body.acknowledged,
    confirm_email=body.confirm_email,
)
target.is_active, target.sessions_invalid_before = False, now
record = kit.admin_action_record(
    kit.AdminAction.DEACTIVATE,
    actor_id=actor.id,
    target_user_id=target.id,
    target_email=target.email,
    detail={"sessions_ended": True},
    now=now,
)
session.add(AdminActionModel(**record))  # the app's own model, in the same transaction
```

**The confirmation level is a floor.** `confirmation_level` answers the contract's level;
`at_least=` raises it and never lowers it. keksdose raises the reset mail and the forced
password change to `type_email` for an account whose key a password opens.

**The detail never holds content.** Kurvenschmiede encrypts notes, titles and bodies at
rest, and a plaintext copy in `detail` would carry them past the encryption, into a
table every admin reads and past the erasure scrub. So `audit_detail` accepts only short
tokens: a role, a locale, an ISO date. It refuses sentences, addresses, secrets and
anything that is not JSON. A refusal is a bug in the app, and its tests catch it.

**The user list** takes the contract's `-key` and DataTable's `key.desc` alike. An app
narrows or widens the state vocabulary: Kurvenschmiede has no `invited` rows, and keksdose
keeps `allowlisted`. It also adds its own sort keys. An unknown token is a 422, never an
empty page.

**The export check is a test.** Build the export of a fixture account with every table
filled, then run `kit.assert_no_secrets(export)`. It flags keys named like a secret and
values shaped like one: hashes, JWTs, digests, links with a token. It walks into
containers, so `api_tokens: [{name, scopes}]` passes. `allow=` names a field it flags
wrongly.

**Deletion** is two-stage: deactivated at once, erased later. In `after_days` mode the
erasure is a Cloud Scheduler → Cloud Run Job, one account per transaction, which
re-checks `deletion_due(…, scheduled_at=row.deletion_scheduled_at)` on the locked row. In
`operator` mode an operator erases from the platform. The email change's link lives
`auth.EMAIL_CHANGE_TTL` (48 h) as a `OneTimeTokenKind.EMAIL_CHANGE` row holding the new
address.

### `eifi1_server_kit.mail`

The **`mail` extra** (`httpx`) for `ResendClient` only; the rest needs nothing.

`MailText(subject, intro, body, cta, outro)` is one mail in one language — keep a table per
mail, keyed by full tags (`de-CH`, `en`) or bare languages (`de`, `en`). `pick(locale,
texts)` chooses the row: the exact tag, its language, then `de-CH` → `de` → `en`
(`FALLBACK_LOCALES`); a table with none of them is a `KeyError`. `pick_entry` also answers
the key it chose. `render_mail(user.locale, texts, link=…, name=…, hours=…)` picks the row
and renders it. It fills `{placeholders}` on the template only and lays the mail out with
`render_message`, which HTML-escapes every part, the subject included, so a name with
markup stays text. The link must be http(s) and is escaped in the `href` too. →
`MailMessage(subject, text, html)`.

The HTML is a **whole document**: `<!doctype html><html lang="…"><head><meta
charset="utf-8"><title>{subject}</title></head><body>…</body></html>`. The `lang` is the
row's own language, so a German fallback says `de-CH`. Outlook junked a fully
authenticated Kurvenschmiede mail that was only a fragment.

`Mailer` is the protocol (`await mailer.send(to, message, kind="verification") -> bool`,
never raises). `ResendClient(api_key=, from_address=, timeout=10, client=None,
reply_to=None)` posts to Resend, retrying once only on a transport error, `429` or `5xx`,
under one `Idempotency-Key`, and never logs the recipient. **Set `reply_to`** from the
app's `*_EMAIL_REPLY_TO` setting, whose default is `support_address(from_address)`, i.e.
`support@<the sender's domain>`. Cloudflare Email Routing forwards `support@` on all three
domains, and nobody reads `noreply@`. `ConsoleMailer(level=logging.INFO)` logs
the mail with its link — development only; kastlan passes `logging.WARNING`. Not yet:
`List-Unsubscribe` and the rest a notification needs (the notifications round).

## Installing it in an app

Apps depend on a **published** version — the wheel attached to a tagged GitHub Release, whose hash `uv.lock` pins —
never on a path outside their repository (a build must not need anything beside it):

```sh
uv add "eifi1-server-kit @ https://github.com/Eifi1/server-kit/releases/download/v0.4.0/eifi1_server_kit-0.4.0-py3-none-any.whl"
```

With the image guard or the Resend client, name the extra: `"eifi1-server-kit[images,mail] @ https://…/eifi1_server_kit-<version>-py3-none-any.whl"`.

The RELEASE WHEEL, not a `git+https` source: slim images (`python:3.14-slim`) have no git
binary, so uv cannot fetch a git source inside a Docker build (keksdose's finding). Each
tag `v*` runs `.github/workflows/release.yml`, which builds the wheel with the full gate and
attaches it to the GitHub Release; uv.lock pins its hash. To try a change locally without
touching an app's `pyproject.toml` or `uv.lock`, run its suite with the kit overlaid:
`uv run --with ../server-kit pytest`.

## Adopting layer 1

The same pattern in each app — the app keeps its models, queries, auth and routes:

```python
from eifi1_server_kit import feedback as kit


class FeedbackCreate(kit.FeedbackCreate):
    attachment_url_policy = kit.AttachmentUrlPolicy(kit.UUID32_SHA12_KEY_PATTERN)  # the app's own keys


# PATCH /feedback/{id}
try:
    kit.reject_manual_crash(payload.category)  # 422 before the lookup
    row = await load_visible(feedback_id)  # app query, RLS, 404
    plan = kit.plan_update(
        status=row.status,
        body=row.body,
        changes=payload.model_dump(exclude_unset=True),
        is_admin=is_admin(actor),
        is_author=row.user_id == actor.id,
    )
except kit.FeedbackError as exc:  # 422 / 403, the app may remap
    raise HTTPException(exc.status_code, str(exc)) from exc
for name, value in plan.changes.items():
    setattr(row, name, value)
```

Every app reads an upload with `read_capped_upload` and checks it with `check_upload`;
then, per app:

- **keksdose** (the source): swap its rule code for the imports, keep its key
  (`SHA12_KEY_PATTERN`), take the three fixes (screenshot URL check, `\Z` anchors, rework
  pictures purged on erasure) and keep its limiter order. Its dev#510 guard is the kit's
  `ensure_decodable_image` (the `images` extra) — the same check, raising `NotAnImageError`
  instead of an `HTTPException`.
- **kastlan**: `UUID32_SHA12_KEY_PATTERN`, `is_admin` from its maintainer role set,
  `FeedbackResponse` subclassed with `company_id` / `user_name`.
- **Kurvenschmiede**: its own key string (no `jpeg`), `stamp_identity`, `limiter.hit()`
  in place of `take()` (`None` instead of `0.0` when allowed), and
  `require_utf8_text=True` on `check_upload` / `sniffed_type` / `is_plain_text` to keep its
  UTF-8 rule for text (the kit's default since 0.2.0 is keksdose's: declared `text/plain`
  without NUL, any encoding).

### The refusals

Every kit refusal is a plain exception carrying the contract's status as `status_code`,
and most are `ValueError`s. keksdose (`main.py:166`) and Kurvenschmiede (`main.py:193`)
answer every uncaught `ValueError` with a 400 — so a kit refusal that got past an
endpoint's own `except` came out as 400 instead of 403, 415 or 422, and nothing said so.
One call while building the app closes that:

```python
from eifi1_server_kit.errors import install_contract_error_handlers

install_contract_error_handlers(app)  # FastAPI or Starlette; before, after or without a ValueError handler
```

Starlette picks a handler along the exception's MRO, so `FeedbackForbiddenError` reaches
the `FeedbackError` handler before the `ValueError` one, whichever was registered first;
an endpoint's own `except` still comes before both (Kurvenschmiede's 404 for a foreign row
stays its own).

- **keksdose**: call it in `create_app` beside `_value_error_to_400`; the routers'
  `except FeedbackError` / `except UploadRejectedError` mappings become optional.
- **Kurvenschmiede**: the same, beside its `_value_error_to_400`; keep `_refused` where an
  endpoint answers differently from the contract.
- **kastlan**: maps its refusals through `DomainError` (`main.py:112`) and may keep doing
  so for the kit's; it has no `ValueError` handler, so a kit refusal nobody caught is a 500
  there — the installer turns that into the contract's status too.

### Behaviour changes on switching over

Two caps are Kurvenschmiede's, now everyone's, and keksdose had neither:

- **`body` ≤ 50 000 characters** (`MAX_BODY_LENGTH`) on create AND on update. A rework
  PATCH sends the whole body with the round appended, so a rework on an already long report
  now answers 422 where it used to be stored. An app that needs more re-declares `body` on
  its `FeedbackCreate` / `FeedbackUpdate` subclass.
- **`context` ≤ 8 KB of JSON** (`MAX_CONTEXT_SIZE`, measured by `context_size`) on create.
  The ten keys fit in a fraction of it, but `url` carries the whole address, so a page with a
  huge query string could have its report refused with a 422. The ui-kit will cap the
  context URL client-side; until an app runs that version, `max_context_size = None` on its
  subclass lifts the cap. The crash payload is unaffected — `CrashReportCreate` truncates
  every field, it never refuses.

## Developing

```sh
uv sync
bash scripts/check.sh      # ruff, ruff format --check, mypy --strict, pytest + coverage, uv build, the API export
```

CI (`.github/workflows/ci.yml`) runs the same script on Python 3.14.

**The API export.** The kit's documentation lives in the ui-kit showcase, as its "Server
kit" group (Marcel, 2026-10-07). `scripts/export_api.py` reads the installed package —
each module's `__all__`, the signatures and docstrings as written, the `#:` comments of
constants and fields, enum values — and writes `dist/server-kit-api.json` (format
`eifi1-server-kit-api`, version 1), with each module's ui-kit contract and the mail layout
rendered over synthetic samples in `en` and `de-CH`. `check.sh` runs it right after `uv
build`, so every release attaches it beside the wheel and the showcase pins it the same
way. A new public module needs an `__all__` and a line in the exporter's `MODULES`; a test
fails until it has both. Commits follow
Conventional Commits; a release is a tag `v<version>` on `main` with `pyproject.toml`,
`__version__` and `CHANGELOG.md` agreeing (a test pins it).

## License

MIT — see [LICENSE](LICENSE).
