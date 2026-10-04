# eifi1-server-kit

The backend sibling of [`@eifi1/ui-kit`](https://github.com/Eifi1/ui-kit): the contracts
the three apps — keksdose, kastlan, Kurvenschmiede — share, as Python code, so each rule
is written once instead of three times.

The kit owns every visible word and every client part; this package owns the server side
of the same contracts. The source of every rule is keksdose's backend (the canon), cited
`file:line` in the docstrings; the contract is
[`docs/feedback-harmonization.md`](https://github.com/Eifi1/ui-kit/blob/main/docs/feedback-harmonization.md)
§3 in the ui-kit.

- Distribution `eifi1-server-kit`, import package `eifi1_server_kit`, Python ≥ 3.14.
- Dependencies: `pydantic>=2.10`, `starlette>=0.40` — both already in every app through
  FastAPI, with no ceilings to fight an app's own pins (keksdose holds FastAPI < 0.137).
  One optional extra, `images` (`pillow>=11`), for `uploads.ensure_decodable_image` only.
- Typed (`py.typed`, mypy `--strict`), 100 % line and branch coverage.

## Layering

| Layer | What | Status |
|---|---|---|
| **1 — contracts as code** | pure functions, Pydantic models, one Starlette middleware. **No** database models, migrations, auth, repositories or routes | **0.1.0** |
| 2 — services behind ports | the feedback service written against a small repository / storage / clock port each app implements; router factories (`feedback_router(deps)`) with per-app hooks (`may_file(user)` for keksdose's demo sessions, the 404-vs-403 choice for a foreign row) | later |

Layer 1 never imports an app's `User`: the rules take plain values (`is_admin`,
`is_author`, the row's stored status and body), refusals are plain exceptions carrying a
`status_code`, and the queries — RLS, kastlan's `company_id` — stay in the app.

## What is in it

### `eifi1_server_kit.feedback`

| Area | Names |
|---|---|
| Enums and sets | `FeedbackStatus` (7), `FeedbackCategory` (5, CRASH first), `PICKABLE_CATEGORIES`, `AUTHOR_EDITABLE_STATUSES`, `TERMINAL_STATUSES`, `REWORKABLE_STATUSES`, `AWAITING_STATUSES` |
| Schemas | `FeedbackCreate`, `FeedbackUpdate`, `FeedbackResponse`, `FeedbackAttachmentResponse`, `CrashReportCreate`, `CrashReportResponse` — subclass them; the knobs are class variables (`attachment_url_policy`, `max_context_size`) |
| Limits | `MAX_ATTACHMENT_URLS` (5), `MAX_ATTACHMENT_BYTES` (10 MB), `ACCEPTED_MEDIA_TYPES`, `MAX_TITLE_LENGTH` (255), `MAX_BODY_LENGTH` (50 000), `MAX_CONTEXT_SIZE` (8 KB), `NOT_NULLABLE_FIELDS` |
| PATCH rules | `plan_update(status=, body=, changes=, is_admin=, is_author=)` → `UpdatePlan(changes, reworking, reopened)`; `reject_manual_crash`, `check_author_edit`, `reopens`, `resolved_at_change` |
| Rework bodies | `append_rework`, `rework_count` (anchored on `^---\s*REWORK\b`), `is_rework_append`, `split_body_attachments`, `body_attachment_urls`, `rework_stamp` |
| Crash filing | `crash_fingerprint(payload, user_id)` (byte-identical to keksdose's), `decide_crash(candidate_status)` → `CrashDecision.FOLD` / `FILE` (the candidate is the newest row with the fingerprint whose status is NOT in `TERMINAL_STATUSES` — filter that in the query, as keksdose does), `fold_crash_context`, `crash_title`, `crash_body`, `crash_context`, `crash_seen_at`, `crash_reference` |
| Erasure | `FEEDBACK_CONTEXT_KEEP`, `ERASED_MARK`, `scrub_text`, `scrub_context`, `anonymise_feedback`, `feedback_attachment_urls`, `attachment_keys_to_purge` |
| Attachment URLs | `AttachmentUrlPolicy(key_pattern, prefix)`, `OPAQUE_KEY_PATTERN` (default), `SHA12_KEY_PATTERN` (keksdose), `UUID32_SHA12_KEY_PATTERN` (kastlan) |
| Context | `stamp_identity` (the server writes `user_*` from the session), `context_size`, `CONTEXT_KEYS` |
| Errors | `FeedbackError` → `FeedbackValidationError` (422: `CrashCategoryNotAssignableError`, `UnknownAttachmentUrlError`), `FeedbackForbiddenError` (403) — all `ValueError`s with a `status_code` |

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
the module docstring records keksdose's order and the contract's recommendation.

### `eifi1_server_kit.cors`

`ExtraOriginCorsMiddleware(app, origins=, routes={path: ExtraOriginRoute(methods, headers)})`
— exact extra origins on listed paths, preflight 204, never `Allow-Credentials` (and strips
the one Starlette's `CORSMiddleware` adds to every response with an `Origin`).
Add it **after** `CORSMiddleware` and call `assert_outside_cors_middleware(app)`.
`parse_extra_origins(raw, own_origin=)` reads the setting; `TRANSLATION_REVIEW_ROUTES` are
the showcase's three paths.

### `eifi1_server_kit.errors`

`install_contract_error_handlers(app)` registers `contract_error_response` — `{"detail":
str(exc)}` at the exception's `status_code` — for each of `CONTRACT_ERRORS`: `FeedbackError`,
`UploadRejectedError` and the translation review's `TranslationLocaleError` (422),
`TranslationAreaError` (422) and `TranslationAccessError` (403). See [the refusals](#the-refusals).

### `eifi1_server_kit.translation_review`

The wire shapes (`TranslationReviewsResponse`, `TranslationReviewBatch`,
`TranslationReviewClear`, `TranslationReviewOut`, `TranslationVerdict`, the token
responses), the `kit.` scope (`is_kit_key`, `require_kit_keys`), grants over the app's own
locales and areas (`review_grant`, `can_review_kit`, `assert_allowed`, `normalize_locales`,
`normalize_areas`, `ReviewerProfile`), and the review token format (`new_review_token(prefix)`
= prefix + 43 URL-safe characters, `hash_review_token` = SHA-256, `tokens_to_retire`,
`review_token_is_live`, `review_token_expiry`).

## Installing it in an app

Apps depend on a **published** version — the wheel attached to a tagged GitHub Release, whose hash `uv.lock` pins —
never on a path outside their repository (a build must not need anything beside it):

```sh
uv add "eifi1-server-kit @ https://github.com/Eifi1/server-kit/releases/download/v0.1.0/eifi1_server_kit-0.1.0-py3-none-any.whl"
```

With the image guard, name the extra: `"eifi1-server-kit[images] @ https://…/eifi1_server_kit-<version>-py3-none-any.whl"`.

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
  UTF-8 rule for text (the kit's default since 0.1.1 is keksdose's: declared `text/plain`
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

## Developing

```sh
uv sync
bash scripts/check.sh      # ruff, ruff format --check, mypy --strict, pytest + coverage, uv build
```

CI (`.github/workflows/ci.yml`) runs the same script on Python 3.14. Commits follow
Conventional Commits; a release is a tag `v<version>` on `main` with `pyproject.toml`,
`__version__` and `CHANGELOG.md` agreeing (a test pins it).

## License

MIT — see [LICENSE](LICENSE).
