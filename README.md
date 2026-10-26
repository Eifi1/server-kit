# eifi1-server-kit

The backend sibling of [`@eifi1/ui-kit`](https://github.com/Eifi1/ui-kit): the contracts
the three apps — keksdose, kastlan, Kurvenschmiede — share, as Python code, so each rule
is written once instead of three times.

The kit owns every visible word and every client part; this package owns the server side
of the same contracts. The source of every rule is keksdose's backend (the canon), cited
`file:line` in the docstrings; the contracts are
[`docs/feedback-harmonization.md`](https://github.com/Eifi1/ui-kit/blob/main/docs/feedback-harmonization.md)
§3, [`docs/auth-harmonization.md`](https://github.com/Eifi1/ui-kit/blob/main/docs/auth-harmonization.md)
§8, [`docs/user-admin-harmonization.md`](https://github.com/Eifi1/ui-kit/blob/main/docs/user-admin-harmonization.md)
§7, [`docs/settings-harmonization.md`](https://github.com/Eifi1/ui-kit/blob/main/docs/settings-harmonization.md)
§6, [`docs/landing-demo-harmonization.md`](https://github.com/Eifi1/ui-kit/blob/main/docs/landing-demo-harmonization.md)
§6, [`docs/billing-harmonization.md`](https://github.com/Eifi1/ui-kit/blob/main/docs/billing-harmonization.md)
§10 with §14, [`docs/text-size-harmonization.md`](https://github.com/Eifi1/ui-kit/blob/main/docs/text-size-harmonization.md)
§6, [`docs/notifications-harmonization.md`](https://github.com/Eifi1/ui-kit/blob/main/docs/notifications-harmonization.md)
§4.1, [`docs/top-bar-harmonization.md`](https://github.com/Eifi1/ui-kit/blob/main/docs/top-bar-harmonization.md)
§2.7 with §6.1–§6.3, [`docs/admin-harmonization.md`](https://github.com/Eifi1/ui-kit/blob/main/docs/admin-harmonization.md)
§9, [`docs/tours-harmonization.md`](https://github.com/Eifi1/ui-kit/blob/main/docs/tours-harmonization.md)
§10 and [`docs/local-first-harmonization.md`](https://github.com/Eifi1/ui-kit/blob/main/docs/local-first-harmonization.md)
§1.5–§1.6 in the ui-kit. The API reference is the ui-kit showcase's "Server kit" group, built
from the `server-kit-api.json` each release carries ([Developing](#developing)).

- Distribution `eifi1-server-kit`, import package `eifi1_server_kit`, Python ≥ 3.14.
- Dependencies: `pydantic>=2.10`, `starlette>=0.40` — both already in every app through
  FastAPI, with no ceilings to fight an app's own pins (keksdose holds FastAPI < 0.137).
  Five optional extras: `images` (`pillow>=11`) for `uploads.ensure_decodable_image` only,
  `mail` (`httpx>=0.28`) for `mail.ResendClient` only, `billing` (`httpx>=0.28`) for
  `billing.PaddleClient` only, `fastapi` (`fastapi>=0.115`, `python-multipart>=0.0.18`)
  for `feedback.router`, `translation_review.router` and `tours.router` only, and `sync`
  (`sqlalchemy>=2.0`) for `sync.mixin` only. No JWT library: the kit builds and checks claims, each app
  signs with its own.
- Typed (`py.typed`, mypy `--strict`), 100 % line and branch coverage.

## Compatibility with ui-kit

The frontend half, [`@eifi1/ui-kit`](https://github.com/Eifi1/ui-kit), is a separate
package with its own version and changelog. **server-kit 0.20 ↔ ui-kit 0.51**: that ui-kit
release is tested against this release's API export (the ui-kit showcase's Server kit pages
and its vocabulary test, `showcase/src/__tests__/server-kit-vocabularies.test.ts`, which
checks the vocabularies the ui-kit writes a second time in TypeScript against
`server-kit-api.json`).

## Layering

| Layer | What | Status |
|---|---|---|
| **1 — contracts as code** | pure functions, Pydantic models, small classes, one Starlette middleware. **No** database models, migrations, sessions, repositories or routes | **0.1.0** |
| **2 — services behind ports** | the feedback service written once against a repository / storage / clock port each app implements (`feedback.ports`, `feedback.service`), and the router factory `feedback_router(FeedbackDeps(…))` (`feedback.router`, the `fastapi` extra). The app's hooks: its own demo guard factory and admin guard as route dependencies, `foreign_row` (403 or 404), the actor on the ports (`get(id, *, viewer)`, `apply(row, changes, *, actor)`), its schema subclasses and `to_response`. The kit never commits. The translation review the same way: the reviewer view carrying the app's grant (`TranslationReviewer`), the store port (`TranslationReviewStore[RowT]`: `verdicts`, `put`, `clear`, `by_reviewer`), `TranslationReviewService` and `translation_review_router(TranslationReviewDeps(…))` (`translation_review.router`); no route guards: the grant decides, and a demo's is empty. Public feedback grows the feedback service: four more ports (`FeedbackReplyStore`, `FeedbackCommentStore`, `FeedbackVoteStore`, `PeopleLookup`) beside the repository, which implements `PublicFeedbackRepository` too; the app's predicate `public_viewer` on the viewer; one write per store, the report last | **0.9.0**; translation review **0.10.0**; public feedback **0.11.0** |

Layer 1 never imports an app's `User`: the rules take plain values (`is_admin`,
`is_author`, the row's stored status and body, `is_first_user`, a token's `iat` and the
account's `sessions_invalid_before`), refusals are plain exceptions carrying a
`status_code`, and the queries — RLS, kastlan's `company_id`, the invitation rows — stay in
the app. Layer 2 doesn't either: the app builds a `FeedbackActor` from its `User`, its
adapters keep the queries, and a layer-2 module may import FastAPI and Starlette, never
SQLAlchemy or an app's model ([`docs/server-services-harmonization.md`](https://github.com/Eifi1/ui-kit/blob/main/docs/server-services-harmonization.md)).

## What is in it

### `eifi1_server_kit.feedback`

| Area | Names |
|---|---|
| Enums and sets | `FeedbackStatus` (9; `READY` between `OPEN` and `IN_PROGRESS`, `REWORK` after `READY`), `FeedbackCategory` (5, CRASH first), `FeedbackEntryKind` (`reply`, `rework`), `PICKABLE_CATEGORIES`, `AUTHOR_EDITABLE_STATUSES`, `TERMINAL_STATUSES`, `REWORKABLE_STATUSES` (the five answered statuses and `REWORK`), `AWAITING_STATUSES`, `QUEUE_STATUSES` (`READY`, `REWORK`, `IN_PROGRESS`: the loops' queue) |
| Schemas | `FeedbackCreate`, `FeedbackUpdate`, `FeedbackResponse`, `FeedbackAttachmentResponse`, `CrashReportCreate`, `CrashReportResponse` — subclass them; the knobs are class variables (`attachment_url_policy`, `max_context_size`) |
| Limits | `MAX_ATTACHMENT_URLS` (5), `MAX_ATTACHMENT_BYTES` (10 MB), `ACCEPTED_MEDIA_TYPES`, `MAX_TITLE_LENGTH` (255), `MAX_BODY_LENGTH` (50 000), `MAX_CONTEXT_SIZE` (8 KB), `NOT_NULLABLE_FIELDS` |
| Statuses the server decides | `initial_status(author_is_admin=, crash=False)` (READY for an admin's own report, OPEN otherwise and for every crash); `rework_status(actor_is_admin=)` (a rework goes back to REWORK from an admin, to OPEN from anyone else; READY before 0.13) |
| PATCH rules | `plan_update(status=, body=, changes=, is_admin=, is_author=)` → `UpdatePlan(changes, reworking)`; an append to an answered report raises `LegacyReworkAppendError` (since 0.16); `reject_manual_crash`, `check_author_edit`, `reopens`, `resolved_at_change` |
| Rework entries (0.13) | `plan_rework(*, status, is_author, actor_is_admin, now=)` → `ReplyPlan` (403 `REWORK_AUTHOR_ONLY` for anyone but the author, 403 outside `REWORKABLE_STATUSES`); `FeedbackReworkCreate(body, attachment_url)`; `split_rework_blocks(body, *, prefix=)` → `(description, [ReworkBlock(stamp, note, url)])` for the migrations (`REWORK_BLOCK_RE`; a rule that is no block, `--- REWORK ANSWERED … ---` or date-only, raises `ValueError`; split `body` only, never `outcome`) |
| Rework bodies (legacy) | `append_rework`, `rework_count` (anchored on `^---\s*REWORK\b`), `is_rework_append`, `split_body_attachments`, `body_attachment_urls`, `rework_stamp` — the old append's 422, a migration's downgrade, a client's fallback count |
| Crash filing | `crash_fingerprint(payload, user_id)` (byte-identical to keksdose's), `decide_crash(candidate_status)` → `CrashDecision.FOLD` / `FILE` (the candidate is the newest row with the fingerprint whose status is NOT in `TERMINAL_STATUSES` — filter that in the query, as keksdose does), `fold_crash_context`, `crash_title`, `crash_body`, `crash_context`, `crash_seen_at`, `crash_reference` |
| Erasure | `FEEDBACK_CONTEXT_KEEP`, `ERASED_MARK`, `scrub_text`, `scrub_context`, `anonymise_feedback`, `anonymise_reply` (an entry kept, un-named and scrubbed, its rework file answered for the purge), `feedback_attachment_urls`, `attachment_keys_to_purge` |
| Attachment URLs | `AttachmentUrlPolicy(key_pattern, prefix)`, `OPAQUE_KEY_PATTERN` (default), `SHA12_KEY_PATTERN` (keksdose), `UUID32_SHA12_KEY_PATTERN` (kastlan) |
| Context | `stamp_identity` (the server writes `user_*` from the session), `context_size`, `CONTEXT_KEYS` |
| Errors | `FeedbackError` → `FeedbackValidationError` (422: `CrashCategoryNotAssignableError`, `UnknownAttachmentUrlError`, `LegacyReworkAppendError` with `code` `LEGACY_REWORK_APPEND`, since 0.16), `FeedbackForbiddenError` (403) — all `ValueError`s with a `status_code` |
| Public feedback (0.11) | `public_body(body, *, policy)` (no file line, no attachment URL anywhere, kastlan's folded `--- COMMENT <stamp> · <name> ---` without the name), `public_text(text, *, policy)` (the URL removal alone, `REMOVED_URL` "[…]"), `is_public(category, public)` (never a CRASH), `feedback_recipients(*, author_id, commenter_ids, writer_id)`; `FEEDBACK_ANSWERED`, `FEEDBACK_COMMENT`, `COMMENT_MAX` (4000), `REPLY_MAX` (8000), `PUBLIC_PAGE_SIZE` (50), `PUBLIC_PAGE_MAX` (100); `PUBLIC_REPORT_FIELDS`, `PUBLIC_COMMENT_FIELDS`, `PUBLIC_REPLY_FIELDS` (what leaves for others, §9.10; `kind` since 0.13, never `attachment_url`); `DECIDED_STATUSES`; `plan_reply(*, status, new_status, now=)` → `ReplyPlan(changes, status_from, status_to)`; `plan_update(…, filed_private=False)` (`public` is the author's while the report is open, at any status when `filed_private`) |
| Public schemas (0.11) | `FeedbackResponse` gains `author_name`, `mine`, `public`, `filed_private`, `votes`, `voted`, `comment_count`, `replies` (all optional); `FeedbackUpdate` gains `public`, and **`outcome` is frozen**: a PATCH naming it is 422 `OUTCOME_READ_ONLY` ("outcome is read-only; post a reply"); `FeedbackReplyCreate`, `FeedbackReplyResponse` (`FeedbackPublicReplyResponse` + `status_from`, + `attachment_url` since 0.13; `kind` on both), `FeedbackPublicListItem`, `FeedbackPublicResponse` (separate models, so nothing added to `FeedbackResponse` reaches the public), `FeedbackCommentCreate`, `FeedbackCommentResponse`, `FeedbackCommentHide`, `FeedbackVoteResponse`, `PublicSort` |

**READY, the triage step** (feedback contract §8.2): OPEN means "filed, not yet triaged",
and an admin releases a row for implementation by setting READY. The server decides the
first status at submit — `initial_status(author_is_admin=…)` — and the client never sends
one with a new row. The author may edit while OPEN, READY or IN_PROGRESS. An app whose
status column is a database enum adds the label first
(`ALTER TYPE feedbackstatus ADD VALUE 'READY' AFTER 'OPEN'`); existing OPEN rows stay OPEN.

**Rework as an entry** (0.13, feedback contract §10): a rework is the author's own dated
entry in `feedback_replies` (`kind = 'rework'`, its one file in `attachment_url`), no
longer a block appended to the body. `POST /feedback/{id}/rework` takes it from the
report's author only (an admin answers with a reply, whose status may be `REWORK`); an
admin author's lands in `REWORK` ("Rework requested", in the loops' queue), anyone else's
in OPEN for the triager. **A rework entry notifies nobody**: the reply store's `add` fires
`feedback_answered` for `kind = 'reply'` only, `feedback_recipients` is never asked for
one, and no admin mail is sent; the REWORK status (or the chip on an OPEN row) is the
inbox's signal. `outcome` answers as the newest entry of kind `reply`. An old client's body
append to an answered report (its rework before ui-kit 0.40), by the author or an admin, is
a coded 422 since 0.16: `{"detail": …, "code": "legacy_rework_append"}`, nothing written, so
a tab open across the deploy shows the detail until it reloads (0.13–0.15 converted it into
an entry). Inside OPEN / READY / IN_PROGRESS an append stays a body edit. Each
app migrates its `--- REWORK <stamp> ---` blocks with `split_rework_blocks` and adds the
enum label (`ADD VALUE IF NOT EXISTS 'REWORK' AFTER 'READY'`) and the columns
`kind VARCHAR(12) NOT NULL DEFAULT 'reply'`, `attachment_url VARCHAR(500) NULL`.

### `eifi1_server_kit.feedback` — layer 2

The flows the three apps each wrote around the rules — the lookups, the demo refusal, the
limiters, reading an upload, the download's headers, the 404 or 403, the erasure's loop —
written once (server-services §2, §4.1). The ports and the service come with
`eifi1_server_kit.feedback` and need no FastAPI; the routes are
`eifi1_server_kit.feedback.router`, the fakes and checks `eifi1_server_kit.feedback.testing`.

| Area | Names |
|---|---|
| Ports | `FeedbackActor(id, email, display_name, is_admin, is_demo=False)`; `FeedbackRow` (read-only: an app's mapped row satisfies it as it is); `NewFeedback`; `FeedbackRepository[RowT]` (`get(id, *, viewer)`, `list_mine`, `inbox(status)`, `open_crash`, `add`, `apply(row, changes, *, actor)`, `by_author`, `urls_named_except`); `AttachmentStorage` (`save(data, upload, *, uploader, filename)` → key, `read(key, *, viewer)`) and `StoredAttachment`; `Clock`, `utc_now` |
| Service | `FeedbackService(repository, *, policy, foreign_row="forbidden", now=utc_now)`: `create`, `report_crash(…, limiter=)`, `list_mine`, `inbox`, `update`, `erase_author(user_id, identifiers)` → `ErasedFeedback(reports, keys)`; `throttle_upload(limiter, user_id)`, `upload_attachment(…)` → the URL, `download_attachment(…)` → `AttachmentDownload(data, media_type, headers)` |
| Routes (`feedback.router`) | `FeedbackDeps(actor=, repository=, storage=, demo_guard=, admin_guard=, policy=, create_model=, update_model=, response_model=, to_response=, foreign_row=, decode_images=, require_utf8_text=, limiters=, now=)`, `feedback_router(deps, *, prefix="/feedback", tags=("feedback",))`, `app_state_limiters` |
| Refusals | `FeedbackNotFoundError` (404 "Feedback not found"), `AttachmentNotFoundError` (404 "Attachment not found"), `FeedbackThrottledError` (429, `Retry-After`, `THROTTLED_DETAIL`, `COMMENTS_THROTTLED_DETAIL`, `VOTES_THROTTLED_DETAIL`), `FeedbackConflictError` (409) — answered by `install_contract_error_handlers` |
| Testing (`feedback.testing`) | `InMemoryFeedbackRepository` (records each write in `writes`; `replies=` for the rework files in `urls_named_except`), `InMemoryAttachmentStorage`, `MemoryFeedbackRow`, `actor(**overrides)`, `fixed_clock(at)`; `check_repository`, `check_storage`, `check_round_trip`; `InMemoryReplyStore.notified` (reply-kind adds only) and `check_reply_store` covering both kinds and an explicit `created_at` (0.13); (0.15) `InMemoryFeedbackRepository.count_by_status` (`demo_authors=` left out), checked by `check_repository`; (0.18) `check_repository(…, replies=, comments=)` checks `urls_named_except` names every entry's and comment's file, on the excepted reports too |
| Attention (0.15) | `FeedbackRepository.count_by_status()` → `{FeedbackStatus: int}` (one `GROUP BY status` in the request's scope, as `inbox`: crash reports in, demo authors out, an erased author's report in; **every app repository adds it**); `feedback_attention(counts)` → `FeedbackAttention(to_triage, awaiting_verdict, released_for_work)` — `to_triage` is `AdminAttention`'s `feedback_waiting`, the other two `extra` keys (`awaiting_verdict`, `released_for_work`) |
| Public ports (0.11) | `FeedbackActor.role` (for the app's predicate); `NewFeedback.public` (True; False for a crash); `PublicFeedbackRow`; `FeedbackReplyRow`, `FeedbackCommentRow`; `NewFeedbackReply`, `NewFeedbackComment`; `PublicListEntry(row, votes, voted, comment_count)`; `PublicFeedbackRepository[RowT]` (`public_page(*, viewer_id, sort, status, category, q, limit, offset)` with the counts in SQL, `get_public(id)`); `FeedbackReplyStore` (`for_reports`, `add(new, *, actor)` fires `feedback_answered` for a reply only, `by_author`, `apply`); since 0.13 `FeedbackReplyRow` reads `kind` and `attachment_url`, `NewFeedbackReply` carries `kind`, `attachment_url` and an optional `created_at` (the migration's stamp), and `urls_named_except` names other reports' rework files too; `FeedbackCommentStore` (`for_report`, `counts(ids, *, include_hidden)`, `commenters`, `add(new, *, actor)` fires `feedback_comment`, `get`, `apply`, `delete`, `by_author`, `clear_hidden_by`), and since 0.18 the optional `FeedbackCommentFilePurge` (`purge_files(keys)`, a runtime-checkable protocol a comment store may also implement); `FeedbackVoteStore` (`counts(ids, *, viewer_id)`, `put`, `delete`, `delete_by_author`); `PeopleLookup` (`names(ids)` → `(first, last)`, the one read of `users`). `AttachmentStorage.read` states the files predicate: the uploader, the author of a report naming the file, an admin |
| Public service (0.11) | `FeedbackService(…, replies=None, comments=None, votes=None, people=None, public_viewer=None, public_search=True)`: `reply`, `list_public`, `get_public`, `comments`, `comment(…, limiter=)`, `hide_comment`, `delete_comment`, `vote(…, limiter=)`, `unvote(…, limiter=)`, `sees_public(actor)`; the page views `admin_view(actor, pairs)`, `author_view(actor, rows)`, `public_item(actor, entries)`, `public_view(actor, row)` (one call per store a page); `create` casts the author's vote; `erase_author` → `ErasedFeedback(…, comments, votes, replies)`; 0.13: `rework(actor, id, payload)`, `update` converts the legacy append, the views carry `kind` (and `attachment_url` for the admin and the author), `erase_author` un-names rework entries and purges their files; 0.18: `delete_comment` answers the bare keys of the deleted comment's file nothing else names, and hands them to a comment store that is a `FeedbackCommentFilePurge` (`purge_files(keys)`, before the commit) — keep or purge, the app decides |
| Public testing (0.11) | `InMemoryReplyStore`, `InMemoryCommentStore`, `InMemoryVoteStore`, `InMemoryPeopleLookup` (each logs `calls`; the reply and comment stores record `added` with the actor), `MemoryReplyRow`, `MemoryCommentRow`; `check_reply_store`, `check_comment_store` (`attachment_url=` one of `policy=`'s URLs, required since 0.19), `check_vote_store`, `check_public_repository`; `assert_public_view(payload, *, policy=)` |
| Admin alerts (0.16, feedback §11) | `FEEDBACK_RECEIVED` (the type every app declares, on by default, `quiet`); `FeedbackAlert(kind, feedback_id, category, actor_id, company_id=None)`, `FeedbackAlertKind` (`report`, `rework`, `crash`); the port `FeedbackAlerts` (`async filed(alert, row)`); `FeedbackService(…, alerts=None)` tells it after `create`, after a NEW crash row (never a fold) and after a rework's `apply` when it lands OPEN — `None` (a loop's service) tells nobody; `FeedbackDeps(alerts=)`; `admin_alert_recipients(admin_ids, *, actor_id)` (the actor never told), `mails_at_once(alert)` (False for a crash: mailed only in the queue's bundle); testing `RecordingAlerts` |

| Route | Endpoint | Guard (the app's) | Answer |
|---|---|---|---|
| `POST /feedback/attachments` | `upload_attachment` | `demo_guard("uploads")` | 201 `{url}` |
| `GET /feedback/attachments/{key}` | `download_attachment` | — | 200, the bytes |
| `POST /feedback` | `create` | `demo_guard("feedback")` | 201 |
| `POST /feedback/crash` | `report_crash` | — | 202, always |
| `GET /feedback/my` | `list_my` | — | the caller's own |
| `GET /feedback` | `list_all` (`status_filter`) | `admin_guard` | each with `user_email` |
| `GET /feedback/public` | `list_public` (`sort`, `status_filter`, `category`, `q`, `limit`, `offset`) | — (`public_viewer`, else 404) | titles only, the counts |
| `GET /feedback/public/{feedback_id}` | `get_public` | — (the same; the report public) | the public body and the replies |
| `GET /feedback/{feedback_id}/comments` | `list_comments` | — (who sees the report) | oldest first |
| `POST /feedback/{feedback_id}/comments` | `create_comment` | `demo_guard("feedback")` | 201 |
| `PATCH /feedback/{feedback_id}/comments/{comment_id}` | `hide_comment` | `admin_guard`, `demo_guard("feedback")` | the comment |
| `DELETE /feedback/{feedback_id}/comments/{comment_id}` | `delete_comment` | `demo_guard("feedback")` (the writer, else 404) | 204 |
| `PUT /feedback/{feedback_id}/vote` | `vote` | `demo_guard("feedback")` | `{votes, voted}` |
| `DELETE /feedback/{feedback_id}/vote` | `unvote` | `demo_guard("feedback")` (the author: 409) | `{votes, voted}` |
| `POST /feedback/{feedback_id}/replies` | `create_reply` | `admin_guard`, `demo_guard("feedback")` | 201, the row with its replies |
| `POST /feedback/{feedback_id}/rework` | `create_rework` (0.13; with `replies`) | `demo_guard("feedback")` only (the author, else 403) | 201, the row with its replies |
| `PATCH /feedback/{feedback_id}` | `update` | `demo_guard("feedback")` | the row |

- **The guards are the app's own**, first in each route's `dependencies=[…]`: FastAPI runs
  them before the body is validated (a demo's `json={}` is 403 `demo_refused`), the 403's
  words stay the app's, and each app's route walk finds them by its own names. The service
  keeps the rule too, for a caller without the router (`refuse_demo`, "Insufficient role").
- **keksdose's endpoint names**, the upload's `file` field and `status_filter`, so its
  operation ids and its `Body_upload_attachment_api_v1_feedback_attachments_post` stay.
  `policy.prefix` must be the mounted path; `create_model.attachment_url_policy` must be
  `policy` (else `ValueError`). An app's own extra routes go after the kit's router.
- **The kit never commits.** Each flow makes at most one repository write (`add` or
  `apply`), as its last step; the app's session or adapter commits.
- **The limiter is charged before the work** (feedback §3.5, §3.6 as amended): an upload
  before it is read, a crash before the lookup. The windows are read per request from
  `app.state.feedback_limiters`.
- **The download** reads the type off the bytes (the signature, else text when stored as
  text and still text, else `application/octet-stream`), always sends
  `Content-Disposition` (`inline` for the four pictures only, the stored name or
  `attachment.<ext>`) and `X-Content-Type-Options: nosniff`.
- **The erasure deletes no file**: `erase_author` anonymises each row with `actor=None`
  and answers the bare keys no other row names, sorted; the app deletes them at its commit.
- **The create stamps the identity** (`stamp_identity`) and decides the first status;
  `attachment_urls` is `None` for none, never `[]`.
- **Public feedback (0.11, feedback §9).** `create_reply` is mounted with `deps.replies`
  (all three apps); the other eight with `deps.public_viewer` (keksdose: everyone signed
  in; Kurvenschmiede: never a CUSTOMER), which needs `comments`, `votes` and `people`
  (else `ValueError` at build time). An admin always sees the public parts; a demo never
  does (404 on the reads, the app's demo refusal on the writes); a CRASH is never public.
  Others read first names only, and every body that leaves for them passes `public_text`.
  The existing answers fill the new fields from the stores given, one call per store a
  page; an app that gives none answers them `null`. A reply with a status adds the reply,
  then applies the row (one write per store, the report last); a status `PATCH` alone
  writes no reply and sends nothing. `outcome` is frozen for this release.

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
`signature_type`, `inline_or_attachment`, `content_addressed_key`,
`media_type_for_extension`, and `content_disposition(filename, disposition="attachment", *,
fallback=None)`: the original name in the RFC 5987 `filename*`, and a plain `filename=`
that every system will write — accents folded (`Débattement-à-gauche.csv` →
`Debattement-a-gauche.csv`), `_` for what stays outside safe ASCII and for Windows'
`<>:/|?*` (never `?`), quotes and backslashes dropped, 100 characters at most — or the
app's own ASCII name, `fallback="curve-12.csv"`, narrowed the same way.

`ensure_decodable_image(data, media_type)` — the **`images` extra** — is keksdose's
dev#510 guard: after `check_upload`, with the detected type, Pillow opens any `image/*` and
`verify()`s it (no `load()`), refusing what it cannot parse as `NotAnImageError` (400) — a
PNG with only its signature, a truncated PNG or WebP, a decompression bomb past Pillow's own
`MAX_IMAGE_PIXELS`. A JPEG or GIF cut off mid-raster still passes (`verify()` does not
decode). Other types and `UNDECODABLE_IMAGE_TYPES` (HEIC/HEIF) pass unparsed. Pillow is
imported lazily; without the extra every call raises `ImportError` naming it.

### `eifi1_server_kit.limiter`

`SlidingWindowRateLimiter(max_hits=, window_seconds=, clock=)` with `hit(key) -> float |
None` and `reset()`; `FeedbackLimiters` (20 uploads and 20 crashes per user per hour;
since 0.11 also `.comment`, 20, and `.vote`, 60 — `comments_per_hour=`, `votes_per_hour=`);
`retry_after_header`. **Process-local**: each worker and each Cloud Run instance counts on
its own. Build limiters per app (`app.state`), never as module globals. When to charge —
before or after validation, before or after the crash dedupe — is the caller's choice;
the module docstring records keksdose's order and the contract's recommendation. `count(key)` reads a key's live window without charging it,
`forget(key)` drops one key's window. The sign-in budgets are
[`auth.AuthLimiters`](#eifi1_server_kitauth).

**Key every per-IP window by `client_ip(headers, peer, *, trusted_hops)`** (landing-demo
§5.1). Every proxy appends the address it was connected from, so `X-Forwarded-For` is
counted from the RIGHT, as many entries as the app has proxies; the left-most entry is
whatever the client sent, and a random one per request would buy a fresh window per
request (keksdose's and Kurvenschmiede's own helpers read it). `trusted_hops=0` ignores
the header and answers the peer; a header shorter than the hops counted answers the peer
too, never the left-most entry. Measure `trusted_hops` once per environment: send
`X-Forwarded-For: 203.0.113.7` and log what arrives.

```python
ip = client_ip(
    request.headers, request.client.host if request.client else None, trusted_hops=settings.trusted_proxy_hops
)
```

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
"invalid_credentials"}` at 401, …), user administration's `AccountError` (409) and
`RosterQueryError` (422), `settings.PatchNullError` (422, `not_nullable`),
`demo.DemoError` (403 / 404 / 429 / 503), the dev switch's `dev_switch.DevSwitchError` (404 / 409 / 422), and billing's `BillingError` (400 / 402 / 404 /
409 / 422 / 502 / 503) and `PlanLimitError` (402), and `jobs.JobsTokenError` (401). An `AccountError`'s or a `PlanLimitError`'s `extra`
fields go beside `detail` and `code`, as in kastlan's `{"detail": …, "code": "last_admin",
"companies": [...]}` or `{"detail": …, "code": "plan_limit", "dimension": "budgets", "plan":
"free", "limit": 1, "used": 1}`; a refusal's `headers` go on the answer (`DemoError`'s
`Retry-After`). See
[the refusals](#the-refusals).

On a FastAPI app it also registers `validation_error_response` for `RequestValidationError`
(0.17.0): FastAPI's 422 shape, `{"detail": [{"type", "loc", "msg"}, ...]}`, without each
error's `input`, `ctx` or `url` — so a too-long password in `LoginRequest`,
`EmailChangeRequest` or `DeletionRequest`, an `EmailChangeConfirm` token, any body's values
no longer come back in the 422 or land in request and response logs. Every route, no app
code. An app's own `RequestValidationError` handler wins: registered before the call the
kit leaves it in place, registered after it replaces the kit's;
`install_contract_error_handlers(app, redact_validation_errors=False)` keeps FastAPI's
echoing answer. The push routes' `parse_push_body` / `parse_push_replace` (0.17.0) stay
valid — their coded `push_body_invalid` 422 is unchanged — and are no longer needed just to
keep `old_auth` out of a 422. A body that isn't valid JSON never reaches them: FastAPI refuses
it first in the redacted shape above (`json_invalid`), so one push route answers two 422
shapes, and neither echoes the body. `documented_body(model)` (0.18) as the route's
`openapi_extra` puts the untyped body's schema back in OpenAPI.

### `eifi1_server_kit.translation_review`

The wire shapes (`TranslationReviewsResponse`, `TranslationReviewBatch`,
`TranslationReviewClear`, `TranslationReviewOut`, `TranslationVerdict`, the token
responses), the `kit.` scope (`is_kit_key`, `require_kit_keys`), grants over the app's own
locales and areas (`review_grant(…, is_demo=False)` — a demo is granted nothing,
`can_review_kit`, `assert_allowed`, `normalize_locales`, `normalize_areas`,
`ReviewerProfile`), and the review token format (`new_review_token(prefix)`
= prefix + 43 URL-safe characters, `hash_review_token` = SHA-256, `tokens_to_retire`,
`review_token_is_live`, `review_token_expiry`). The four request bodies refuse unknown
fields (0.10): a misspelt `suggestion` is a 422. `TranslationReviewOut` carries
`reviewer_first_name` and `reviewer_last_name` beside `reviewer_name` (0.10).

**A key lies in an area** when it is the area, continues it with `.` or `:` (kastlan's
`ns:key`, 0.10), or is the kit's wording for it under `kit.<area>.` — `in_areas`, and
ui-kit's `keyInArea` on the page. **An app that filters a listing by area in SQL** builds
the filter from `area_like_patterns(grant.areas)` — the four arms per area (`["legal"]` →
`["legal", "legal.%", "legal:%", "kit.legal.%"]`, `None` = no filter) — and never by hand:

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

### `eifi1_server_kit.translation_review` — layer 2

H1's three routes, the area filter, the reviewer's name and the export, written once
(server-services §6, 0.10.0). The ports and the service come with
`eifi1_server_kit.translation_review` and need no FastAPI; the routes are
`eifi1_server_kit.translation_review.router`, the fake and the checks
`eifi1_server_kit.translation_review.testing`.

| Area | Names |
|---|---|
| Ports | `TranslationReviewer(id, first_name, last_name, locale, grant, kit_only=False)` (the app's `review_grant(…)`; keksdose's review token is the same view with `kit_only=True`); `TranslationReviewRow` (read-only: an app's mapped row satisfies it); `ReviewerName`; `NewVerdict`; `TranslationReviewStore[RowT]` (`verdicts(locales, areas)` with the reviewer's name, `put(verdicts)` an upsert per `(locale, key)`, `clear(pairs)` → how many existed, `by_reviewer(id)` oldest first); `Clock`, `utc_now` from `eifi1_server_kit.clock` |
| Service | `TranslationReviewService(store, *, locale_codes, now=utc_now)`: `scope(viewer)` → the grant (403 `NO_REVIEW_ACCESS` without a locale; a token's areas `("kit",)`, 403 `NO_KIT_REVIEW_ACCESS` on a grant with an area limit), `list_reviews`, `save`, `clear`, `export(reviewer_id)`; `VERDICTS_EXPORT_KEY` (`"translation_verdicts"`) |
| Routes (`translation_review.router`) | `TranslationReviewDeps(reviewer=, store=, locale_codes=, now=)`, `translation_review_router(deps, *, prefix="/translations", tags=("translations",))` |
| Testing (`translation_review.testing`) | `InMemoryTranslationReviewStore` (records each write in `writes`), `MemoryVerdict`, `reviewer(**overrides)`; `check_store`, `check_round_trip` |

| Route | Endpoint | Answer |
|---|---|---|
| `GET /translations/reviews` | `list_translation_reviews` | the caller's locales, areas and verdicts; 403 with no locale |
| `PUT /translations/reviews` | `save_translation_reviews` | the rows written, with the caller's name |
| `POST /translations/reviews/clear` | `clear_translation_reviews` | `{cleared}` |

- **No route guard**: every route is "any signed-in", and the viewer's grant decides. A
  demo is granted nothing (`review_grant(…, is_demo=True)`).
- **The refusals are layer 1's**, answered by `install_contract_error_handlers`; in order:
  the body (422, an unknown field too), a token naming a key outside the kit (403),
  `assert_allowed` (422 for a locale the app does not ship, 403 outside the grant).
- **The store filters by area with `area_like_patterns`**, and the service filters its
  answer again with `in_areas`. **`reviewer_name` is in the reader's order**
  (`full_name(first, last, viewer.locale)`), beside the two parts.
- **The kit never commits.** Each request makes at most one store write (`put` or
  `clear`), as its last step. An app's own routes (keksdose's review tokens, kastlan's
  `GET /translations/documents`, which calls `scope(viewer)` for the same 403) go after
  the kit's router.
- **The export is keksdose's shape** for every app, under `data["translation_verdicts"]`:
  `locale`, `string`, `text`, `verdict`, `suggestion`, `note`, `reviewed_at`, oldest first.

### `eifi1_server_kit.auth`

Sign-in, sign-up and the account (auth contract §3–§6, §8). keksdose is the reference.

| Area | Names |
|---|---|
| Addresses | `normalise_email` (trim + lower-case, never strips a `+tag`); `tagged_variant(email, tag)` (keksdose's `taggedEmail`: `None` when there is already a `+` or nothing to split); `invitation_accepts(invited, registered, tag)` (the exact address or the app's own tag on it); `verified_by_invitation(invited, registered)` (the exact address only — a tagged sign-up verifies by its own mail); `addresses_for_reset(email, tag)` (the submitted address, then its tagged variant) |
| The gate | `registration_decision(is_first_user=, on_env_list=, has_valid_invitation=)` → `RegistrationDecision.FIRST_ADMIN` / `INVITED` / `CLOSED` (`.allowed`, `.first_admin`); `parse_env_list`, `env_list_match` (`None` = no list set) |
| Names | `full_name(first, last, locale)` ("First Last"; hu "Last First"; zh "LastFirst" for a CJK name, a Latin one as written; a missing part leaves the other), `first_name_of(first, last)` (0.11: the first name others see, `None` while the last name is blank — no first-word guessing), `name_incomplete(first, last, is_demo=)`, `erasure_identifiers(email, first, last, old_display_name)` |
| One-time tokens | `mint(prefix="")` → `MintedToken(raw, digest)`, `hash_token` (SHA-256 hex), `is_expired(issued_at, ttl, now)`; `RESET_TTL` 1 h, `VERIFY_TTL` 48 h, `INVITE_TTL` 14 d, `EMAIL_CHANGE_TTL` 48 h; `OneTimeTokenKind` (`password_reset`, `verification`, `invitation`, `email_change` — the mail kinds too) with `ONE_TIME_TOKEN_TTLS`, `EMAIL_CHANGE_TOKEN_KIND` |
| Session claims | `access_claims(sub, now=, lifetime=, extra=)`, `refresh_claims(…)`, `challenge_claims(sub, ChallengeKind, now=)` (`{sub, type, iat, exp}`, `iat` fractional; `extra` may not override those nor carry a name, email or locale); `token_is_revoked(iat, sessions_invalid_before)`; `ACCESS_TOKEN_LIFETIME` 24 h, `REFRESH_TOKEN_LIFETIME` 30 d (also `*_EXPIRE_MINUTES`) |
| Limits | `AuthLimiters(clock=)`: `login` (30 / 5 min per IP), `challenge` (20 / 5 min per IP), `challenge_subject` (10 / 15 min per user), `register` (5 / 5 min per IP), `reset_ip` (10 / h), `reset_address` (3 / h), `verification_resend` (10 / h per recipient), `login_failures` (`LoginFailureThrottle`); each a `Budget` an app may replace |
| Schemas | `RegisterRequest`, `LoginRequest`, `RefreshRequest` (`{refresh_token}`, any text: an empty one is the route's 401), `TokenResponse[UserT]` (`expires_at`, set for a demo), `TwoFactorChallenge`, `PasswordChangeChallenge`, `UserResponse` (computed `display_name`, `name_incomplete`, the `name_completion_exempt()` hook; `demo_expires_at`; `text_size` and `contrast`, `None` = never chosen), `ProfileUpdate` (the `offered_locales` knob, settings §6.2; `text_size` and `contrast`, never `null`); (0.16, notifications §10.3, automatic only) the zone on the account: `UserResponse.device_timezone` and `ProfileUpdate.device_timezone` (the device's report, never `null`; one this server doesn't know is dropped — left unset, never written — as at sign-up, 0.17.0; no pick: a `timezone` is a 422 from 0.18, as any unknown field — ignored in 0.16 and 0.17; breaking for a stale ≤0.45 bundle only), a `ZoneName` (an IANA zone `zoneinfo` knows, `MAX_ZONE_LENGTH` 64), `RegisterRequest.device_timezone` (an unknown one dropped, never a 422), `apply_zone_update(user, patch, *, is_demo)` (nothing for a demo; pass `skip=PROFILE_ZONE_FIELDS` to `apply_patch`); `TEXT_SIZES` (`normal`, `large`, `xlarge`) and `CONTRAST_MODES` (`system`, `standard`, `more`), with their types `TextSize` and `ContrastMode` (text-size §6, §10.6); the types `Email`, `NewPassword` (8 characters, 72 bytes), `ExistingPassword`, `PersonName` (1–120 trimmed), `LocaleTag`, `UtcDateTime` (always ISO-8601 in UTC; a naive value is read as UTC) |
| Sign-in timing | `absent_account_check(hash_password, verify_password, *, cost=None)` → the check for an address with no account: one verify against a dummy hash of a random secret (`ABSENT_ACCOUNT_SECRET_BYTES` 32), built lazily and again when `cost()` changes, through the app's own `verify_password`; answers `False`, always (§5.2) |
| Refusals | `AuthErrorCode` (`invalid_credentials` 401, `registration_closed` 403, `email_taken` 409, `invitation_invalid` / `invitation_expired` / `token_invalid` / `token_expired` 400, `session_ended` 401 — every definitive refusal of `/auth/refresh`, auth §11), `AuthError(code, detail=None)` |

**The kit signs no JWT.** Each app keeps its library and secret and hands the claim dicts
to it: `jwt.encode(kit.access_claims(user.id, now=now, extra={"role": user.role}), …)`.
On every request and on refresh: `if kit.token_is_revoked(claims.get("iat"),
user.sessions_invalid_before): …401`. The comparison is exact, so a token minted after a
cut-off in the same second survives. On refresh, every refusal is
`AuthError(AuthErrorCode.SESSION_ENDED)`, and the route's own failure stays a 5xx; a
signed-in endpoint answers 401 only about the session, never for a wrong input (ui-kit's
`attachSessionRefresh` signs out on a second 401; the rule is `auth.tokens`' docstring).

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
ip = client_ip(request.headers, request.client.host if request.client else None, trusted_hops=HOPS)
if (wait := limiters.login.hit(ip)) is not None:
    raise HTTPException(429, headers={"Retry-After": retry_after_header(wait)})
await asyncio.sleep(limiters.login_failures.delay(payload.email))
user = await authenticate(session, payload.email, payload.password)  # check_absent(password) for unknown addresses
if user is None:  # unknown, wrong password, deactivated — one answer
    limiters.login_failures.record_failure(payload.email)
    raise kit.AuthError(kit.AuthErrorCode.INVALID_CREDENTIALS)
limiters.login_failures.clear(payload.email)
```

**An address with no account costs what one with an account costs** (§5.2): the one 401
must not come back in microseconds for an unknown address and in a quarter of a second for
a known one (Kurvenschmiede measured 3.7 ms against 262.2 ms). Build the check once from
the app's own hasher — the kit hashes nothing — and call it where the real check would be:

```python
check_absent = kit.absent_account_check(hash_password, verify_password, cost=lambda: settings.bcrypt_rounds)


async def authenticate(session, email, password):
    user = await get_user_by_email(session, email)
    if user is None:
        check_absent(password)  # False, after the same work a real check does
        return None
    password_ok = verify_password(password, user.password_hash)  # checked even when deactivated
    ...
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
| Rules | `refuse_self(actor_id, target_id)`; `refuse_last_admin(target_is_admin=, active_admins=, change_removes_admin=)` (kastlan counts per company); `confirm_email_matches(target_email, typed)` (normalised, so case and spaces never fail it and a `+tag` is part of it); `require_confirmation(level, target_email=, acknowledged=, confirm_email=)` (`target_email=None` is a group: the tick alone, never `type_email`) |
| Actions and levels | `AdminAction` (the §4.3 list: `deactivate`, `reactivate`, `role`, `membership_remove`, `password_change_require` / `_withdraw`, `mail_verification` / `mail_reset`, `reviewer`, `invite`, `invite_resend`, `invite_revoke`, `transfer`, `deletion_request`, `deletion_cancel`, `erase`, billing §6's `plan` at `acknowledge`, and §11's group words `group_create`, `group_rename`, `group_delete` (at `acknowledge`), `membership_add`, `membership_role`, and (0.15) `group_deactivate`, `group_reactivate` (a whole company switched off and on); a team's `group_rename` detail is `{}`, a company's `{from, to}`); `ConfirmationLevel` (`none` / `acknowledge` / `type_email`), `CONFIRMATION_LEVELS`, `confirmation_level(action, at_least=)` |
| User list | `parse_roster_query(limit=, offset=, sort=, q=, role=, state=, sort_keys=, accepted_states=, roles=)` → `UserListQuery` (`.search_pattern` for `LIKE`), `parse_sort`, `parse_tokens`, `RosterSort`, `SORT_KEYS`, `STATE_TOKENS`, `DEFAULT_ROSTER_SORT` (newest first), `DEFAULT_PAGE_SIZE` 25, `MAX_PAGE_SIZE` 200, `RosterQueryError` (422), `LIKE_ESCAPE` |
| Audit | `admin_action_record(action, actor_id=, target_user_id=, target_email=, detail=, company_id=, target_group_id=, target_group_name=, now=)` → the row's fields (`target_group_name` on a `group_delete` row only) (an `AdminAction`, or the app's own action kept as written); `audit_detail(detail, action=)` (ids, roles, flags and counts only; 0.16: a `group_rename`'s top-level `from` / `to` are names, free text cleaned to at most `DETAIL_NAME_MAX_LENGTH` 200, never refused), `group_name_problem(name)` → `GroupNameProblem` (`blank` / `too_long` / `control_character` / `secret_shaped`) or `None`, the same rule for the app's request schema (0.16), `AuditDetailError`, `DETAIL_TOKEN_MAX_LENGTH` 64, `DETAIL_MAX_BYTES` 2048, `DETAIL_MAX_DEPTH` 3 |
| Schemas | `AdminUserRow` (never the person's `device_timezone`, 0.17.0), `UserListResponse[RowT]`, `ActionConfirmation` → `ActiveChange`, `RoleChange`, `RolesChange`, `MailRequest` (`MailKind`); `MailResult` (a link only on the console, `MAIL_BACKEND_CONSOLE`); `InvitationCreate`, `InvitationRow`, `InvitationStatus`, `invitation_status(…)`; `ReviewerUpdate`; `PersonRef`; `GroupRef`; `AdminActionRow` (`target_group_id`, `target_group`); `EmailChangeRequest`, `EmailChangeConfirm`, `DeletionRequest` |
| Deletion | `DeletionMode` (`after_days`, `operator`), `deletion_schedule(now, mode, days=30)`, `deletion_due(requested_at, days, now, scheduled_at=)`, `deletion_mail_retention_note(backup_days=7, log_days=30)` → `RetentionNote` |
| Export | `export_envelope(app, account, data, now)` (`"format": "eifi1-account-export"`, `"version": 1`), `export_filename`, `EXPORT_PER_USER` (once a minute), `NEVER_EXPORT`; `assert_no_secrets(obj, allow=)` / `secret_paths` → `ExportSecretError`; `looks_secret`, `looks_secret_key`, `looks_secret_value` |
| Attention | `AdminAttention` (`GET /admin/attention`: `feedback_waiting` = OPEN, crashes included, in the admin's scope; `deletion_requests`, `invitations_pending`, `users_total`, `users_new_7d`, `extra`; each `AttentionCount` = `int ≥ 0` or `None` for "no such thing here"; demo accounts never count), `AdminAttention.of(…, demo=)` (every count passed in), `AdminAttention.for_demo(*extra_keys)` (all `None`) |
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
One exception (0.16): a company's name is business data, so a `group_rename` detail's
top-level `from` and `to` are free text. They are cleaned, never refused, so the log
cannot turn a rename into a 500, even for a stored name from before the rule: control and
format characters go (a tab or line break becomes a space, a soft hyphen or zero-width
space vanishes), whitespace collapses, and a name over 200 characters is cut, ending in
`…`. A blank or secret-shaped name is left out with its key. The seam is
`audit_detail(detail, action=)`, and `admin_action_record` passes its own action. The rest
of that detail is still tokens, and every other action still refuses a name. A team's
rename keeps `{}`. A NEW name is the app's to refuse, with a 422 at the edge, by the same
rule:

```python
@field_validator("name")
@classmethod
def _name(cls, name: str) -> str:
    if problem := kit.group_name_problem(name):  # "blank", "too_long", ...
        raise ValueError(f"name: {problem}")
    return name.strip()
```

**The user list** takes the contract's `-key` and DataTable's `key.desc` alike. An app
narrows or widens the state vocabulary: Kurvenschmiede has no `invited` rows, and keksdose
keeps `allowlisted`. It also adds its own sort keys. An unknown token is a 422, never an
empty page.

**The export check is a test.** Build the export of a fixture account with every table
filled, then run `kit.assert_no_secrets(export)`. It flags keys named like a secret and
values shaped like one: hashes, JWTs, digests, links with a token. It walks into
containers, so `api_tokens: [{name, scopes}]` passes. `allow=` names a field it flags
wrongly. A feedback file's storage `key` (a keyed digest) is flagged, and rightly left
out rather than allowed: it is an address inside the app, not the user's data
(Kurvenschmiede's 0.30 adoption; keksdose has the same field).

**Plant a secret, too.** The shape check can't know every secret: a value under a harmless
key that has no secret's shape passes it. keksdose's export test also plants known secret
values (a token, a push endpoint, a key wrap) in the fixture and asserts their bytes appear
nowhere in the serialised export. Do the same.

**Deletion** is two-stage: deactivated at once, erased later. In `after_days` mode the
erasure is a Cloud Scheduler → Cloud Run Job, one account per transaction, which
re-checks `deletion_due(…, scheduled_at=row.deletion_scheduled_at)` on the locked row. In
`operator` mode an operator erases from the platform. The email change's link lives
`auth.EMAIL_CHANGE_TTL` (48 h) as a `OneTimeTokenKind.EMAIL_CHANGE` row holding the new
address.

### `eifi1_server_kit.settings`

The backend half of the settings round (settings contract §6). The endpoints, their models
and their columns stay in the app.

| Area | Names |
|---|---|
| The PATCH rule | `apply_patch(obj, update, *, not_nullable, defaults=None, skip=())` → the fields written (`obj=None` when every field is skipped); `PatchNullError` (422, `code: "not_nullable"`, `fields`), `PATCH_NULL_CODE` |
| The language | `canonical_locale(tag, offered)`, `parse_accept_language(header, offered)`, `profile_update_model(offered, name="ProfileUpdate")` |

**One rule for every settings body** (§6.1), whatever its verb — keksdose's push writes
stay PUT. An omitted field keeps its value; an explicit `null` clears a nullable field;
an explicit `null` on a field that can't be empty is a 422; unknown fields are a 422; the
answer is the whole resource. `apply_patch` does the first three from the body's
`model_fields_set`, the one thing that tells "left out" from "sent as null":

```python
from eifi1_server_kit.settings import apply_patch

@router.patch("/company/settings", response_model=CompanySettingsOut)  # rule 5: the whole resource
async def update_settings(body: CompanySettingsUpdate, …) -> CompanySettings:  # extra="forbid": rule 4
    apply_patch(company, body, not_nullable=("default_language", "default_account_country"))
    return company
```

`not_nullable` is required, so an endpoint whose fields may all be cleared says `()` out
loud. `defaults={"overspend": True}` makes a `null` reset to a value instead of `None`
(keksdose's push preferences). Every null is checked before anything is written, so a
refused body never leaves the row half changed. `skip=("types",)` leaves a field to the
caller: a null on it is still refused with the others, but it is neither written nor
answered (notifications' `types`, which `apply_switches_patch` writes). A body whose
every field is skipped has no row to write onto and passes `obj=None` (kastlan's
notification settings, `types` alone): the nulls are checked, nothing is written, and the
answer is `{}`. A model without `extra="forbid"`, a guard naming no field of the model,
or `obj=None` with a field that isn't skipped is a `TypeError` / `ValueError` on the
first call.

**The account's language** (§6.2) is one canonical `locale` from the app's offered
languages. `canonical_locale(tag, offered)` normalises case and `_` (`de_ch` → `de-CH`),
takes an exact offered tag, else the language's offered tag (`de`, `de-DE` → `de-CH`),
else `None`; it answers the tag as the app writes it, because every client compares the
stored string. It replaces keksdose's `canonical_locale`, kastlan's `normalize_lang` and
Kurvenschmiede's `LanguageIn`. `ProfileUpdate` takes the offered languages through a knob,
so `PATCH /auth/me` stores the canonical tag and answers an unoffered one with a 422:

```python
from eifi1_server_kit import auth, settings

# Either the class for the app's languages, in one call…
ProfileUpdate = settings.profile_update_model(("de-CH", "en", "fr", "it"))


# …or, to add fields, a subclass that sets the knob (mypy takes no call as a base class).
class KeksdoseProfileUpdate(auth.ProfileUpdate):
    offered_locales = LANGUAGES
    reporting_currency: CurrencyCode | None = None
```

`parse_accept_language(header, offered)` is for a mail to someone without an account; for
everyone else, mails and pushes use the account's locale, and the server never guesses
from the request. Nothing writes the account's locale on its own (§6.2): a data migration
through `canonical_locale` replaces keksdose's session-start sync.

### `eifi1_server_kit.demo`

The demo's server half (landing-demo contract §5–§7). The demo data and its seeder, the
access mechanics, the reap's row list and the refusal at each route stay in the app.

| Area | Names |
|---|---|
| Settings | `DemoSettings`: `demo_session_enabled` (**False**), `demo_user_max_age_hours` 24, `demo_session_max_live` 500, `demo_session_rate_max` 5, `demo_session_rate_window_seconds` 3600; `.demo_user_max_age`, `.demo_session_rate` (a `Budget`) |
| The gate | `DemoGate(settings, reap_limit=20).admit(ip, *, reap, count_live, is_ready, now=None)` → `DemoAdmission(now, cutoff, reaped)`; the callbacks' protocols `DemoReaper`, `DemoCounter`; `REAP_PER_START` 20 |
| Refusals | `DemoErrorCode` (`demo_disabled` 404, `demo_rate_limited` 429 + `Retry-After`, `demo_capacity` 429, `demo_not_ready` 503, `demo_read_only` 403, `demo_refused` 403), `DemoError(code, detail=None, *, retry_after=None)`, `refuse_demo(user_is_demo, what)` |
| Read-only (model R) | `demo_write_allowed(method, path, allow=frozenset())`, `READ_METHODS` |
| The user | `demo_address(domain)` (`demo+<32 hex>@demo.<domain>`), `is_demo_address(email, domain=None)`, `demo_password()`, `DEMO_FIRST_NAME`; `demo_expires_at(created_at, settings)`, `stale_cutoff(now, settings)` |

**The settings** are meant to be inherited by the app's own, so each reads its environment
variable there; the switch is off by default, and keksdose turns it on in its subclass:

```python
from eifi1_server_kit.demo import DemoSettings


class Settings(BaseSettings, DemoSettings):
    demo_session_enabled: bool = True
```

**The start**, in §5.1's order: the switch (404), the per-IP window (429 with
`Retry-After`, before any database work), the reap of at most the 20 oldest stale users,
the live cap counting only users younger than the maximum age (429), the demo data in any
version (503) — then the app mints. The reap and the count get one cutoff, so no user is
both stale and live:

```python
gate = app.state.demo_gate  # DemoGate(settings), built in create_app

@router.post("/auth/demo-session", status_code=201)
async def demo_session(request: Request, body: DemoSessionRequest | None = None, session=Depends(db)) -> TokenResponse:
    admission = await gate.admit(
        client_ip(request.headers, request.client.host if request.client else None, trusted_hops=HOPS),
        reap=lambda *, cutoff, limit: demo_service.reap(session, cutoff=cutoff, limit=limit),  # commits per user
        count_live=lambda *, cutoff: demo_service.count_live(session, cutoff=cutoff),
        is_ready=lambda: demo_service.is_ready(session),
    )
    user = await demo_service.mint(
        session,
        email=demo_address("kastlan.app"),
        password_hash=hash_password(demo_password()),
        first_name=DEMO_FIRST_NAME,
        created_at=admission.now,  # so the token and /auth/me end at the same instant
        locale=canonical_locale(body.locale if body else None, LANGUAGES) or DEFAULT_LOCALE,
    )
    token = jwt.encode(access_claims(user.id, now=admission.now, lifetime=settings.demo_user_max_age), …)
    ends = demo_expires_at(admission.now, settings)
    return TokenResponse(access_token=token, refresh_token=None, expires_at=ends, user=…)
```

The reap callback commits per user (a savepoint each), so a refusal after it never rolls
it back and one bad row can't block the batch; the same function serves the scheduled job
with `stale_cutoff(now, settings)`. **The token lives as long as the account** (24 h, no
refresh token), and ships only together with the refusals of §6.4.

**Read-only, layer 1** (model R, §6.3), in the app's `get_current_user`. GET, HEAD and
OPTIONS always pass; anything else only on the app's allow-list, which is empty by default:

```python
DEMO_WRITES = frozenset(
    {"POST /api/v1/auth/logout"}
)  # keksdose: {"POST /assistant/ask", "DELETE /assistant/threads/{id}"}

if user.is_demo and not demo_write_allowed(request.method, request.url.path, DEMO_WRITES):
    raise DemoError(DemoErrorCode.DEMO_READ_ONLY)
```

The demo's database layer — the read-only database transaction — is a recipe, not kit
code (§6.3); it is not the kit's layer 2, and a feedback service running in the app's
session keeps it.
**`refuse_demo(user.is_demo, "passkeys")` on every never-list route (§6.4), GET ones
included**: layer 1 catches only writes, and the account export is a GET. Mail to a demo
address is skipped silently (`is_demo_address`). Every app passes `is_demo` to
`name_incomplete`, or each demo is asked for a last name it doesn't have.

### `eifi1_server_kit.dev_switch`

The dev switch (top-bar contract §2.7, §6.1–§6.3; §6 wins), 0.14: in local development
only, a signed-in user sets their OWN role and plan with `POST /dev/switch {role?, plan?}`
(the kit's `DevSwitchers`), and the server enforces them. **The route lets a user make
themselves ADMIN**, so it must be impossible to switch on in a deployment by accident.
Layer 1: the gate, the startup check, the body and its validator; the route is a recipe,
like billing's, and the writes are the app's.

| Area | Names |
|---|---|
| Settings | `DevSwitchSettings`: `dev_switch_enabled` (**False**; `<APP>_DEV_SWITCH_ENABLED`, set only in the local `.env`) |
| The gate | `dev_switch_enabled(*, flag, local_run, production)` (true only for the flag AND the app's proof of a local run — a bool or a callable, asked only when it matters — AND not production; anything but real booleans shuts it), `assert_dev_switch_safe(*, flag, production)` → `DevSwitchUnsafeError` (a `RuntimeError`, at start), `require_dev_switch(gate)` (the route dependency: 404 unless `gate()` is `True`, the second lock behind the mount) |
| The wire | `DevSwitchRequest` (`role`, `plan`, `today`, at least one; extra fields refused; `today: null` clears the override, left out it is unchanged — `sets_today` tells them apart), `DevSwitchAnswer(role, plan, today)`, `validate_dev_switch(request, *, roles, plans=(), clock=False)` (the app's spelling back, compared without case; 422 otherwise; `today` kept as sent with `clock=True`, refused without it), `DEV_SWITCH_PATH` (`/dev/switch`), `DEV_SWITCH_LOG_TAG` (`dev_switch`) |
| The dev clock (0.20) | `effective_today(override, *, gate, today)`: the override while `gate` (the route's own predicate, or a callable) is exactly `True`, else the app's real `today()` — required, in the app's time zone (the server's own date is UTC on Cloud Run) — so an override left in a local database never applies in a deployment. The route passes `clock=True` to `validate_dev_switch`. Every path that asks for today asks this; the client takes today from `DevSwitchAnswer.today` while one is set (ui-kit's `DevMenu` "Time" section) |
| Refusals | `DevSwitchError`, `DevSwitchErrorCode`: `dev_switch_disabled` 404, `dev_switch_unknown_role` / `dev_switch_unknown_plan` 422, `dev_switch_refused` 409 (the app's own invariant), `dev_switch_no_clock` 422 (a `today` for an app without `clock=True`); `DEV_SWITCH_ERROR_STATUS`, `DEV_SWITCH_ERROR_DETAIL` |

**An environment name is never the gate** (§6.1). keksdose runs a hosted dev deployment
(`keksdose-dev`) and its `metrics_environment` defaults to `"dev"`; kastlan's
`environment` and Kurvenschmiede's `DEPLOYMENT` both DEFAULT to `"development"`, so a
deployment that forgot its variable would mount the route. The gate needs all three: the
explicit flag, the app's proof of a local run, and production off.

```python
class Settings(BaseSettings, DevSwitchSettings):  # KASTLAN_DEV_SWITCH_ENABLED=true in .env only
    model_config = SettingsConfigDict(env_prefix="KASTLAN_")


def dev_switch_on(settings: Settings) -> bool:
    return dev_switch_enabled(
        flag=settings.dev_switch_enabled,
        # A loopback database host and not on Cloud Run — never `environment != "production"`,
        # whose default is "development". KS: lambda: settings.is_development and is_local_database(settings)
        local_run=lambda: is_loopback(settings.database_url) and "K_SERVICE" not in os.environ,
        production=settings.environment == "production",
    )


def create_app(settings: Settings) -> FastAPI:
    assert_dev_switch_safe(flag=settings.dev_switch_enabled, production=settings.environment == "production")
    ...
    if dev_switch_on(settings):  # otherwise absent: 404
        app.include_router(dev_router)


dev_router = APIRouter(
    include_in_schema=False, dependencies=[Depends(require_dev_switch(lambda: dev_switch_on(get_settings())))]
)


@dev_router.post(DEV_SWITCH_PATH)
async def dev_switch(body: DevSwitchRequest, user=Depends(get_current_user), session=Depends(db)) -> DevSwitchAnswer:
    body = validate_dev_switch(body, roles=[r.value for r in Role], plans=PLANS)  # signed in, NOT require_admin
    ...  # the app's writes, below; then commit


@dev_router.get(DEV_SWITCH_PATH)
async def dev_switch_state(user=Depends(get_current_user)) -> DevSwitchAnswer:
    return DevSwitchAnswer(role=user.role, plan=...)  # the plan in effect, billing on or off
```

- **`GET /dev/switch` is the client's probe**, behind the same gate: a 404 means the gate
  is shut, so the client hides `DevSwitchers` (`enabled={import.meta.env.DEV && answered}`)
  rather than drawing flasks with nothing behind them; an answer gives `currentRole` and
  `currentPlan`, which the overview can't while billing is off (kastlan's `/billing/*` is
  404 then).

- **A signed-in user only, never ADMIN**: a lesser role must be able to switch back
  (`DevSwitchers` keeps the original role in session storage, "Back to {role}").
- **The role is written in the local database**, as every app reads it from the row:
  keksdose `users.role`; kastlan the acting company's `user_role_assignments` (replace
  the caller's roles there), then `issue_session`; Kurvenschmiede `users.role` on the
  caller's row directly, skipping `set_role`'s self and last-admin refusals, but refusing
  CUSTOMER for an account that owns work (`DevSwitchError(DevSwitchErrorCode.DEV_SWITCH_REFUSED,
  "Sign in as the seed's customer")`, 409).
- **The plan goes through the operator's grant, permanent** (`comped_until=None`):
  keksdose `set_plan`, kastlan `BillingService.set_plan`, Kurvenschmiede `change_plan` on
  the caller's own row. Billing §16's permanent-grant warning then shows, which is right —
  except on a row under a running beta grant (keksdose before launch): `comped_until=None`
  keeps beta and moves only the plan, so no permanent grant is made. Accepted: the switch
  is for previewing limits. With billing off nothing is enforced.
- **No admin action log entry**, or one tagged `dev_switch` (`DEV_SWITCH_LOG_TAG`).
- Keep the route out of the OpenAPI schema (`include_in_schema=False`); KS's demo walk
  classifies it `not_in_the_demo` and tests that a production settings object doesn't
  mount it.

### `eifi1_server_kit.billing`

Billing, plans and payment (billing contract §10 and §14; its §12 wins over the sections
above it, and §14.16 over the rest of §14). All three apps charge through a Merchant of
Record — Paddle (decision 16), one account for the three apps — in CHF and EUR, with
Paddle's checkout and a hosted portal only. A lapsed payer is read-only, never locked.
Layer 1: the subscription row and `billing_events`, the routes, the gate's allow-list, the
notice job's query, mail and trigger, and the provider seam stay in the app. The kit sends
one request each for checkout, portal, cancel and undoing a cancel, through
`billing.PaddleClient`, only with the **`billing` extra** (httpx, imported lazily: `import
eifi1_server_kit.billing` never needs it). **Lemon Squeezy is gone** since 0.8 (deprecated
in 0.7): `BillingProvider` is `paddle` alone, and a price id is text.

| Area | Names |
|---|---|
| Settings | `BillingSettings`: `billing_enabled` (**False**), `billing_provider` (`BillingProvider`: `paddle`; any other provider fails at start), `billing_api_key`, `billing_webhook_secret` (both `SecretStr`; with the provider, required to switch on), `billing_signature_tolerance` (seconds, Paddle's 5 by default), `billing_price_ids` (plan → currency → interval → price id, or a list with retired ids after the current one), `billing_launch_at`, `billing_app` (the app's tag, set in code — lowercase, a letter first, checked even where the app's subclass redeclares the field as `str`; with Paddle, required to switch on), `billing_environment` (`PaddleEnvironment`: `sandbox`, `live`; unset: the key's prefix), `billing_checkout_page_url` (the pay page), `billing_hosted_checkout_url` (wins over it); `.require_billing_enabled()` (404 `billing_disabled`), `.billing_price_id(plan, currency, interval)` (503 `billing_not_configured`), `.billing_price_ref(price_id)` → `PriceRef`, `.billing_plan_for_price(price_id)`, `.billing_webhook_key()`, `.billing_standing(row, now, *, retry_grace=None)` (with `billing_launch_at` as the launch), `.billing_api_environment()` (503 if unknown), `.verify_billing_webhook(provider, raw_body, headers, *, now=None)` (the secret AND the tolerance) |
| Plans | `PlanSpec(code, limits, prices, sort)` (`.limit(dimension)`, `.price(currency, interval)`), `plan_catalogue(plans)`, `normalize_plan(code)`, `check_limit(plan, dimension, used, *, adding=1)` → `PlanLimitError`, `dimensions_over_limit(plan, usage)`; `BillingCurrency` (`CHF`, `EUR`), `BillingInterval` (`month`, `year`), `CURRENCY_EXPONENTS`, `minor_to_decimal(amount, currency)`; the types `PlanCode`, `Currency`, `Interval`, `MinorUnits` |
| Standing | `SubscriptionStatus` (`trialing`, `active`, `past_due`, `canceled`, `expired`, `comped`), `SubscriptionSource` (`trial`, `provider`, `manual`, `beta`), the `SubscriptionRow` protocol; `in_good_standing(row, now, *, retry_grace=None, launch=None)`, `grant_holds(row, now, *, launch=None)`, `effective_comped_until(row, launch)`; `trial_ends_at(now)` (`TRIAL_LENGTH` 30 days), `beta_comped_until(launch)` (`BETA_FREE_MONTHS` 12), `is_beta(invitation_created_at, launch)` |
| The gate | `billing_write_allowed(method, path, *, standing, allow=frozenset())`, `refuse_billing_read_only(in_good_standing, what)` |
| Webhooks | `verify_webhook_signature(provider, raw_body, headers, secret)`, `verify_paddle_signature`, `PADDLE_SIGNATURE_TOLERANCE` 5 s; `EventKind`, `NormalisedEvent` (`.payer_ref`, `.app`, `.buyer`), `parse_webhook_event(provider, raw_body, *, app=None)`, `map_paddle_event`, `PoisonEventError`, `checkout_custom_data(payer_ref, *, app=None, buyer=None)`, `PAYER_REF_KEY`, `APP_KEY`, `BUYER_KEY` (kastlan's buying admin); the `EventStore` port and `DuplicateEventError`, `dispatch(event, store, apply, *, load, plan_for_price, now=None, launch=None)` → `DispatchOutcome`, `row_changes` (`provider_price_id` from every snapshot, a grant's too), `webhook_answer(result)` → `WebhookAnswer(status_code, log)` |
| The provider | the `BillingProviderClient` port (`checkout_url` with `customer_email`, `portal_url` with `PortalTarget` `overview` / `cancel` / `payment_method`, `cancel`, `remove_scheduled_cancel`, `preview_plan_change` / `change_plan` → `ProviderSwitchPreview`, `customer_for(email)`), `billing_provider_client(settings, *, client=None)` → `PaddleClient` or `NoProviderClient` (503); `sold_plan(catalogue, request, *, sold=None)` (422 `billing_plan_not_sold`), `require_new_checkout(row, now)` (409 `billing_already_subscribed`), `require_plan_switch(row, now)` (409 `billing_not_subscribed`), `switch_price(catalogue, row, request, settings, *, sold=None)` (422 `billing_plan_not_sold`, 409 `billing_plan_unchanged`, 503 without a known price on the row); `cancel_for_deletion(row, client)` → `CancelOutcome`, `resume_after_withdrawal(row, client, now=None)` → `ResumeOutcome`; (0.14) the portal for the buyer only, `PortalAccess` (`open` / `buyer_only`), `portal_access(row, *, viewer_is_buyer=None)`, `require_portal_access(row, *, viewer_ref)` (403 `billing_portal_buyer_only` when the row's `provider_payer_user_id` is set and isn't the viewer; a no-op without one), and the in-app cancel and its undoing, `async cancel_at_period_end(row, client)` and `async resume_scheduled_cancel(row, client)` (409 `billing_not_at_provider` without a subscription, a no-op when already cancelled / not cancelled, 502 / 503 as mapped; for `POST /billing/cancel` and `POST /billing/resume`, 204); `CHECKOUT_RETURN_PARAM`, `CHECKOUT_RETURN_VALUE`, `checkout_return_url(app_base_url, path)` |
| Paddle (the `billing` extra) | `PaddleClient(api_key, *, environment, app=None, checkout_page_url=None, hosted_checkout_url=None, timeout=15, client=None)`, `PaddleError` (`.status`, `.paddle_code`, `.paddle_detail`, `.request_id`; 502, or 503 for a 401/403 and for a 4xx in `PADDLE_CONFIGURATION_CODES` (0.13)), `paddle_environment_of(api_key)`, `paddle_locale(kit_locale)` (ui-kit's `paddleLocale`: `de-CH` → `de`, `zh` → `zh-Hans`, `hu` → `en`; a hosted checkout's `locale`), `PADDLE_API_BASES`, `PADDLE_API_VERSION`, `PADDLE_TIMEOUT_SECONDS` |
| Notices | `billing_notice_due(row, now, *, launch, sent, ahead=NOTICE_AHEAD, late_limit=NOTICE_LATE_LIMIT)` → `BillingNotice(kind, ends_at, days_left)` (`.key`, the marker), `BillingNoticeKind` (`trial_ending`, `trial_ended`, `grant_ending`, `grant_ended`), `NOTICE_STATUSES`; over the app's candidates, `billing_notices_owed(candidates, now, *, settings)` → `OwedNotice(payer, kind, ends_at, days_left, row)` (the candidate's row, typed as the app's) from `NoticeCandidate(payer, row, sent, deactivated, deletion_requested)` |
| Schemas | `BillingStatus`, `BillingOverview` (`.from_row(…, price_ref=None)`, with `at_provider`, `paid: PaidPrice | None` and (0.13) `switching`, which `provider_subscription_runs(row, now)` computes: a plan pick goes to the switch while it is true; (0.14) `portal` and `buyer_name` from `.from_row(…, viewer_is_buyer=None, buyer_name=None)`), (0.14) `BillingStanding` (`.from_row(row, *, now, retry_grace=None, launch=None, viewer_is_buyer=None, buyer_name=None)`: `{status, in_good_standing, trial_ends_at, comped_until, portal, buyer_name}`, for a `GET /billing/status` that carries the standing), `PlanOut` (`.from_spec(plan)`) with `PlanPrices` / `PlanIntervalPrices`, `plans_out(catalogue)`, `CheckoutRequest`, `CheckoutAnswer`, `PortalRequest` (`target`), `PlanChangeRequest`, `PlanChangeResponse` (`.of(previous_plan, plan, usage, *, kept_beta=False, comped_until=None)`; `comped_until` required, may be `null`), `AdminPlan` (`.from_spec(plan)`) and `admin_plans_out(catalogue)` for `GET /admin/plans`, the payer's switch `PlanSwitchRequest` `{plan, interval}`, `ProviderSwitchPreview` `{currency, now_minor, next_minor, next_billed_at}`, `PlanSwitchPreview` (that plus `{plan, interval, over_limit, limits, usage}`, from `plan_switch_preview(answer, plan, usage, *, interval)`), `PaidPrice` `{plan, interval, currency}`, `SyncRefusal` |
| Refusals | `BillingErrorCode` (`billing_disabled` 404, `billing_read_only` 402, `billing_not_configured` 503, `invalid_signature` 400, `billing_provider_unavailable` 502, `billing_not_at_provider` 409, `billing_already_subscribed` 409, `billing_plan_not_sold` 422, `billing_not_subscribed` 409, `billing_plan_unchanged` 409, `billing_portal_buyer_only` 403), `BillingError(code, detail=None)`; `PlanLimitError` 402 `{detail, code: "plan_limit", dimension, plan, limit, used}` |
| Test helpers | `eifi1_server_kit.billing.testing`, in the wheel, not re-exported: `paddle_event(step, *, payer_ref, price_id, app=None, …)`, `PADDLE_STEPS` (with `plan_switched`), `PADDLE_STEP_PERIODS`, `sign_paddle`, `signed_paddle_event`, `post_signed` (localhost only), `FakeBillingProvider` (`.previews`, `.switches`, `.customers` beside the others), `PaddleApiFake` (the preview, the update, `GET` / `POST /customers`; `.customers`, `.preview`), `PADDLE_API_EXAMPLES` |

**The switch is off by default**, and switching on without the provider, the API key or the
webhook secret fails at start. While off, `GET /billing/status` answers `{billing_enabled:
false}` (to a demo user too) and nothing else of billing exists: every other route and the
webhook answer 404 `billing_disabled`, and everyone is in good standing:

```python
from eifi1_server_kit.billing import BillingSettings


class Settings(BaseSettings, DemoSettings, BillingSettings):
    model_config = SettingsConfigDict(env_prefix="KEKSDOSE_")
    billing_app: str | None = "keksdose"  # the app's tag: in code, not per deployment
    # KEKSDOSE_BILLING_PRICE_IDS='{"pro": {"CHF": {"year": "pri_01…"}, "EUR": {"year": "pri_01…"}}}'
    # KEKSDOSE_BILLING_CHECKOUT_PAGE_URL=https://pay.keksdose.app/
```

**Switching on with Paddle also needs the app's tag and an environment** that resolves
and agrees with the API key: `billing_environment` (`sandbox`, `live`), or — unset — the
key's prefix (`pdl_sdbx_apikey_`, `pdl_live_apikey_`; a key from before 2025-05-06 has
none and needs the setting). A sandbox key against the live API fails at start, not at
the first checkout, and the API's address is the kit's constant, never a setting.
`.env.example` lists `<APP>_BILLING_APP` (as "set in code"), `<APP>_BILLING_ENVIRONMENT`,
`<APP>_BILLING_CHECKOUT_PAGE_URL` and `<APP>_BILLING_HOSTED_CHECKOUT_URL` (kastlan's name).
The two URLs are `https://`, or `http://` on localhost.

**An empty billing variable is an unset one**: `KEKSDOSE_BILLING_LAUNCH_AT=` is no launch
date and `KEKSDOSE_BILLING_PRICE_IDS=` no price ids, so an `.env` template can list every
variable empty until billing goes on. The price ids skip pydantic-settings' own JSON
decoding (its `NoDecode`), which refuses an empty value, and the mixin decodes them; only
the mixin's fields are read this way, never the app's own.

**A test that monkeypatches `billing_price_ids` passes the parsed shape**: lowercase plan
codes, `CHF` / `EUR`, `month` / `year`, and a LIST of ids per combination, current first —
`monkeypatch.setattr(settings, "billing_price_ids", {"pro": {"CHF": {"year": ["pri_test"]}}})`.
The string → list step, the keys' normalisation and the one-id-one-combination check are
the field's validation, which runs when the settings are built or validated, never on
attribute assignment (`BillingSettings` doesn't set `validate_assignment`, pydantic's
default). A bare `"pri_test"` slips through and is read character by character:
`billing_price_id("pro", "CHF", "year")` answers `"p"`, and the webhook's `"pri_test"` finds
no plan. To keep the environment's shape, validate it first:
`BillingSettings(billing_price_ids={"pro": {"CHF": {"year": "pri_test"}}}).billing_price_ids`.

**The catalogue lives in code**; the provider's price ids live in the settings, the names
in the app's i18n. A limit gates creation only — a downgrade never deletes or hides
anything — and comes after the read-only gate, so a lapsed payer creating gets
`billing_read_only`, never `plan_limit`:

```python
PLANS = plan_catalogue(
    [
        PlanSpec(code="free", limits={"budgets": 1}),
        PlanSpec(code="pro", limits={"budgets": 5}, prices={("CHF", "year"): 3000, ("EUR", "year"): 3000}, sort=1),
    ]
)

refuse_billing_read_only(settings.billing_standing(owner_row, now), "a new budget")  # 402 billing_read_only
check_limit(PLANS[normalize_plan(owner_row.plan_code)], "budgets", owned_budgets)  # 402 plan_limit
```

**`GET /billing/plans`** answers `plans_out(PLANS)`: a `PlanOut` per plan, `{code, prices,
limits, sort}` in `sort` order, with the prices nested currency → interval → gross minor
units — ui-kit's `PlanPrices`, so `BillingPlan.prices` takes them as they are
(`{"CHF": {"month": 7900, "year": 79000}}`). The kit's `PlanPrices` and
`PlanIntervalPrices` name every key, so openapi-typescript generates `{CHF?: {month?:
number; year?: number}; EUR?: …}` — ui-kit's type, no cast. A combination the plan doesn't
sell is left out, never `0`, and unlimited is `null`. The names and feature lines are the page's, from
the app's i18n by `code`. **`GET /admin/plans`** (admins only, whatever the switch says)
answers `admin_plans_out(PLANS)`: an `AdminPlan` `{code, limits, sort}` per plan, the
ones not sold any more included and no prices — the operator's plan picker and the
roster's plan filter.

**Whose standing**: the payer's, not the caller's — keksdose's budget owner, Kurvenschmiede's
row owner (the creator's for a create), kastlan's acting company. No row means always in
good standing (demo users, ownerless items) — so with RLS on the subscription table, read
the payer's row with the bypass, or a guest reads it back as nothing and the gate fails
open; where a row must exist, a missing one is an error. The gate is the app's choice of two equal
shapes: `billing_write_allowed` at the auth dependency, after the demo's 403, with an
allow-list in the demo's syntax (billing, the account's settings, signing out, feedback,
removing access, leaving, admin routes, receiving sync); or `refuse_billing_read_only` at
the app's own write choke points. Sync stays on the list and refuses changes one by one, in
a `SyncRefusal` beside the updates. Scheduled jobs skip a lapsed payer's data themselves.

**The row's dates**: a new payer starts `trialing` until `trial_ends_at(now)` (a pure guest's
row waits with it empty until their first owned item); a beta payer — existing at launch,
or invited before it (`is_beta`) — is `comped` until `beta_comped_until(launch)`; an
operator's grant is `comped`, `manual`, with or without an end (`AdminAction.PLAN`,
`acknowledge`; `PlanChangeRequest` / `PlanChangeResponse`) — except that a running beta
grant given no new end keeps its beta: only the plan moves, or a pre-launch move would be
free for good (keksdose's `kept_beta`). A beta row written before the
launch date is known (the beta migration, a registration before launch) stores no
`comped_until`, and the kit reads it as `beta_comped_until(settings.billing_launch_at)` at
read time, so a moved launch date needs no data change: `effective_comped_until(row,
launch)` for a banner's "free until …", and pass `launch=settings.billing_launch_at` to
`in_good_standing`, `grant_holds`, `dispatch` and `BillingOverview.from_row`
(`settings.billing_standing` passes it itself). Without a launch date such a row has no
end — and switching billing on does not require one.

**The webhook**, one per provider, `POST /webhooks/<provider>`, in one transaction:

```python
@router.post("/webhooks/paddle")
async def paddle_webhook(request: Request, session=Depends(system_session)) -> Response:
    settings.require_billing_enabled()  # 404
    raw = await request.body()
    settings.verify_billing_webhook("paddle", raw, request.headers)  # 400; the secret AND the tolerance
    try:
        event = parse_webhook_event("paddle", raw, app=settings.billing_app)  # None: not used, or not ours
        result = (
            None
            if event is None
            else await dispatch(
                event,
                EventTable(session),
                apply,
                load=find_payer_row,
                plan_for_price=settings.billing_plan_for_price,
                launch=settings.billing_launch_at,
            )
        )
        await session.commit()
    except Exception as exc:
        await session.rollback()
        result = exc
    answer = webhook_answer(result)
    if answer.log:
        logger.warning("billing webhook %s: %r", answer.status_code, result)  # never the body
    return Response(status_code=answer.status_code)
```

The signature follows Paddle's docs: `Paddle-Signature: ts=…;h1=…` (hex HMAC-SHA256 of
`ts:raw_body`, any `h1` during a secret rotation, and Paddle's five-second tolerance on `ts`
unless `billing_signature_tolerance` widens it — a scale-to-zero host's cold start can eat
five seconds; keksdose runs 60). The checkout carries
`checkout_custom_data(payer_ref)`, which Paddle sends back with every subscription
event, so `load` finds the row by `event.payer_ref`, else by the subscription id — never
by the customer id (0.12): one Paddle customer per e-mail address serves every app and
every company a person pays for. `dispatch`
skips a duplicate — one `seen` finds, or one whose insert hits the unique key, which the
store's `record` reports by raising `DuplicateEventError` (flush the insert there and undo
it: a savepoint where the driver nests them, else a rollback, which also works on SQLite in
tests); skips a snapshot older than `updated_from_event_at` (Paddle doesn't
guarantee the order); skips a replaced subscription's late end; leaves a running grant's
status, source and plan alone (writing only the link and dates — the provider's paid period
counts once the grant ends); and otherwise hands `apply(event, row, changes)` the columns
to write. Unknown and duplicate events answer 200, poison 200 and a log line, a price the
settings don't know 503, anything else 500, so the provider retries. The event table sits
outside tenant RLS: a webhook writes rows for any payer.

**One Paddle account, three apps** (decision 18): Paddle's notification destinations can't
filter by product or custom data, so every app's webhook receives every app's events.
Every checkout carries `{"payer_ref": …, "app": <billing_app>}` (the kit's client adds the
tag itself), and `parse_webhook_event(…, app=settings.billing_app)` answers `None` — 200,
never recorded or dispatched — for another app's event and, once `app` is given, for an
untagged one (logged at WARNING): it is never matched by the provider's customer id, since
one person may be one Paddle customer across the apps and `user:42` repeats between
keksdose and Kurvenschmiede.

**The provider** is one client behind one port, built from the settings, behind a
**module-level seam** — a deletion's cancellation runs in the service layer, where a
FastAPI override can't reach — which the tests replace with `FakeBillingProvider`:

```python
def billing_client() -> BillingProviderClient:  # the app's seam
    return billing_provider_client(get_settings(), client=shared_http_client)


@router.post("/billing/checkout", response_model=CheckoutAnswer)
async def checkout(body: CheckoutRequest, payer: Payer = Depends(current_payer)) -> CheckoutAnswer:
    settings.require_billing_enabled()  # 404
    sold_plan(PLANS, body, sold=SOLD)  # 422 billing_plan_not_sold
    row = await row_of(payer)
    require_new_checkout(row, now())  # 409 billing_already_subscribed: change plans by the switch
    url = await billing_client().checkout_url(
        price_id=settings.billing_price_id(body.plan, body.currency, body.interval),  # 503
        custom_data=checkout_custom_data(payer_ref(payer), app=settings.billing_app),
        customer_id=row.provider_customer_id,
        customer_email=payer.sign_in_email,  # no customer yet: the shared one, found or created
    )  # 502 billing_provider_unavailable
    return CheckoutAnswer(url=url)


@router.post("/billing/portal", response_model=CheckoutAnswer)
async def portal(body: PortalRequest | None = None, viewer: User = Depends(current_user)) -> CheckoutAnswer:
    settings.require_billing_enabled()
    row = await row_of(payer_of(viewer))
    require_portal_access(row, viewer_ref=str(viewer.id))  # 403 billing_portal_buyer_only; no buyer stored: a no-op
    return CheckoutAnswer(
        url=await billing_client().portal_url(
            customer_id=row.provider_customer_id,  # None: 409 billing_not_at_provider
            subscription_id=row.provider_subscription_id,
            target=(body or PortalRequest()).target,  # overview, cancel, payment_method
        )
    )


@router.post("/billing/cancel", status_code=204)  # optional for a person-payer app
async def cancel(payer: Payer = Depends(current_payer)) -> None:
    settings.require_billing_enabled()
    await cancel_at_period_end(await row_of(payer), billing_client())  # 409 billing_not_at_provider, 502, 503


@router.post("/billing/resume", status_code=204)  # "Keep subscription"
async def resume(payer: Payer = Depends(current_payer)) -> None:
    settings.require_billing_enabled()
    await resume_scheduled_cancel(await row_of(payer), billing_client())
```

**The portal for the buyer only, and the in-app cancel** (0.14, contract §16): where the
row stores a buyer (kastlan's `provider_payer_user_id`), every portal target opens for
that user only, since each reaches the overview of all the buyer's subscriptions. The
overview's `portal` says `buyer_only` to anyone else, with the buyer's name (`buyer_name`,
`None` once the buyer is gone or has left). Another admin cancels in the app, and any admin
keeps the subscription again. Both routes are for every admin who may read the overview,
the buyer included. They take no write lock (a read-only payer may always cancel: on the
gate's allow-list beside `/billing/portal`) and sit behind the app's demo guard. The row
follows Paddle's `subscription.updated`.

The checkout opens on the kit's pay page on the app's pay host
(`billing_checkout_page_url`, `https://pay.<app domain>/`, sent as the transaction's
`checkout.url`), or on a Paddle hosted checkout (`billing_hosted_checkout_url`: every
sandbox, live only with Paddle's approval). No success URL is ever on the request; the way
back is `?checkout=done` on the subscription page (`checkout_return_url(app_base_url,
path)`), the pay page's `successUrl` or the hosted checkout's redirect. The overview's
`at_provider` says whether a provider customer exists: without one the page shows a
caption in place of "Payment and invoices" and "Cancel", and never asks for the portal.
Paddle failing is 502 `billing_provider_unavailable` ("try again"); a 401/403 — a wrong
key, or one without transaction, customer-portal-session and subscription write or (0.12)
customer read and write — 503
`billing_not_configured`; so is a 4xx whose code is in `PADDLE_CONFIGURATION_CODES` (0.13: a setup
refusal, such as `transaction_checkout_url_domain_is_not_approved`, not an outage). Each failure is logged once with the method, path, status,
Paddle's code and request id, never the body or the key; nothing is retried, since a
transaction is not idempotent.

**The plan switch** (0.12, contract §15.2.10) replaces the portal for plan changes:
`POST /billing/plan/preview` and `POST /billing/plan`, both `{plan, interval}` →
`PlanSwitchPreview`, for whoever may check out, behind the app's demo guard and the
billing switch. `require_plan_switch(row, now)` (409 `billing_not_subscribed`, the inverse
of the checkout's rule), `switch_price(PLANS, row, body, settings, sold=SOLD)` — the price
in the currency the row pays, from its `provider_price_id` — then
`billing_client().preview_plan_change(…)` or `.change_plan(…)` (Paddle's `PATCH
/subscriptions/{id}/preview` and `PATCH /subscriptions/{id}`, prorated immediately both ways,
a declined upgrade changing nothing), and `plan_switch_preview(answer, PLANS[body.plan],
usage, interval=body.interval)`. The row follows through `subscription.updated`. **The
row keeps the price it pays** — `provider_price_id`, a new column every app adds
(`VARCHAR(200) NULL`, breaking) — and `BillingOverview.from_row(…,
price_ref=settings.billing_price_ref)` says it as `paid`. **The shared customer**: the
checkout passes the payer's sign-in e-mail, and without a `provider_customer_id` the client
finds (`GET /customers?email=`) or creates the one Paddle customer for that address; the
row's id is still written only by the webhook.

**A second checkout while subscribed** is refused, 409 `billing_already_subscribed` —
including a payer who bought while a grant held, whose row keeps `comped` but carries the
provider's link and a period still running; one set to cancel at its period's end doesn't
block. **At a deletion request** `cancel_for_deletion(row, billing_client())` cancels at
the period's end (a paused subscription — `expired` — at once) and never raises: `None`
(nothing to cancel), `"cancelled"`, or `"failed"` with an ERROR "cancel it by hand"; put
the outcome in the request's audit detail. **At a reactivation**, only where that detail
says `"cancelled"`, `resume_after_withdrawal(row, billing_client())` removes the scheduled
cancellation (Paddle's `PATCH /subscriptions/{id} {"scheduled_change": null}`) — whatever
the row's `cancel_at_period_end` says, which only Paddle's webhook sets (not yet arrived,
or refused while billing is off); a subscription cancelled at once, past its period, or
one Paddle answers has ended isn't resumed (`None`) — the payer subscribes again.

**Trial and grant end notices** (§12.22) are each app's daily job; the kit decides only
which payer is owed which notice. `billing_notice_due(row, now, launch=…, sent=…)`
answers `BillingNotice(kind, ends_at, days_left)` — `…_ending` from 7 days before the end,
`…_ended` from the end until 3 days after (a missed `…_ending` is skipped) — for the
cardless trial and for a grant (a beta row's end is the launch plus 12 months; no notice
while a paid period runs past the grant), or `None`, also when `sent` is the notice's
`key`. The job: nothing while billing is off; rows with `status IN NOTICE_STATUSES`, read
with the RLS bypass; no deactivated account and none with a deletion request; the app's
own mail per kind and locale; then `row.billing_notice_sent = notice.key`
(`VARCHAR(64) NULL`) once `send` returned True, committed per row.
`billing_notices_owed(candidates, now, settings=settings)` is the same decision over
`NoticeCandidate`s, the switch and the skipped accounts included; each `OwedNotice` hands
the candidate's row back for the marker (`owed.row.billing_notice_sent = owed.key`). The trigger — a token
in `X-Jobs-Token`, never the query string — is the app's; the kit has no helper for it.

**`eifi1_server_kit.billing.testing`** ships in the wheel for the apps' tests and local
CLIs (stdlib only; the API fake imports httpx when used) and replaces each app's fixture
builder and signer: `signed_paddle_event("checkout", secret=…, payer_ref=…, price_id=…,
app=…)` gives the raw body and its `Paddle-Signature`, for every step of a subscription's
life including the scheduled cancellation; `post_signed(url, raw, headers)` posts it to
localhost only; `FakeBillingProvider` records the port's calls (and fails on demand), and
`PaddleApiFake` is Paddle's API as an `httpx.MockTransport` answering Paddle's documented
examples.

### `eifi1_server_kit.jobs`

The scheduler's token routes (billing contract §14.7): a daily job is `POST /api/v1/ops/…`,
called by a Cloud Scheduler HTTP job with no account behind it, so the caller proves it is
the job with `X-Jobs-Token` (`JOBS_TOKEN_HEADER`) — a header, never a query parameter,
which Cloud Run's request log records. `JobsSettings`, a mixin like `DemoSettings`, holds
`jobs_token` (`SecretStr | None`, `<APP>_JOBS_TOKEN`). `require_jobs_token(token)` is the
FastAPI dependency, reading the setting through `token` at every call;
`check_jobs_token(presented, expected)` is its check, in constant time on bytes. Missing,
wrong, or no token set: `JobsTokenError` (401, one answer for the three; the last logged at
WARNING). Lifted from kastlan's and Kurvenschmiede's `ops_router.py`.

```python
from eifi1_server_kit.jobs import require_jobs_token

router = APIRouter(prefix="/ops", dependencies=[Depends(require_jobs_token(lambda: get_settings().jobs_token))])
```

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
`MailMessage(subject, text, html)`. The plain text's link line is `{cta}: {link}`, with
the no-break space French sets before the colon (`Choisir\u00a0: …`).

The HTML is a **whole document**: `<!doctype html><html lang="…"><head><meta
charset="utf-8"><title>{subject}</title></head><body>…</body></html>`. The `lang` is the
row's own language, so a German fallback says `de-CH`. Outlook junked a fully
authenticated Kurvenschmiede mail that was only a fragment.

`Mailer` is the protocol (`await mailer.send(to, message, kind="verification") -> bool`,
never raises). The `kind` is what the mail is for, in its log lines and Resend's
`Idempotency-Key`: a one-time link's `OneTimeTokenKind`, billing's `BillingNoticeKind`, or
`AccountNoticeKind` for the account notices with no token — `email_change_notice` (to
the old address, user-admin §6.2), `account_deletion` (the answer to a deletion
request, §6.4; kastlan said `deletion`, Kurvenschmiede `deletion_request`) and
`work_transferred_notice` (Kurvenschmiede: to an active former owner whose work an admin
handed on, notifications §9.3) and `feedback_published` (0.11: once to an author of
reports the round-0.38 migration made public, feedback §9.12).
`ResendClient(api_key=, from_address=, timeout=10, client=None,
reply_to=None)` posts to Resend, retrying once only on a transport error, `429` or `5xx`,
under one `Idempotency-Key` — `MailMessage.idempotency_key` where the message has one,
else a random `{kind}-{uuid}` — and never logs the recipient. A `409` (the key's first
request still running, or the key used with another body) is not sent and not retried.
**Set `reply_to`** from the
app's `*_EMAIL_REPLY_TO` setting, whose default is `support_address(from_address)`, i.e.
`support@<the sender's domain>`. Cloudflare Email Routing forwards `support@` on all three
domains, and nobody reads `noreply@`. `ConsoleMailer(level=logging.INFO)` logs
the mail with its link — development only; kastlan passes `logging.WARNING`.

**The admin's test mail** (0.15, admin §9.2): `test_mail_message(locale, *, app_name,
backend, sent_at)` → `HtmlMailMessage` in the admin's language (seven, through `pick_entry`'s
fallback), naming the app, the transport and the time in UTC — never the recipient, so
neither the mail nor the route's log line carries the address; `sent_at` must be aware.
`HtmlMailMessage` (0.16.0) is a `MailMessage` whose `html` is a `str`, so an app whose own
send takes `html: str` passes `message.html` as it is, no `or ""`.
`MAIL_TEST_KIND` (`mail_test`) is its `kind` and metric label, `MAIL_TEST_LIMIT` (3 per 10
minutes per account) the app's limiter's budget, `MailTestResult` `{sent, mail_backend}` the
answer. No router: each app's `POST /admin/mail/test` (kastlan `/platform/mail/test`) is a
few lines over its own transport. **Await the app's own send and return its bool — never an
after-commit hook**, which would answer before anything was sent:

```python
from eifi1_server_kit.mail import MAIL_TEST_KIND, MailTestResult, test_mail_message


@router.post("/admin/mail/test", response_model=MailTestResult, dependencies=[Depends(mail_test_limit)])
async def send_test_mail(admin: User = Depends(require_admin)) -> MailTestResult:
    backend = settings.mail_backend  # what GET /admin/users reports as mail_backend
    message = test_mail_message(admin.locale, app_name="Kurvenschmiede", backend=backend, sent_at=utc_now())
    sent = await mailer.send(admin.email, message, kind=MAIL_TEST_KIND)
    return MailTestResult(sent=sent, mail_backend=backend)
```

**The card reads `mail_backend` before `sent`**: a console transport answers `True`
although nothing left the server (`MAIL_BACKEND_CONSOLE`), so ui-kit's `MailTestCard` says
"nothing was sent" there whatever `sent` is. A provider's error stays in the server log,
never in the answer.

**A notification mail** (notifications §3.6) is sent under its type's id as `kind`
(`share_granted`) and carries RFC 8058's one-click unsubscribe. `MailUnsubscribe(label,
page, one_click)` is its way out: `label` the footer line in the recipient's language,
`page` the app's signed-out `/unsubscribe?token=…` page, `one_click` the `POST
/notifications/unsubscribe/{token}` URL — https, or `None` on a development server, which
then sends the footer line without the headers. Pass it as `render_mail(…,
unsubscribe=…)`: `render_message` adds the line after the footer, escaped and typeset like
the action line (`{label}: {page}` in the text half), and `MailMessage.headers` gets
`list_unsubscribe_headers(one_click)` — `List-Unsubscribe: <…>` and
`List-Unsubscribe-Post: List-Unsubscribe=One-Click`. `ResendClient` sends `headers` as
Resend's `headers` object, `ConsoleMailer` logs them, and kastlan's SMTP transport sets
them on its message. Account mail and billing's notices carry none. A notification mail a
job sends again carries `idempotency_key` (`MailMessage(…, idempotency_key=…)`, 1–256
printable ASCII characters, no space), the send's request header to Resend, which
`ConsoleMailer` ignores (`notifications`, "a failed send").

### `eifi1_server_kit.notifications`

The server half of the notifications round (notifications contract §4.1). Layer 1 like the
rest: no tables, no routes, no sending, no new dependency. The app keeps its registry's
words, its tables (the switches, the settings row with `digest_sent`), its routes and the
demo's refusal, its triggers, its jobs, its service worker and its sending (`pywebpush`
off the event loop, its mailer). keksdose is the source
(`backend/keksdose/domain/schemas/push.py` and its services).

| Area | Names |
|---|---|
| The types | `NotificationType(id, app, email=None, quiet=True, push=None, bundle=None)`, `NotificationTypes(*types, push_channel=True)` (`in`, `[id]`, `.ids`, `.push_channel`, `.receivable(keep)`), `TYPE_ID_PATTERN` |
| The settings resource | `NotificationSettingsResponse`, `QuietHoursFields`, `DigestFields`, `TypeSwitches(app, email, push=None)`; `NotificationSettingsUpdate` (class knob `registry`), `QuietHoursUpdate` (both refuse a `timezone` from 0.18, a 422 as any unknown field — ignored in 0.16 and 0.17), `DigestUpdate`, `TypeSwitchesPatch`; `StoredSwitches(app, email, push)`, `resolve_switches(types, stored)`, `apply_switches_patch(types, stored, patch, *, couple_push_to_app=False)`, `migrate_enabled_to_channels(enabled)`, `SETTINGS_NOT_NULLABLE`, `SETTINGS_SKIP` |
| Delivery | `plan_delivery(type, switches, *, active, is_actor, quiet_now, email_verified, keeps_inbox, service=False)` → `Delivery(inbox, push, email)`, `Delivery.NONE`; per device (0.16) `device_receives(muted_types, type_id)`, `read_muted_types(types, stored)`, `apply_muted_types(types, muted)` |
| Quiet hours, the zone, the digest | `in_quiet_hours(start, end, zone, now)`, `quiet_hours_end(start, end, zone, now)`, `effective_zone(device_timezone, fallback=DEFAULT_ZONE)`, `local_today(zone, now)`, `resolve_zone(name, fallback=DEFAULT_ZONE)`, `is_known_zone`, `digest_period(cadence, day)`, `digest_due(cadence, zone, now, *, sent, late_days=3)`, `digest_tag(period)`, `DigestCadence`, `DIGEST_CADENCES`, `DIGEST_LATE_DAYS`, `DEFAULT_ZONE`, `QUIET_HOURS_PATTERN` |
| A failed send | `mail_due(event_day, today, *, sent, late_days=3)`, `mail_event(*parts)`, `MailMarker(user_id, type_id, event)` (`.idempotency_key(day)`), `MAIL_LATE_DAYS`, `MAIL_MARKER_RETENTION` (30 days), `MAIL_EVENT_PATTERN` |
| Push | `PushSubscriptionIn`, `PushSubscriptionKeys`, `PushUnregisterIn`, `PushKeyResponse`, `PushTestResponse`, `PushDeviceResponse` (`muted_types`), `PushDeviceUpdate(muted_types)`; `PushPayload(title, body, tag, data).encode(limit=3072)`, `PushData(url, type, inbox=True)`; `push_outcome(status)` → `PushOutcome`, `should_reap(failure_count, *, limit=PUSH_MAX_FAILURES)`, `push_gone_expired(gone_at, now, *, grace=PUSH_GONE_GRACE)`, `vapid_subject(support_address)`; `parse_push_body(model, body)`, `parse_push_replace(body)` (0.17.0: a bad body is a plain 422 that never echoes `old_auth` or a key; a body that isn't valid JSON is refused by FastAPI first, in the redacted `json_invalid` shape — one route, two 422 shapes, neither echoes); `documented_body(model)` (0.18: the `openapi_extra` that documents an untyped `Annotated[Any, Body()]` body from its model, nested models inlined); `PUSH_TTL`, `PUSH_TIMEOUT`, `PUSH_MAX_FAILURES`, `PUSH_PAYLOAD_LIMIT`, `PUSH_TEST_TYPE`, `PUSH_GONE_GRACE` (7 days) |
| Unsubscribe | `unsubscribe_token(secret, user_id, type_id)`, `read_unsubscribe_token(secret, token)` → `UnsubscribeClaim(user_id, type_id)`, `unsubscribe_key(secret)` |
| The inbox | `InboxItem` (`company`, 0.16), `InboxResponse`, `InboxMarkRead`, `InboxMarkReadResponse`, `INBOX_RETENTION` (90 days); layer 2 (0.16): `InboxStore` (`add`, `page`, `mark_read`, `clear`, `prune`), `NewInboxItem`, `INBOX_PAGE_DEFAULT`, `INBOX_PAGE_MAX` |
| The queue (0.16, layer 2, opt-in) | `NotificationQueue` (`enqueue`, `open_bundle`, `claim`, `done`, `drop`, `defer`, `purge`), `NewQueueItem`, `QueueItem`, `QueueChannel`, `FlushPort` (`still_wanted` — 0.17.0: gets the flush's `now=` when it takes one —, `send_push`, `send_mail`), `QueueService(queue, port)` (`hold_push`, `queue_mail`, `flush`, `purge`), `FlushResult(sent, dropped, left, purged)`, `notifications_tick(service, now)`, `QUEUE_CLAIM_TIMEOUT` (10 min), `QUEUE_MAX_ATTEMPTS` (3), `QUEUE_RETENTION` (30 days), `QUEUE_FLUSH_LIMIT`, `QUEUE_MORE` |
| Routes (`notifications.router`, the `fastapi` extra) | `InboxDeps(store=, current_user_id=, guards=())`, `inbox_router(deps, *, prefix="/notifications/inbox")`; `notifications_jobs_router(service, require_jobs_token, *, now=utc_now, prefix="/ops/notifications")` → `POST /flush` |
| Testing (`notifications.testing`) | `InMemoryInboxStore`, `InMemoryNotificationQueue` (`commit`, `rollback`, `commits`), `RecordingFlushPort` |
| Refusals | `NotificationError`; `NotificationTypeError` (422, `notification_type_not_received`, `types`), `NotificationChannelError` (422, `notification_channel_not_offered`, `types`, `channel`; 0.16), `PushEndpointTakenError` (409, `push_endpoint_taken`), `PushBodyInvalidError` (422, `push_body_invalid`, `fields`; 0.17.0), `UnsubscribeTokenError` (400, `unsubscribe_invalid`) |

**The types are the app's**: a registry built once, the one list — the `GET` answers every
type the account receives, in its order, and the card draws what it answers. A type
offers `app` (in the app), `push` (0.16; `app`'s default unless set) and `email`, each with its default; off
unless it can only fire in answer to the person's own action. `receivable(keep)` is the
account's narrowing (kastlan: staff in any company).

**`GET/PATCH /notifications/settings`** under the settings rule. The body is the kit's
models with the app's registry and knobs; `types` goes through `apply_switches_patch` (a
type's `null` resets both switches, a switch's `null` that one, an unreceived type a 422
before anything is written), the rest through `apply_patch`:

```python
from eifi1_server_kit import notifications as kit
from eifi1_server_kit.settings import apply_patch


class SettingsUpdate(kit.DigestUpdate, kit.QuietHoursUpdate, kit.NotificationSettingsUpdate):
    registry = TYPES  # an unknown type, or e-mail on a type without, is a 422
    low_balance_threshold: Decimal | None = Field(default=None, ge=0)


@router.patch("/notifications/settings", response_model=SettingsOut)
async def update_settings(body: SettingsUpdate, …) -> SettingsOut:
    types = TYPES.receivable(lambda _type: True)
    changed = kit.apply_switches_patch(types, stored, body.types)
    apply_patch(row, body, not_nullable=kit.SETTINGS_NOT_NULLABLE, skip=kit.SETTINGS_SKIP)
    …  # write `changed`; an app without a digest passes not_nullable=kit.SETTINGS_SKIP
    return SettingsOut(types=kit.resolve_switches(types, {**stored, **changed}), …)
```

**Delivery** (§3.3, §10.2): nothing for an inactive account, a demo or the event's actor;
`app` on is an inbox row; `push` on (0.16: its own switch) a push, held for a `quiet` type
inside quiet hours; `email` on is the mail, only to a verified address; `service=True`
skips the switches in the app and by push (keksdose's guest deadline). **Quiet hours** fail
open (half set, start = end); the zone is the account's (`effective_zone`), and an unset
or no longer resolvable one is `Europe/Zurich`, never UTC. **The digest** is due from its period's first local day until
3 days after, once per period: store `digest_due`'s key as `digest_sent` in the
transaction that writes its inbox row (an app with no inbox: once the send answered
`True`); its push's tag is `digest:2026-W42`.

**A failed send** (§9.4): a notification mail a job sends is owed until it went, for 3
days; one a request sends is tried once, after the commit, and a failure is logged; a
push is never retried. Each scheduled run (never the run at the app's start) reads the
events still due by `mail_due(event_day, today, sent=…)` and their recipients by
`plan_delivery`, read again at every try, and skips a recipient with a marker,
`MailMarker(user_id, type_id, event)`. The send carries the marker's
`.idempotency_key(today)` as `MailMessage.idempotency_key`:
`<type>:<event>:<user_id>:<YYYY-MM-DD>`, so two runs on one day send one mail (Resend
keeps a key 24 hours). Sent: the marker row, `ON CONFLICT DO
NOTHING`, committed per recipient; not sent: logged with the type and the event, nothing
written. The event key is the app's, built by `mail_event` (kastlan:
`mail_event(company_id, day)` → `"42:2026-10-13"`): ids and days, no personal data, no
sealed content. The same job deletes markers older than `MAIL_MARKER_RETENTION`.

**Push**: the payload is `{title, body, tag, data: {url, type, inbox}}` — `url` a path in
the app, never `//` or a scheme — encoded under 3 KB with the body cut by "…". A 404 or
410 marks the device gone (`gone_at`, deleted after `PUSH_GONE_GRACE`; 0.16), anything
else counts a failure, and 10 in a row reap it
(`should_reap(count, limit=…)` takes the app's own limit). TTL 24 h and
timeout 10 s, passed explicitly. The VAPID subject is `mailto:` the support address.

**One-click unsubscribe**: `v1.<data>.<mac>`, signed with a key derived from a dedicated
unsubscribe secret the app passes (its own setting, not the JWT secret), not stored, never expiring; it names one account and one type
and can only turn that e-mail off. A bad token is a 400; a GET never unsubscribes. The
mail half is `mail.MailUnsubscribe`.

**Round 0.46** (server-kit 0.16, notifications §10). **Three channels**, each its own
switch: `app` (in the app), `push` and `email`; push no longer needs the inbox, and a push
with no inbox row carries `data.inbox: false`. An app with no push builds its registry
with `push_channel=False` (kastlan, Kurvenschmiede): every `push` is `null`, and a PATCH
sending one is a 422. keksdose migrates `enabled` with `migrate_enabled_to_channels` and
passes `couple_push_to_app=True` in its legacy `/push/preferences` shim only, while old
bundles send `{app}` alone (never in `PATCH /notifications/settings`, where the card sends
`{app}` alone on purpose). A device mutes
types (`muted_types`), never adds them. **The zone is the account's**, automatic only
(amended 2026-10-20): `effective_zone(user.device_timezone)` (the device last used, else
Zurich; no pick) for everything written for one person, `local_today(zone, now)` for "today".
A `timezone` (`PATCH /notifications/settings`, keksdose's `/push/settings` shim,
`PATCH /auth/me`) is a 422 from 0.18, as any unknown field is (ignored in 0.16 and 0.17):
breaking only for a stale ≤0.45 bundle, which still sends one.
The tick is idempotent, so a job runner may retry it.
**The inbox** has a port and three routes; **the queue** is opt-in — an app that runs it
holds a quiet type's push instead of dropping it, and calls the tick every 15 minutes:

```python
from eifi1_server_kit import notifications as kit

zone = kit.effective_zone(user.device_timezone)
if switches.push and kind.quiet and kit.in_quiet_hours(start, end, zone, now):
    until = kit.quiet_hours_end(start, end, zone, now)
    await queue_service.hold_push(user.id, kind.id, event, payload, not_before=until, now=now)

result = await kit.notifications_tick(queue_service, now)  # the 15-minute job
```

**The rotation route** (`POST /notifications/devices/replace`, §10.6): `PushReplaceIn`'s
docstring holds its rules — the old row proven before the 409, an inactive or demo
account the same 404 (and its row deleted), a row marked gone claimable for
`PUSH_GONE_GRACE` (`push_gone_expired`), no RLS on device rows, the body never logged;
and the app deletes an account's push rows on "sign out everywhere", deactivation and
erasure.

### `eifi1_server_kit.tours`

Tour progress on the account (0.15, tours contract §10): ui-kit's `useTourProgress` with an
`account` loads and saves the record here, and the device's record becomes its cache. Layer 1
and layer 2 in one package; the routes are `eifi1_server_kit.tours.router` (the `fastapi`
extra), the fake `eifi1_server_kit.tours.testing`. The table (`tour_completions`: `user_id` FK
ON DELETE CASCADE, `tour_id varchar(64)`, `completed_at timestamptz NULL`, primary key
`(user_id, tour_id)`; no `company_id`) and its migration stay in the app.

| Area | Names |
|---|---|
| Wire shapes | `TourProgressResponse` `{completed: {[id]: ISO \| ""}}` (UTC; `""` is a completion with no date), `TourProgressPatch` `{completed?: {[id]: ISO \| ""}, forget?: [id]}` (`extra="forbid"`; an instant without a zone is a 422), `TourId`, `TourInstant`, `TOUR_ID_PATTERN`, `TOUR_PROGRESS_LIMIT` (500 per body and per account) |
| The merge | `merge_completions(stored, patch, *, now)` → `TourProgressMerge(upserts, deletes, answer)`: per tour the later completion wins, a future one is clamped to `now`, a dated one beats `""`, `forget` deletes ("Mark as not done"); `progress_response(rows)`; a naive stored value is read as UTC |
| Refusals | `TourProgressError` (422, `code`: `TOUR_PROGRESS_CONFLICT` `tour_progress_conflict` for an id in both lists, `TOUR_PROGRESS_FULL` `tour_progress_full`), `TourProgressErrorCode` — answered by `install_contract_error_handlers` |
| Port and service | `TourProgressStore` (`completions(user_id)` → `{tour_id: completed_at \| None}`, `write(user_id, upserts, deletes)`: flushes, never commits; a naive column writes `value.replace(tzinfo=None)`); `TourProgressService(store, *, now=utc_now)`: `progress`, `update` (one write, last, only when something changed), `export` → `[{tour_id, completed_at}]` under `TOUR_PROGRESS_EXPORT_KEY` (`tour_progress`) in the account export |
| Routes (`tours.router`) | `TourProgressDeps(store=, current_user_id=, guards=(), now=)`, `tour_progress_router(deps, *, prefix="/tours", tags=("tours",))`: `GET /tours/progress`, `PATCH /tours/progress`, both the whole record; the guards (the demo refusal, the limiter, CSRF) run first; the account is the session's own, no route takes a user id |
| Testing (`tours.testing`) | `InMemoryTourProgressStore(rows=None)` (`rows[user_id][tour_id]`; `reads`, and `writes` as `(user_id, upserts, deletes)`, so a test sees a refusal wrote nothing) |

```python
from eifi1_server_kit.tours.router import TourProgressDeps, tour_progress_router

app.include_router(
    tour_progress_router(
        TourProgressDeps(store=tour_progress_store, current_user_id=current_user_id, guards=(refuse_demo,))
    ),
    prefix="/api/v1",
)
install_contract_error_handlers(app)
```

The export's `completed_at` is spelled as the routes answer it, `2026-10-18T07:00:00Z`
(0.16.0; 0.15.0 wrote `isoformat()`'s `+00:00`). **A dead-code check** (vulture, as in
Kurvenschmiede) sees the store's `completions` called only by the kit's service through the
port: baseline it, as the feedback and translation-review store methods already are.

### `eifi1_server_kit.sync`

Local-first sync, the server half (0.15, local-first contract §1.5 D1, D2, D5 and §1.6):
keksdose's `/sync` protocol, shared with kastlan. The routes, the entity registries, tenancy
and RLS, E2EE, billing's refusals, unique-key merges and revival rules, and each scope's own
rules (its guards) stay in the app. Everything but `sync.mixin` is pure; the mixin needs the **`sync` extra**
(`eifi1-server-kit[sync]`, `sqlalchemy>=2.0`) and is imported lazily, so `eifi1_server_kit.sync`
loads without it.

| Area | Names |
|---|---|
| Wire shapes | `SyncChange` (`entity`, `id`: a UUID or text of 1–200 characters, `op`, `data`, the device's edit time `updated_at`, `knowledge`), `SyncRequest` (`scope`, `last_knowledge`, `changes`, `refusals`, `paged`, `page_cursor`, (0.16.0) `client_now`: the device's clock when it sends the push, spelt as `updated_at`), `SyncResponse` (`server_knowledge`, `changes`, `refused`, the per-change `rejections`, `page_cursor`), `SyncRefusal` (the WHOLE push; moved from billing, which re-exports it), `SyncRejection(change_id, code, detail)` with `SYNC_REJECTED` / `SYNC_PLAN_LIMIT`, `SyncPageCursor`; subclass them (no model forbids extra fields; `id` and `changes` narrow covariantly). A request without `refusals` or a change without `updated_at` still parses, for one release |
| The merge | `merge_lww(existing_updated, incoming_updated, *, now, skew=timedelta(0))` (whole-row LWW on the device's edit time; equal keeps the incoming row; a row without a stamp loses), `merge_delete(…)` (always tombstones, stamped now), `clamp_stamp(stamp, *, now, skew=timedelta(0))` (the stamp shifted by `skew`, then none or a future stamp → now), `as_utc` (a naive value read as UTC); (0.16.0) the device clock's skew, `clock_skew(client_now, *, now)` once per push (`now - client_now`: a SLOW clock's edits no longer lose to a colleague's later-arriving older edit, a FAST clock's no longer win; `timedelta(0)` without `client_now`, i.e. 0.15.0's clamp alone, or within `CLOCK_SKEW_TOLERANCE` (2 s, latency); capped at `MAX_CLOCK_SKEW` (24 h), past which the clock likely changed between edit and push) and `correct_stamp(stamp, *, skew)` (saturates at the calendar's edge). It assumes the device's clock did not change between the edit and the push: `skew = clock_skew(request.client_now, now=now)`, then `merge_lww(row.updated_at, change.updated_at, now=now, skew=skew)` and store `clamp_stamp(change.updated_at, now=now, skew=skew)` |
| The counter | `KnowledgeCounter` (`bump(session, tenant_id)` → the next value under a row lock, synchronous, inside `before_flush`; `None`: unstamped), `TenantOf` |
| The paged pull | `begin_page`, `PageStart` (`.resume_in(index)`), `cut_page`, `page_of` (the walk over rows in memory), `cursor_at_start`, `page_order`, `is_after`, `SYNC_PAGE_LIMIT` (2000): ordered `(knowledge, id)` per entity, entities in the app's order, the window pinned at `window_end` |
| SQLAlchemy (`sync.mixin`, the `sync` extra) | `SyncColumns` (`knowledge`, `deleted_at`, a client-owned `updated_at`); `install_sync_stamping(session_cls, *, counter, tenant_of, is_synced, now=)` (one bump per tenant per flush, so rows of one flush share a knowledge: a pull orders within it itself, entities in the app's order; a model without `updated_at` gets its knowledge only; naive columns get naive UTC); `install_tombstone_filter(session_cls, *, synced)` and `INCLUDE_TOMBSTONED` (the pull's opt-out); `tombstone(obj, *, now=)`, `bulk_tombstone(model, *where, knowledge, now)`, `bulk_restamp(…)` |
| Scopes (0.16.0, §2.4) | `SyncScope(name, roles, readable, writable={}, withheld=frozenset(), after_apply=None)` (`withheld` holds `"<entity>.<field>"`, never pulled and never written, D14; `.withheld_fields(entity)`; `.pull_order(walk)`: the readable entities in the app's walk order, for the paged pull's `entities`, a `ValueError` when the walk leaves one out); `EntityRule(model, create, update, deletable=False, fixed_parents=(), fixed_fields=(), guard=None)` (`create=None`: not created through sync; a fixed parent or field may be re-sent unchanged; `guard(RuleContext)` → a reason refuses); the port `ScopeRows` (`find(entity, row_id)` in the request's company, tombstones included; `parent_id(row, wire_key)`; `references`: wire key → entity); `async sort_changes(scope, changes, rows)` → `SortedChanges(allowed, rejections, context)` (per change: writable, deletable, no withheld field, the create/update schema, references in the company or new in the push, fixed parents and fields, the guard; a child of a refused NEW row is refused with it, "its parent was refused"; `.refuse_whole()` raises the first as `SyncScopeError(change_id, detail)` for a client without `refusals`); `AfterApplyContext(scope, rows, changes, found, before)` for `after_apply` (`found` holds the LIVE rows, which the apply has mutated; `before[id]` is the snapshot of the fields the change sends, as they stood before the apply: compare against it in the row's type, `date.fromisoformat(data[k]) != before[id][k]`, as the wire is JSON; two changes to one row each see the row before the push); `withhold(scope, change)` strips the withheld fields from a pulled `SyncChange` or dict |
| Testing (`sync.testing`) | `InMemoryKnowledgeCounter(*, per_tenant=True, start=0)` (`calls` as `(tenant_id, autoflush)`, `current(tenant_id)`; `per_tenant=False` is kastlan's one database-wide row) |

**The lock is load-bearing**: `bump` reads and writes one row, so an implementation takes it
with `SELECT … FOR UPDATE` (keksdose mutates its `households.knowledge` row in memory) or one
atomic `INSERT … ON CONFLICT DO UPDATE … RETURNING` (kastlan); two flushes that both read N
would hide one device's rows from the other.

**A scope in the app's router** (0.16.0): the app's `ScopeRows` holds its session, so a guard
or `after_apply` reads it from the context's `rows`. The router stays the app's (roles,
billing, the demo, the company pin):

```python
scope = SCOPES[payload.scope]
rows = CompanyRows(session, company_id)  # the app's ScopeRows
changes = [change.model_dump() for change in payload.changes]
sorted_changes = await sort_changes(scope, changes, rows)
if not payload.refusals:
    sorted_changes.refuse_whole()  # an older client: the first refusal, a 422, nothing written
await apply_incoming(session, sorted_changes.allowed)
if scope.after_apply is not None:
    await scope.after_apply(sorted_changes.context)
page, cursor = page_of(
    rows_by_entity,
    entities=scope.pull_order(ENTITY_ORDER),
    cursor=payload.page_cursor,
    last_knowledge=payload.last_knowledge,
    server_knowledge=server_knowledge,
)
pulled = [withhold(scope, serialize(entity, row)) for entity, row in page]
return SyncResponse(
    server_knowledge=server_knowledge,
    changes=pulled,
    rejections=sorted_changes.rejections,
    page_cursor=cursor,
)
```

## Installing it in an app

Apps depend on a **published** version — the wheel attached to a tagged GitHub Release, whose hash `uv.lock` pins —
never on a path outside their repository (a build must not need anything beside it):

```sh
uv add "eifi1-server-kit @ https://github.com/Eifi1/server-kit/releases/download/v0.20.0/eifi1_server_kit-0.20.0-py3-none-any.whl"
```

With the image guard, the Resend client, the Paddle client, a router or the sync stamping (`[sync]`, 0.15), name the extra: `"eifi1-server-kit[billing,fastapi,images,mail] @ https://…/eifi1_server_kit-<version>-py3-none-any.whl"` (kastlan `[billing,fastapi,images,mail]`, keksdose `[billing,fastapi,images]`, Kurvenschmiede `[billing,fastapi,mail]`).

The RELEASE WHEEL, not a `git+https` source: slim images (`python:3.14-slim`) have no git
binary, so uv cannot fetch a git source inside a Docker build (keksdose's finding). Each
tag `v*` runs `.github/workflows/release.yml`, which builds the wheel with the full gate and
attaches it to the GitHub Release; uv.lock pins its hash. To try a change locally without
touching an app's `pyproject.toml` or `uv.lock`, run its suite with the kit overlaid:
`uv run --with-editable ../server-kit pytest`. Editable, not `--with ../server-kit`: uv
caches the wheel it builds from a path until the kit's `pyproject.toml` changes, so an edit
made after the first run would not be what gets tested (Kurvenschmiede's S6).

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

On FastAPI it also answers every validation 422 without the request's values (0.17.0,
[above](#eifi1_server_kiterrors)) unless the app has its own `RequestValidationError` handler.

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

## Adopting layer 2

The feedback routes become the kit's (server-services §4.4). Each app writes two adapters
over its own session and store, one actor dependency and its demo guard factory, then
includes the kit's router where its own was; its models, migrations (no schema change),
guards and extra routes stay:

```python
from eifi1_server_kit.errors import install_contract_error_handlers
from eifi1_server_kit.feedback.router import FeedbackDeps, feedback_router
from eifi1_server_kit.limiter import FeedbackLimiters

router = feedback_router(
    FeedbackDeps(
        actor=feedback_actor,  # User -> FeedbackActor (is_admin, is_demo, display_name)
        repository=feedback_repository,  # FeedbackRepository on the request's session
        storage=feedback_storage,  # AttachmentStorage over the app's store
        demo_guard=not_in_the_demo,  # the app's factory around refuse_demo
        admin_guard=require_role(UserRole.ADMIN),  # the app's own guard and 403
        policy=FEEDBACK_ATTACHMENTS,  # == FeedbackCreate.attachment_url_policy
        create_model=FeedbackCreate,
    )
)
app.include_router(router, prefix="/api/v1")  # the app's own extra routes after it
app.state.feedback_limiters = FeedbackLimiters()  # in create_app
install_contract_error_handlers(app)
```

Run `feedback.testing`'s `check_repository` and `check_storage` once against the adapters
on the test database, and `check_round_trip` over the app's client; keep a wiring test per
route and the app's own variations. Then, per app:

- **keksdose** (`[billing,fastapi,images]`): the repository's `get` ignores the viewer
  (`session.get`), `add` and `apply` flush, `urls_named_except` is `id NOT IN`; the
  repository dependency takes `Depends(db_session)`, the session `commit_boundary`
  commits. `decode_images=True`. The support door charges the same window through
  `throttle_upload(request.app.state.feedback_limiters.upload, user.id)`.
  `_anonymise_feedback` calls `erase_author`, prefixes `feedback/` and hands the keys to
  its after-commit purge. Visible: the identity stamp; `Content-Disposition` on every
  download.
- **kastlan** (`[billing,fastapi,images,mail]`): the repository commits after a flush only
  when its `commit` flag is on, and only the router's dependency sets it — the erasure
  builds it with the flag off and stays one commit. `foreign_row="forbidden"`,
  `decode_images=True`; the admin guard is `require_role(*ADMIN_ONLY)`. Its `GET
  /feedback/counts` and `GET /feedback/{feedback_id}` are included after the kit's
  router. On the wire: no `Location`, no `user_name`, the address in the inbox only.
- **Kurvenschmiede** (`[billing,fastapi,mail]`): `get` is `visible_by_id` for the viewer,
  `add` claims the named uploads, `apply` hangs a rework's files as the actor's, clears
  the attachment rows when the erasure nulls the URL columns and writes through
  `save(session, row, actor=…)`; the storage keeps its sweep and HMAC.
  `foreign_row="not_found"`, `require_utf8_text=True`; `{row_id}` becomes
  `{feedback_id}`. Its feedback loop files through `FeedbackService(…).create(first_admin,
  payload)`, and the erasure ignores the keys (files go by report).

The 429's sentence becomes keksdose's (`THROTTLED_DETAIL`) in kastlan and Kurvenschmiede;
the inbox's 403 stays each app's own guard's.

## Developing

```sh
uv sync
bash scripts/check.sh      # ruff, ruff format --check, mypy --strict, pytest + coverage, uv build, the API export
```

CI (`.github/workflows/ci.yml`) runs the same script on Python 3.14.

**Dependencies before a push** (0.13.1, as keksdose's): enable the hook once per clone with
`git config core.hooksPath .githooks`. `.githooks/pre-push` runs
`scripts/check-dependencies.py`: an open Dependabot alert blocks the push, and newer
minor/patch versions of the direct dependencies are listed without blocking. Dependabot no
longer opens uv version-update PRs; GitHub Actions keep theirs. `SKIP_DEPENDENCY_CHECK=1`
skips it.

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
