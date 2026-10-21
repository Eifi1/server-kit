# Changelog

All notable changes to `eifi1-server-kit` — the backend sibling of `@eifi1/ui-kit`.

The contract until 1.0 is the kit's: a **minor** (`0.x.0`) may remove or rename a public
name or change a rule's behaviour, and says so under **⚠ BREAKING CHANGES**; a **patch**
(`0.x.y`) fixes behaviour without changing the surface. Apps pin an exact tag (its commit
hash lands in their `uv.lock`), so nothing reaches an app until it asks for it. Write the
entry in the Conventional Commit; this file is assembled from them at release.

## [Unreleased]

## [0.18.0] (2026-10-21)

server-kit 0.18.0: the backend half of round 0.47 (ui-kit 0.47), from the 0.46 build reviews
(keksdose N12/N13, S4) and keksdose live #447 (feedback §12). A **minor** that **breaks**
(under **⚠ BREAKING CHANGES**): a sent `timezone` is refused, as 0.16 and 0.17 announced, and
an app with public comments adds one nullable column. Per-app steps: ui-kit's
`docs/adopt-0.47.md`, the server-kit 0.18 section.

### ⚠ BREAKING CHANGES

* **notifications, auth:** a sent `timezone` is a 422, as announced (ignored in 0.16 and
  0.17): on `PATCH /notifications/settings`, keksdose's `/push/settings` shim and
  `PATCH /auth/me`, as any unknown field. Only a stale ≤0.45 bundle still sends one.
* **feedback:** `FeedbackCommentRow` requires `attachment_url`: an app with comments adds a
  nullable `VARCHAR(500)` column. `check_comment_store` writes a comment with a file.

### Added

* **notifications:** `documented_body(model)`, the `openapi_extra` that puts an untyped
  `Annotated[Any, Body()]` push body back into OpenAPI from its model (nested models inlined).
* **feedback:** an attachment on an admin reply and on a public comment (§12): one
  `attachment_url` on `FeedbackReplyCreate` and `FeedbackCommentCreate`, answered on
  `FeedbackCommentResponse`; `FeedbackDeps.reply_model` / `comment_model` to narrow
  `attachment_url_policy` in a subclass. A comment's file is shown to whoever reads its text
  and purged with an erased author's comment.
* **feedback:** `delete_comment` answers the keys of the deleted comment's file that nothing
  else names and hands them to a comment store that is a `FeedbackCommentFilePurge`
  (`purge_files(keys)`, before the commit) — keep or purge, the app decides.
* **feedback:** `check_repository(…, replies=, comments=)` checks that `urls_named_except`
  names every entry's and comment's file, on the excepted reports too (keksdose S4).

### Docs

* **notifications:** a push route's two 422 shapes: `push_body_invalid` from
  `parse_push_body` / `parse_push_replace`, and FastAPI's redacted `json_invalid` for a body
  that isn't JSON; neither echoes the body.

## [0.17.0] (2026-10-20)

server-kit 0.17.0: the apps' 0.46 adoption findings (keksdose, kastlan, Kurvenschmiede). A
**minor** only because it adds public names (`parse_push_body`, `parse_push_replace`,
`PushBodyInvalidError`, …), which the policy reserves for a minor — **nothing breaking**: no
name removed or renamed, and an app needs no code change to take it. A stale `timezone` is
still accepted and ignored in 0.17 (refused from 0.18, not 0.17 as 0.16.0 said). One
behaviour an app's tests may notice: `PATCH /auth/me` with an unknown device zone now answers
200 (the zone dropped) where 0.16.0 answered a 422 — re-pin such a test (ui-kit's
`docs/adopt-0.46.md`, the server-kit 0.17.0 rows).

### Fixed

* **errors:** no validation 422 echoes what was sent. FastAPI's default answer repeats each
  error's `input` (and `ctx`, `url`), so passwords and tokens (login, e-mail change, deletion,
  confirm) reached the answer and the logs. On a FastAPI app `install_contract_error_handlers`
  now answers 422s as `{"detail": [{"type", "loc", "msg"}, ...]}` only;
  `redact_validation_errors=False` opts out, and an app's own `RequestValidationError`
  handler wins.
* **notifications:** push bodies are parsed without echoing secrets (keksdose S1):
  `parse_push_body(model, body)` and `parse_push_replace(body)` validate an untyped body and
  raise `PushBodyInvalidError` (422 `push_body_invalid`, the failing fields named, no value, no
  chained `ValidationError`), so neither `old_auth` nor a key reaches the answer or a traceback.
* **auth:** an unknown or malformed device zone is dropped on `PATCH /auth/me`
  (`ProfileUpdate`), as `RegisterRequest` does, so a browser with newer tzdata keeps saving the
  rest (Kurvenschmiede S2).
* **user_admin:** `AdminUserRow` no longer inherits `device_timezone`: a person's zone never
  reaches the admin roster (Kurvenschmiede S1).

### Added

* **notifications:** `InboxItem.company_id` (kastlan N7); `FlushPort.still_wanted` may take a
  keyword `now=`, and the flush passes its own `now` to a port that does (keksdose N8; a port
  without it is called as before).
* **errors:** `validation_error_response`, the redacting 422 handler the installer registers.

### Docs

* **notifications:** the unsubscribe key takes a dedicated secret (keksdose S2).
* **feedback:** the alerts docstring says kastlan awaits the alert inside `filed`.

## [0.16.0] (2026-10-19)

server-kit 0.16.0: the backend half of round 0.46 (ui-kit 0.46), from the 0.45 build reviews
(kastlan, keksdose, Kurvenschmiede) and the D2 amendment of local-first sync
(`docs/local-first-harmonization.md`), plus notifications §10 and feedback §11 (settled
2026-10-19, the zone amended 2026-10-20) and local-first §2's named sync scopes. A **minor**,
not the 0.15.1 it was prepared as: it adds public names, and it **breaks** three things an app
must handle in the same bump (under **⚠ BREAKING CHANGES**): the legacy rework append is a
coded 422 and its names are gone, push becomes a channel of its own, and the notification
settings lose their picked `timezone` (the zone is automatic). The behaviour changes an app
may notice without code are under **Changed**. Per-app steps: ui-kit's `docs/adopt-0.46.md`.

### ⚠ BREAKING CHANGES

* **feedback:** the legacy rework append is a coded 422 (feedback contract §10.3; Marcel
  2026-10-19, kastlan and Kurvenschmiede agreed). A body-only PATCH that appends to a report
  outside OPEN / READY / IN_PROGRESS — the rework of a client from before ui-kit 0.40, which
  0.13–0.15 converted into an entry — is refused before anything is written:
  `LegacyReworkAppendError` (a `FeedbackValidationError`), answered `{"detail": "This page is
  out of date: reload it, then send the report back again.", "code": "legacy_rework_append"}`
  (`LEGACY_REWORK_APPEND`, `LEGACY_REWORK_APPEND_DETAIL`). Only a tab open across the deploy
  still sends it. Removed: `legacy_rework_block`, `UpdatePlan.rework`, `UpdatePlan.reopened`,
  `plan_update(prefix=)` and the `feedback.legacy_rework_append` log line. An app test that
  PATCHes an append to an answered report expects the 422 now.
* **notifications:** push is a channel of its own (notifications §10.2). `plan_delivery`
  sends the push on `switches.push`, no longer on `app`; `resolve_switches` answers `push`
  (a type's `push=None` takes its `app` default in a registry with push, `null` in one built
  with `push_channel=False`). A `TypeSwitches` built by hand without `push` sends no push.
  **keksdose migrates first** (`app = push = enabled`, `migrate_enabled_to_channels`), or a
  type switched off would push again by its default. kastlan and Kurvenschmiede build their
  registry with `push_channel=False` (else kastlan's `app=False` types read `push: false`).
  `NotificationType`'s repr and the settings' JSON carry `push` (and `bundle`).
* **notifications, auth:** the zone is automatic only (notifications §10.3, amended
  2026-10-20): the account's zone is the device last used, else Zurich, and nobody picks one.
  `QuietHoursFields.timezone` and `QuietHoursUpdate.timezone` are removed, so the settings'
  answer carries no zone; `UserResponse` / `ProfileUpdate` have no `timezone` either. A
  `timezone` sent anyway — a stale 0.45 bundle's picker or its quiet hours' first entry, to
  `PATCH /notifications/settings`, keksdose's `/push/settings` shim (through
  `QuietHoursUpdate`) or `PATCH /auth/me` — is **ignored in 0.16 and 0.17** (dropped before the checks,
  never written) **and refused from 0.18** as any unknown field (`extra="forbid"`, a 422): the
  PWA keeps old bundles for days (keksdose S2/S3). Quiet hours read `effective_zone(user.device_timezone)` (below); an app moves its stored
  settings zone to `users.device_timezone` in the bump's migration.

### Added

* **notifications (round 0.46, §10):** `NotificationType(push=, bundle=)`,
  `NotificationTypes(push_channel=)` and `.push_channel`; `TypeSwitches.push`,
  `TypeSwitchesPatch.push`, `StoredSwitches.push`; `apply_switches_patch(couple_push_to_app=)`
  (keksdose's `{app}`-only bundles, with the `/push/*` aliases); `migrate_enabled_to_channels`;
  `NotificationChannelError` (422 `notification_channel_not_offered`, also on `{push}` where
  the app has none); per device `PushDeviceUpdate(muted_types)`,
  `PushDeviceResponse.muted_types`, `device_receives`, `read_muted_types`, `apply_muted_types`;
  `PushData.inbox` (`false`: no inbox row, the worker shows it even while focused); the zone on
  the account, automatic only: `effective_zone(device_timezone)`, `local_today(zone, now)`,
  `quiet_hours_end(start, end, zone, now)` (across midnight, both DST days); the inbox's port
  `InboxStore`, `NewInboxItem`, `InboxItem.company`, `INBOX_PAGE_DEFAULT` / `INBOX_PAGE_MAX`;
  the opt-in queue `NotificationQueue`, `NewQueueItem`, `QueueItem`, `QueueChannel`,
  `FlushPort`, `QueueService` (`hold_push`, `queue_mail`, `flush`, `purge`), `FlushResult`,
  `notifications_tick`, `QUEUE_*`; layer 2 `notifications.router` (the `fastapi` extra:
  `InboxDeps`, `inbox_router`, `notifications_jobs_router`) and `notifications.testing`
  (`InMemoryInboxStore`, `InMemoryNotificationQueue`, `RecordingFlushPort`).
* **notifications (§10.6):** the push replace route, `POST /notifications/devices/replace`, for
  a subscription the browser rotated (`pushsubscriptionchange` runs in the service worker,
  without a session): `PushReplaceIn` (the old endpoint plus its `auth` secret as the proof,
  and the new subscription) and `push_auth_matches(stored, given)` (a constant-time compare);
  the app mounts the route. Its rules: `PUSH_GONE_GRACE` (7 days) and
  `push_gone_expired(gone_at, now)`; `PushReplaceIn`'s docstring holds the settled rules (an
  inactive or demo account the same 404 and its row deleted, the proof before the 409, a gone
  row claimable within the grace, no RLS on device rows, the body never logged, push rows
  deleted on sign-out-everywhere, deactivation and erasure).
* **auth (notifications §10.3, amended 2026-10-20: automatic only):**
  `UserResponse.device_timezone`, `ProfileUpdate.device_timezone` (the device's report, never
  `null`, in `PROFILE_NOT_NULLABLE`), `RegisterRequest.device_timezone` (an unknown zone
  dropped), `ZoneName`, `MAX_ZONE_LENGTH`, `PROFILE_ZONE_FIELDS` (`("device_timezone",)`),
  `apply_zone_update(user, patch, *, is_demo)` (a demo's zone ignored). The account's zone is
  the device last used, else Zurich; there is no pick (see **⚠ BREAKING CHANGES**).
* **feedback (§11, the admins' alert):** `FEEDBACK_RECEIVED`, `FeedbackAlert`,
  `FeedbackAlertKind`, the `FeedbackAlerts` port, `admin_alert_recipients(admin_ids, *,
  actor_id)`, `mails_at_once(alert)`; `FeedbackService(alerts=None)` tells it after `create`,
  after a NEW crash row (never a fold) and after a rework's `apply` that lands OPEN (an
  admin's rework, a reply, a status change, a comment, a vote and `alerts=None` tell nobody);
  `FeedbackDeps(alerts=)`; testing `RecordingAlerts`.

* **sync:** each push's device clock skew is corrected (D2 amended): `SyncRequest.client_now`
  (the device's clock when it sends, spelt as `updated_at`), `clock_skew(client_now, *, now)`
  once per push (`now - client_now`; `timedelta(0)` without `client_now` or within
  `CLOCK_SKEW_TOLERANCE`, 2 s; capped at `MAX_CLOCK_SKEW`, 24 h), `correct_stamp(stamp, *,
  skew)`, and an optional `skew=` on `merge_lww` and `clamp_stamp`. A SLOW clock's edits no
  longer lose to a colleague's later-arriving older edit; without `client_now`, 0.15.0's
  behaviour.
* **sync:** named scopes, `sync.scopes` (local-first §2.4; lifted from kastlan's
  `sync_scopes.py`, its handover rules left to the app as guards): `SyncScope` (`name`, `roles`,
  `readable`, `writable`, `withheld`, `after_apply`; `pull_order(walk)` for the paged pull's
  `entities`), `EntityRule` (`create`/`update` schemas, `deletable`, `fixed_parents`,
  `fixed_fields`, `guard`), the `ScopeRows` port (`find`, `parent_id`, `references`),
  `RuleContext`, `AfterApplyContext` (`found`: the live rows, mutated by the apply; `before`: a
  snapshot of the fields each allowed change sends, taken before the apply — kastlan S5),
  `async sort_changes(scope, changes, rows)` →
  `SortedChanges(allowed, rejections, context)` (one rejection per refused change; a child of a
  refused new row is refused with it; `refuse_whole()` for a client without `refusals`),
  `SyncScopeError`, and `withhold(scope, change)`: a scope's `withheld` `"<entity>.<field>"`
  never reaches a device and a push that writes one is rejected (D14; kastlan:
  `estates.qr_iban`). Layer 1, no extra.
* **mail:** `HtmlMailMessage`, a `MailMessage` whose `html` is a `str` (keksdose N2).
* **user_admin:** `group_name_problem(name)` → `GroupNameProblem` (`blank` / `too_long` /
  `control_character` / `secret_shaped`) or `None`, for the app's 422 on a new bad name;
  `DETAIL_NAME_MAX_LENGTH` (200); `audit_detail(detail, *, action=)`, which
  `admin_action_record` passes its action.

### Changed

* **tours:** the export spells `completed_at` with `Z`, as the wire does (was `isoformat()`'s
  `+00:00`; Kurvenschmiede). **A test that pins the export's value must be re-pinned.**
* **mail:** `test_mail_message` returns `HtmlMailMessage` (still a `MailMessage`).
* **notifications:** a push service's 404/410 marks the device gone (`gone_at`) instead of
  deleting it at once (§10.6 rule 4; the app's reaper); the daily reap deletes it after
  `PUSH_GONE_GRACE`. `in_quiet_hours`' held push waits for `quiet_hours_end` where the app
  runs the queue.

### Fixed — from the 0.45 build reviews

* **user_admin:** a company's `group_rename` records its `{from, to}` names, which 0.15.0
  documented but `audit_detail` refused (a 500; kastlan K8). The names are cleaned, never
  refused: control and format characters out, whitespace collapsed, cut at 200 with an
  ellipsis, a blank or secret-shaped name dropped (kastlan S1). Every other detail stays tokens.

### CI

* the shared commit check (`Eifi1/shared-workflows` v1.3.0) over the PR or push range; the
  commit hooks follow the shared rules as of shared-workflows b4582d3 (v1.3.1, pending tag).

### Docs

* **sync:** the rows of one flush share a knowledge — take it with `SELECT … FOR UPDATE` or one
  atomic `INSERT … ON CONFLICT DO UPDATE … RETURNING` (kastlan K5); **tours:** the export's
  spelling and the vulture note (Kurvenschmiede).

## [0.15.0] (2026-10-19)

server-kit 0.15.0: the backend half of round 0.45 (ui-kit 0.45), settled on 2026-10-19 after
the three app reviews — the admin area's §9 (the test mail, the attention counts, a
company's switch and rename; `docs/admin-harmonization.md`), tour progress on the account
(`docs/tours-harmonization.md` §10) and the server half of local-first sync
(`docs/local-first-harmonization.md` §1).

### ⚠ BREAKING CHANGES

* **feedback:** `FeedbackRepository` gains `async count_by_status()`: every app repository must
  add it (one `GROUP BY status` in the request's scope, as `inbox`), and `check_repository`
  checks it.

### Added

* **tours:** tour progress on the account (§10). Layer 1: `TourProgressResponse`
  `{completed: {[id]: ISO | ""}}`, `TourProgressPatch` `{completed?, forget?}`,
  `TOUR_ID_PATTERN`, `TOUR_PROGRESS_LIMIT` (500), `merge_completions` (per tour the later
  completion wins; a future one is clamped to now) → `TourProgressMerge`, `progress_response`,
  and `TourProgressError` (422 `tour_progress_conflict`, `tour_progress_full`, answered by
  `install_contract_error_handlers`). Layer 2: the `TourProgressStore` port over the app's
  `tour_completions` table, `TourProgressService` (`progress`, `update`, `export` under
  `TOUR_PROGRESS_EXPORT_KEY`), `tours.router.tour_progress_router(TourProgressDeps(store=,
  current_user_id=, guards=, now=))` for `GET` / `PATCH /tours/progress` (the `fastapi`
  extra), and the fake `tours.testing.InMemoryTourProgressStore`.
* **feedback:** `feedback_attention(counts)` → `FeedbackAttention(to_triage, awaiting_verdict,
  released_for_work)`, the admin's three feedback tiles from `count_by_status()` and the
  kit's own status sets (§9.5); `InMemoryFeedbackRepository.count_by_status`.
* **mail:** the admin's test mail (§9.2): `test_mail_message(locale, *, app_name, backend,
  sent_at)` in seven languages, naming the app, the transport and the time, never the
  recipient; `MAIL_TEST_KIND` (`mail_test`), `MAIL_TEST_LIMIT` (3 per 10 minutes),
  `MailTestResult` `{sent, mail_backend}`. No router: the app's route awaits its own send.
* **user_admin:** `AdminAction.GROUP_DEACTIVATE` and `GROUP_REACTIVATE` (kastlan's operator
  switching a company off and on, §9.1.3), both at `none`; a company's `group_rename` logs
  `{from, to}` (its name is business data), a team's still logs `{}`.
* **sync:** local-first sync, the server half (§1.5, §1.6): keksdose's `/sync` shapes
  (`SyncChange` with the device's `updated_at`, `SyncRequest` with `refusals` and `paged`,
  `SyncResponse` with the new per-change `rejections: [SyncRejection]`, `SyncPageCursor`,
  `SYNC_REJECTED`, `SYNC_PLAN_LIMIT`); the merge rule (`merge_lww`: the latest edit time wins,
  `clamp_stamp` clamps a future stamp; `merge_delete`: a delete always tombstones; `as_utc`);
  the `KnowledgeCounter` port and `TenantOf`; the paged pull (`begin_page`, `PageStart`,
  `cut_page`, `page_of`, `cursor_at_start`, `page_order`, `is_after`, `SYNC_PAGE_LIMIT`); and,
  behind the new **`sync` extra** (`sqlalchemy>=2.0`, imported lazily), `SyncColumns`,
  `install_sync_stamping`, `install_tombstone_filter`, `INCLUDE_TOMBSTONED`, `tombstone`,
  `bulk_tombstone`, `bulk_restamp`. `sync.testing.InMemoryKnowledgeCounter` for the apps'
  tests.

### Changed

* **billing:** `SyncRefusal` moved to `eifi1_server_kit.sync`; `billing` re-exports the same
  class, so nothing changes for an app.
* **ci:** the pre-push hook checks every pushed commit's message against the shared rules
  (`Eifi1/shared-workflows` commit-check v1.3.0, vendored as `scripts/check-commit-msg.cjs` and
  kept in step by `scripts/sync-commit-rules.py`); without node it says so and skips.

### Fixed — from the 0.45 build reviews

* **sync:** the stamping hook stamps a synced model without `updated_at` with its knowledge
  only, and writes naive UTC into a naive timestamp column (asyncpg refuses an aware value
  there); `SyncChange.id`'s text arm is 1–200 characters (kastlan, Kurvenschmiede, keksdose).
* **tours:** a naive stored `completed_at` is read as UTC (a store over `timestamp without time
  zone`).

## [0.14.0] (2026-10-17)

Three contracts settled on 2026-10-17 after the three app reviews, released with ui-kit 0.42:
billing §16 (the portal for the buyer only, an in-app cancel), the admin area
(docs/admin-harmonization.md in ui-kit) and the top bar's dev switchers
(docs/top-bar-harmonization.md). Nothing is removed; every new field has a default.

### Added

* **billing:** `require_portal_access(row, *, viewer_ref)` answers 403 `billing_portal_buyer_only`
  where a buyer is stored and the viewer isn't them (`portal_access`, `PortalAccess`).
  `BillingOverview` gains `portal` (`"open"` | `"buyer_only"`) and `buyer_name`, from
  `from_row(…, viewer_is_buyer=, buyer_name=)`; a person-payer app passes neither and stays
  `"open"`.
* **billing:** `BillingStanding` (`GET /billing/status`'s standing, as kastlan had its own) with
  the same `portal` and `buyer_name`, so the payment-failed banner can name the buyer.
* **billing:** `cancel_at_period_end(row, client)` and `resume_scheduled_cancel(row, client)`, the
  in-app cancel and its undo for every admin who reads the overview (`POST /billing/cancel`,
  `POST /billing/resume`, 204; no write lock; optional for a person-payer app).
* **user_admin:** `AdminAttention` (`GET /admin/attention`): what waits on the admin, every count
  passed in by the app, `None` where the app has no such thing; demo accounts never count, a demo
  session answers all `None` (`AdminAttention.for_demo`).
* **dev_switch:** the dev-only role and plan switch (`POST /dev/switch`). `dev_switch_enabled`
  needs an explicit off-by-default flag AND the app's proof of a local run AND not production —
  never an environment name; `assert_dev_switch_safe` refuses to start with the flag set in
  production; `require_dev_switch` answers 404 when shut; `validate_dev_switch` 422s an unknown
  role or plan.

## [0.13.1] (2026-10-16)

The apps' 0.40 adoption findings, released as one batch with ui-kit 0.40.1 once all three had
reported.

### Fixed

* **feedback:** `check_reply_store` writes its rework entry's file under the app's own
  attachment policy (`policy=`) or a file the app stored (`attachment_url=`), so a store that
  resolves the file accepts it (Kurvenschmiede).

### Changed

* **ci:** Dependabot's uv version updates run locally before a push (`.githooks/pre-push`,
  `scripts/check-dependencies.py`): an open alert blocks, newer minor/patch versions are
  listed; Dependabot keeps the GitHub Actions and no longer rebases open PRs.

## [0.13.0] (2026-10-16)

server-kit 0.13.0: the backend half of round 0.40 (ui-kit 0.40) — feedback §10 settled after
the three apps' reviews (a rework is the author's entry in the conversation, with its own
REWORK status), and the Paddle sandbox run's results for billing.

### ⚠ BREAKING CHANGES

* **feedback:** `FeedbackReplyRow` requires `kind` and `attachment_url`; `rework_status(actor_is_admin=True)`
  answers REWORK (was READY); a legacy PATCH append on an answered report becomes an entry
  instead of writing `body` (for one release); `urls_named_except` must include rework-entry
  files; `REWORKABLE_STATUSES` gains REWORK and the status order puts it third.

### Added

* **feedback:** `FeedbackStatus.REWORK`, `QUEUE_STATUSES`, `FeedbackEntryKind`, `plan_rework`,
  `FeedbackReworkCreate`, `FeedbackService.rework`, `POST /{feedback_id}/rework`,
  `split_rework_blocks` / `ReworkBlock` / `REWORK_BLOCK_RE` (body only); a rework fires no
  `feedback_answered`; erasure purges entry files.
* **billing:** `BillingOverview.switching`, computed by the now-public
  `provider_subscription_runs`; `PADDLE_CONFIGURATION_CODES`.

### Fixed

* **billing:** Paddle's setup refusals (e.g. `transaction_checkout_url_domain_is_not_approved`)
  answer 503 `billing_not_configured`, not 502 `billing_provider_unavailable`.
* **billing:** a switch preview's `next_minor` is the plan's price (`total`): Paddle's
  `grand_total` ignored the switch's own credit. Recorded sandbox answers are test fixtures (P5).

## [0.12.0] (2026-10-15)

server-kit 0.12.0: the backend half of round 0.39 (ui-kit 0.39) — the Paddle billing
amendment, `docs/billing-harmonization.md` §15, settled after the three apps' reviews. Each
app keeps its own pay host and any host serves any app's checkout; plan changes go through
the kits (a preview, then an immediate prorated switch); one Paddle customer per e-mail
address across the apps. Billing stays off until the sandbox run.

### ⚠ BREAKING CHANGES

* **billing:** `SubscriptionRow` requires `provider_price_id` (each app adds the column), and
  the provider port's three new methods must be implemented by any app-side client.
* **translation_review:** `can_review_kit` refuses every area-limited grant, as `scope()`
  does (decision T2); no app has such a grant.

### Added — billing §15 (round 0.39)

* **billing:** the port's `preview_plan_change`, `change_plan` (Paddle
  `PATCH /subscriptions/{id}[/preview]`, `prorated_immediately`) and `customer_for` (by e-mail,
  else created); `checkout_url(customer_email=)`; `require_plan_switch`, `switch_price` (422
  `billing_plan_not_sold`, 409 `billing_plan_unchanged`), `plan_switch_preview`,
  `PlanSwitchRequest`, `ProviderSwitchPreview`, `PlanSwitchPreview`, `PaidPrice`,
  `BillingOverview.paid`, `BUYER_KEY`, `NormalisedEvent.buyer`, the code
  `billing_not_subscribed`; `row_changes` writes `provider_price_id`; fakes and a
  `plan_switched` step. `billing_already_subscribed` no longer points at the portal.

## [0.11.1] (2026-10-14)

The apps' 0.38 adoption findings, released as one batch with ui-kit 0.38.1 once all three
had reported.

### Fixed

* **feedback:** `outcome` answers as the newest reply's body (§9.7), as the viewer reads
  it, so a client from before 0.38 shows the latest answer; the frozen column only while
  a report has no reply (kastlan).
* **feedback:** `create_reply` and `hide_comment` refuse a demo with `demo_refused` before
  the role's 403, as every other write does (Kurvenschmiede).

## [0.11.0] (2026-10-14)

server-kit 0.11.0: the backend half of round 0.38 (ui-kit 0.38) — Marcel's public feedback,
settled as `docs/feedback-harmonization.md` §9 after the three apps' reviews. A report is
readable by the app's other users its `public_viewer` admits, minus attachments, `context`,
the crash fingerprint and the author's address; crash reports stay private; a demo is not
signed in. Upvotes, comments by others as rows, and the admin's answers as a timeline of
replies. Kastlan keeps the public part off and takes the replies.

### ⚠ BREAKING CHANGES

* **feedback:** `outcome` is frozen: a PATCH naming it (null too) is a 422. An answer is a
  reply (`POST /feedback/{id}/replies`); `outcome` migrates into the first reply. Bump ui-kit
  0.38 with it.

### Added — public feedback (round 0.38, feedback §9)

* **feedback, layer 1:** `public_body` / `public_text` (every attachment URL stripped, anywhere),
  `is_public` (never a CRASH), `feedback_recipients`, `plan_reply` / `ReplyPlan`,
  `DECIDED_STATUSES`, the `public` lane in `plan_update`; the reply, comment, vote and public
  schemas; `FEEDBACK_ANSWERED`, `FEEDBACK_COMMENT`; `FeedbackConflictError`.
* **feedback, layer 2:** the ports `PublicFeedbackRepository`, `FeedbackReplyStore`,
  `FeedbackCommentStore`, `FeedbackVoteStore`, `PeopleLookup`; the service's reply, comment and
  vote flows and page views (one call per store); nine routes behind `FeedbackDeps.public_viewer`
  and the reply store; the erasure covers comments, votes and replies. `feedback.testing`:
  in-memory stores, `check_reply_store`, `check_comment_store`, `check_vote_store`,
  `check_public_repository`, `assert_public_view`.
* **auth:** `first_name_of(first, last)` — the first name, or None while the last is empty.
* **limiter:** `FEEDBACK_COMMENTS_PER_HOUR` (20), `FEEDBACK_VOTES_PER_HOUR` (60), on
  `FeedbackLimiters` as `.comment` and `.vote`.
* **mail:** `AccountNoticeKind.FEEDBACK_PUBLISHED`.
* `FeedbackActor.role`, `NewFeedback.public`, `ErasedFeedback.comments/votes/replies`.

## [0.10.0] (2026-10-14)

server-kit 0.10.0: the backend half of round 0.37 (ui-kit 0.37), two contract sections settled
after the three apps' reviews (Marcel, 2026-10-14: "Do 0.37 as recommended"). **The translation
review joins layer 2** (`docs/server-services-harmonization.md` §6): a service behind a store
port, its router factory and testing support, as the feedback service's — about 100, 140 and 115
lines out of keksdose, kastlan and Kurvenschmiede. **Notifications §9**: a notification mail a
job sends is owed until it went, for 3 days, with one `Idempotency-Key` per recipient, event and
day. Nothing is removed. Behaviour that changes: the four translation-review request bodies
refuse unknown fields (a 422, not a silent drop); `scope()` refuses a kit-only review token on
an area-limited grant; the area rule gains the `<area>:` arm.

### Added — the translation review, layer 2 (round 0.37, server services §6)

* **translation_review:** `ports` (`TranslationReviewer`, the app's grant on a view, keksdose's
  review token being the same view with `kit_only`; `TranslationReviewRow`, `ReviewerName`,
  `NewVerdict`, `TranslationReviewStore[RowT]` with `verdicts`, an upsert `put`, `clear`,
  `by_reviewer`), `service` (`TranslationReviewService`: `scope`, `list_reviews`, `save`,
  `clear`, `export` in keksdose's shape under `VERDICTS_EXPORT_KEY = "translation_verdicts"`:
  locale, string, text, verdict, suggestion, note, reviewed_at, oldest first), `router`
  (`translation_review_router(TranslationReviewDeps(…))`, the `fastapi` extra, keksdose's
  endpoint names, no route guards: the grant decides; the kit never commits) and `testing`
  (`InMemoryTranslationReviewStore`, `reviewer`, `check_store`, `check_round_trip`).
* **translation_review, layer 1:** the `<area>:` arm in `in_areas` and `area_like_patterns`
  (four patterns per area); `review_grant(…, is_demo=False)`; `TranslationReviewOut`'s
  `reviewer_first_name` / `reviewer_last_name`; `NO_REVIEW_ACCESS`, `NO_KIT_REVIEW_ACCESS`;
  `extra="forbid"` on `TranslationReviewWrite`, `TranslationReviewBatch`,
  `TranslationReviewKey` and `TranslationReviewClear`.
* **clock:** `Clock` and `utc_now`, shared by both layer-2 services; `feedback` still exports them.

### Added — notifications §9 (round 0.37)

* **notifications:** `retry` — `mail_due`, `mail_event`, `MailMarker(…).idempotency_key(day)`,
  `MAIL_LATE_DAYS` (3), `MAIL_MARKER_RETENTION` (30 days), `MAIL_EVENT_PATTERN`. With no inbox,
  the digest's marker is written once the send answered True.
* **mail:** `MailMessage.idempotency_key`, which `ResendClient` sends as the `Idempotency-Key` of
  both tries (a 409 is not sent); `AccountNoticeKind.WORK_TRANSFERRED_NOTICE`.

### Changed — from the apps' 0.36 adoptions

* **notifications:** `should_reap(failure_count, *, limit=PUSH_MAX_FAILURES)` (keksdose).
* **settings:** `apply_patch(None, …)` when every field of the model is skipped (kastlan).

### Fixed

* **user_admin:** the export check no longer flags `digest_cadence` ("cadence" is a fact word;
  keksdose).
* **feedback:** the module docstring's subclass example no longer shows kastlan's retired
  `user_name`.

## [0.9.0] (2026-10-13)

server-kit 0.9.0: the backend half of round 0.36 (ui-kit 0.36), two contracts settled after the
three apps' reviews. **Notifications** (`docs/notifications-harmonization.md`) get their layer 1:
app-declared types with an in-app and an e-mail switch, quiet hours, the digest period, push
shapes and outcomes, and a signed one-click unsubscribe, which `mail` now carries as RFC 8058
headers and a footer line. **Layer 2** begins (`docs/server-services-harmonization.md`): the
feedback service behind ports, its router factory (the new `fastapi` extra) and the shared
erasure — about 160, 225 and 185 lines out of keksdose, kastlan and Kurvenschmiede. Nothing is
removed. One typing change: `render_mail` takes a typed `unsubscribe=`, so a `**Mapping[str, str]`
splat into it no longer type-checks (no app does this).

### Added — notifications (round 0.36, `docs/notifications-harmonization.md`)

* **notifications:** layer 1 of the notifications round (settings §6.4): pure rules and models, no
  database and no routes. `registry` (`NotificationType`, `NotificationTypes`: each app declares
  its own types, the server's list is the only one), `resource` (the settings resource and its
  PATCH: `apply_switches_patch`, `resolve_switches`, `SETTINGS_SKIP`), `delivery` (`plan_delivery`:
  the `app` and `email` switches, a verified address, quiet hours holding push only), `timing`
  (`in_quiet_hours`, which fails open; `resolve_zone`, a null or unknown zone is Europe/Zurich;
  the digest's period key `2026-W42` / `2026-10` and `digest_due`, once a period with 3 days'
  grace), `push` (the subscription and payload shapes, `push_outcome`: 404/410 is gone,
  `should_reap` after 10 failures; `vapid_subject`), `unsubscribe` (a signed, unstored,
  non-expiring token whose key is derived from the app's own signing secret; it can only turn
  e-mail off), `inbox` (the inbox row and its retention) and `errors`. Lifted from keksdose's
  `schemas/push.py` and its services.
* **mail:** `MailMessage.headers`, `list_unsubscribe_headers(url)` (RFC 8058's
  `List-Unsubscribe` and `List-Unsubscribe-Post`, https only) and `MailUnsubscribe(label, page,
  one_click)`, which `render_mail(…, unsubscribe=)` turns into the footer line; `ResendClient`
  sends the headers, `ConsoleMailer` logs them. A header name the RFC does not allow, or CR/LF in
  a value, is refused. A mail's `kind` may be a notification type's id.
* **settings:** `apply_patch(…, skip=())`: a skipped field is checked as not nullable but left to
  its own rule (the notification types).

### Added — feedback, layer 2 (round 0.36, `docs/server-services-harmonization.md`)

* **feedback:** the feedback service behind ports — the flows the three apps each wrote
  around the layer-1 rules, written once. `feedback.ports`: `FeedbackActor`,
  `FeedbackRow`, `NewFeedback`, `FeedbackRepository[RowT]` (`get(id, *, viewer)`,
  `list_mine`, `inbox`, `open_crash`, `add`, `apply(row, changes, *, actor)`, `by_author`,
  `urls_named_except`), `AttachmentStorage` (`save`, `read`), `StoredAttachment`, `Clock`,
  `utc_now`. `feedback.service`: `FeedbackService(repository, *, policy, foreign_row,
  now)` with `create`, `report_crash`, `list_mine`, `inbox`, `update` and
  `erase_author` → `ErasedFeedback(reports, keys)`; `throttle_upload`,
  `upload_attachment`, `download_attachment` → `AttachmentDownload`. Both re-exported from
  `eifi1_server_kit.feedback`, without FastAPI, so a CLI files through the service too.
  The kit never commits: each flow makes at most one repository write, as its last step.
  The limiter is charged before the work (feedback §3.5, §3.6 as amended). The download
  reads the type off the bytes and always sends `Content-Disposition` and `nosniff`. The
  erasure deletes no file: it answers the bare keys, and each app deletes them at its
  own commit.
* **feedback:** `feedback.router` — `feedback_router(FeedbackDeps(…), *, prefix, tags)`,
  the contract's seven routes with keksdose's endpoint names (its operation ids and its
  `Body_upload_attachment_…` component stay), the app's own demo guard factory and admin
  guard first in each route's `dependencies` (so they answer before the body is
  validated), `foreign_row` (403 or 404), the app's schema subclasses and `to_response`,
  the limiters read from `app.state.feedback_limiters`; `app_state_limiters`. A
  `ValueError` when the create model's policy is not `deps.policy`. Behind the new
  **`fastapi` extra** (`fastapi>=0.115.0`, `python-multipart>=0.0.18`); without it,
  importing the module names the extra.
* **feedback:** three refusals, answered by `install_contract_error_handlers`:
  `FeedbackNotFoundError` (404 "Feedback not found"), `AttachmentNotFoundError` (404
  "Attachment not found"), `FeedbackThrottledError` (429 with `Retry-After`,
  `THROTTLED_DETAIL`, keksdose's sentence).
* **feedback:** `feedback.testing` (not re-exported, no FastAPI, no httpx):
  `InMemoryFeedbackRepository` (records each write), `InMemoryAttachmentStorage`,
  `MemoryFeedbackRow`, `actor`, `fixed_clock`, and the conformance checks an app runs on
  its own adapters: `check_repository` (files through the storage as the uploader
  first), `check_storage`, `check_round_trip` (also checks that `policy.prefix` is the
  mount).
* **export:** `scripts/export_api.py` exports `eifi1_server_kit.feedback.router`, and a
  `type` statement (`type Clock = …`) as a constant with its source.

### Docs — feedback, layer 2

* The limiter's and the crash module's docstrings say the amended rule: charge before the
  work. The README's layering table marks layer 2 as 0.9.0, gains a "layer 2" section and
  "Adopting layer 2", and calls the demo's read-only transaction "the demo's database
  layer".

## [0.8.0] (2026-10-13)

server-kit 0.8.0: the backend half of round 0.34 (ui-kit 0.34), from the apps' 0.33 / 0.7
adoption reports and Kurvenschmiede's server-kit asks of 2026-10 (S1, S2, S3, S5). Lemon
Squeezy goes, as 0.7 announced (decision 25, §14.13) — breaking for anything that still
names it, and no app sells through it. The rest is additive: the dummy hash for an
address with no account, the two account notices' words, the scheduler's token route as a
module, `content_disposition`'s `fallback=`, and the server halves of two contracts settled
after the apps' reviews — session refresh (ui-kit `docs/auth-harmonization.md` §11:
`session_ended`, `RefreshRequest`) and groups in the audit (`docs/user-admin-harmonization.md`
§11: five action words, `target_group_id`, `GroupRef`, a deleted group's kept name). One header changes where an app may
notice: the plain `filename=` of a name with a letter outside ASCII (below, Fixed).

### ⚠ BREAKING CHANGES

* **billing:** Lemon Squeezy is gone (decision 25, §14.13; deprecated since 0.7.0):
  `BillingProvider.LEMONSQUEEZY`, `map_lemonsqueezy_event`, `LEMONSQUEEZY_EVENT_KINDS`,
  `LEMONSQUEEZY_STATUSES`, `verify_lemonsqueezy_signature` and
  `LEMONSQUEEZY_SIGNATURE_HEADER`. `BillingProvider` is `paddle` alone, and
  `WEBHOOK_MAPPERS` and `SIGNATURE_HEADERS` hold Paddle only. `"lemonsqueezy"` is no
  provider: `<APP>_BILLING_PROVIDER=lemonsqueezy` fails at start, `parse_webhook_event` and
  `verify_webhook_signature` raise `ValueError` for it as for `"stripe"`, and a route that
  types its path parameter as `BillingProvider` answers 422 for `/webhooks/lemonsqueezy`.
  The three apps read that parameter as text and answer 404 for any provider but their
  own, so their routes don't change; keksdose's test posting to `/webhooks/lemonsqueezy`
  still gets its 404. Switching billing on with a provider other than Paddle — an app's
  `billing_provider` field typed `str` and holding another name — now fails at start: it
  switched on without Paddle's checks before.
* **billing:** a price id is text. `billing_price_ids` refuses a JSON number (Lemon
  Squeezy's numeric variant ids: `{"year": 12345}` read as `"12345"` until now), and a
  webhook whose `customer_id` is a number is poison (`PoisonEventError`, answered 200) —
  no Paddle id is one.
* **mail:** the account-deletion mail's word is `account_deletion`
  (`AccountNoticeKind.ACCOUNT_DELETION`, below), keksdose's. Breaking for an app on
  adoption, not for the kit's surface: kastlan's `kind="deletion"` and Kurvenschmiede's
  `deletion_request` become `account_deletion`, in their log lines and in Resend's
  `Idempotency-Key`, and a log query or alert on the old word moves with it.

### Added

* **auth:** `absent_account_check(hash_password, verify_password, *, cost=None)`, the
  check for an address with no account (auth §5.2, KS S3): one verify against a dummy
  hash, then `False`, always, so an unknown address costs what a known one does
  (Kurvenschmiede measured 3.7 ms against 262.2 ms before its own). Built from the app's
  own two functions — the kit hashes nothing — and holding the rules the three apps each
  kept in their own way: the dummy hashes a random secret
  (`secrets.token_urlsafe(ABSENT_ACCOUNT_SECRET_BYTES)`, 32, keksdose's), never a string in
  a source file (kastlan's); it is built lazily, at the first unknown address, and again
  whenever `cost()` changes (Kurvenschmiede's per-cost cache), so it is at the cost the real
  rows are checked at; and it goes through the app's own `verify_password`, so an over-long
  password is refused without hashing on both paths. In `auth.passwords`, re-exported from
  `eifi1_server_kit.auth`. Lifted from keksdose `auth_service.py:129-140`.
* **auth:** the refresh's coded refusal (auth §11, decision 7; ui-kit 0.34's
  `attachSessionRefresh`): `AuthErrorCode.SESSION_ENDED` — `"session_ended"`, 401, "This
  session has ended. Please sign in again." (Kurvenschmiede's `TokenRefusal.ENDED`) — for
  every definitive refusal of `POST /auth/refresh`: an unknown, expired or revoked token,
  a deactivated account, a required password change. The client decides by the status,
  so an app that has not switched keeps working; each app switches its refusals together
  (keksdose's five, Kurvenschmiede's `TokenRefusedError`, kastlan's token and session
  checks). And `RefreshRequest {refresh_token}` in `auth.schemas`, the body each app
  defined for itself, with no length rule: an empty token is the route's 401, never a 422,
  which the client would read as transient.
* **mail:** `AccountNoticeKind` — `email_change_notice` (to the old address, user-admin
  §6.2; the three apps' word already) and `account_deletion` (the answer to a deletion
  request, §6.4) — the `kind` for the two account mails that carry no one-time token (KS
  S5). Not `MailKind`, which is `user_admin`'s word for the mail an admin sends again.
  `Mailer.send`'s docstring names every kind: `OneTimeTokenKind`, `AccountNoticeKind`,
  `BillingNoticeKind`.
* **jobs:** a new module for the scheduler's token routes (billing §14.7), lifted from
  kastlan's and Kurvenschmiede's `ops_router.py`: `require_jobs_token(token)`, the FastAPI
  dependency (`Depends(require_jobs_token(lambda: settings.jobs_token))`; the setting read
  at every call; Starlette's `Request`, so the kit still doesn't import FastAPI), over
  `check_jobs_token(presented, expected)` — `X-Jobs-Token` (`JOBS_TOKEN_HEADER`) compared
  in constant time on bytes, a `SecretStr` or text setting, blank is unset — and
  `JobsTokenError` (401 for a missing or wrong token and for none set, the last logged at
  WARNING: the apps' one answer). `JobsSettings`, a mixin, holds `jobs_token: SecretStr |
  None` (`<APP>_JOBS_TOKEN`). `JobsTokenError` joins `CONTRACT_ERRORS`, so
  `install_contract_error_handlers` answers it 401, not an app's `ValueError` 400. The API
  export lists `eifi1_server_kit.jobs` against billing §14.7.
* **uploads:** `content_disposition(…, *, fallback=None)` (KS S2, part 2): the plain
  `filename=` an app would rather offer — `curve-12.csv`, a record's id — narrowed as the
  original would be; `filename*` keeps the original either way.
* **user_admin:** groups in the audit (user-admin §11, KS S1; ui-kit 0.34's
  `AdminActionLog groupNoun`). One vocabulary for both kits: "group" in the words, each
  app's noun — team, company, budget — only in the labels.
  * `AdminAction` gains `group_create`, `group_rename`, `group_delete`, `membership_add`
    and `membership_role` (§11.3). `CONFIRMATION_LEVELS` gives `group_delete`
    `acknowledge` (decision 5) and the other four `none`, so a `levels` answer built from
    every entry grows by five. A `match` over `AdminAction` that ends in `assert_never`
    adds the five.
  * `admin_action_record(…, target_group_id=None, target_group_name=None)`: each key only
    when given, as `company_id`, so a table without the columns (kastlan's, keksdose's)
    takes the record as it is. `target_group_name`, a deleted group's name (decision 6),
    is written on its `group_delete` row only and beside its `target_group_id`; anywhere
    else it is a `ValueError`. The columns are the app's, nullable and with no foreign key.
  * `GroupRef {id, name}` and `AdminActionRow.target_group_id` / `.target_group`, joined by
    the app as `actor` is: from the live group, else from its `group_delete` row's stamp
    (kastlan: from `company_id`).
  * `require_confirmation(…, target_email: str | None)` (KS 10): `None` is a group, which
    has no address — `none` passes, `acknowledge` passes on `acknowledged` alone (and is
    `confirmation_required` without it), `type_email` raises `ValueError`.
  * An app writes no group row before it pins 0.8: a schema whose `action` is typed
    `AdminAction` (Kurvenschmiede's `AdminActionOut`) refuses a word it doesn't know when
    the row is read, so `GET /admin/actions` would answer 500 (KS 19).

### Fixed

* **uploads:** `content_disposition`'s plain `filename=` writes no `?` (KS S2, part 1;
  round 0.34 B2): accents fold (NFKD, the combining marks dropped:
  `Débattement-à-gauche.csv` → `Debattement-a-gauche.csv`, 0.7 wrote
  `D?battement-?-gauche.csv`), and what is still not safe ASCII — `ß`, `Ø`, a CJK name, and
  Windows' `<>:/|?*` — becomes `_` (`停车扫描-轮廓-1.csv` → `____-__-1.csv`). `?` is one of
  the nine characters Windows refuses in a file name, and a reader that takes only
  `filename=` (`curl -J`) wrote what it said. Quotes, backslashes and control characters
  are dropped as before; the cut to 100 characters comes after the folding. The browser's
  name, `filename*`, is unchanged; ui-kit 0.34's `filenameFromDisposition` reads it first.

### Docs

* **billing:** the docstrings, the README and the events table speak Paddle only; a
  notice (`status` `None`) stays the dispatch's rule for a mapper that answers one, though
  no Paddle event is one. `NoProviderClient` is no longer "a provider the kit has no
  client for": with billing on, the provider is Paddle.
* **auth:** `auth.tokens` states what a refresh answers (auth §11.6): every definitive
  refusal is `401 session_ended`, never a 403; the route's own failure (a database error,
  a timeout) stays a 5xx, never a 401; and a signed-in endpoint answers 401 only about the
  session, never for a wrong input — a wrong current password or 2FA code is a 400, or the
  client's replay signs the user out over a typo (kastlan moves `POST /auth/2fa/disable`
  to 400 before it adopts).
* **auth:** the README's sign-in recipe calls `check_absent(password)` for an unknown
  address.

## [0.7.1] (2026-10-12)

server-kit 0.7.1: a patch from the apps' 0.33 / 0.7 adoption reports (kastlan, keksdose).
No public name is removed or renamed. Three shapes grow where an app may notice:
`OwedNotice` carries a fifth field, `row` (a hand-built one — a test's expected value —
passes the row too, and unpacking it into four names no longer works);
`PlanChangeResponse(…)` built directly needs `comped_until` (`of()` fills it, as every app
builds it); and `resume_after_withdrawal` asks the provider for a row that doesn't say
`cancel_at_period_end`.

### Fixed

* **billing:** `resume_after_withdrawal` undoes the deletion's cancellation before Paddle's
  webhook, and with billing off. It answered `None` unless the row said
  `cancel_at_period_end`, which `cancel_for_deletion` never sets — only Paddle's
  `subscription.updated` does, and the webhook answers 404 while billing is off — so a
  reactivation before that webhook left the cancellation scheduled at Paddle while the
  app's audit said "cancelled" and the reactivation recorded nothing to undo. It now asks
  the provider whenever the row has a provider subscription that hasn't ended (not
  `canceled` or `expired`, its period not over); the request's audit stays the check that
  the deletion made the cancellation (§12.37, §14.8). Removing nothing is harmless:
  `scheduled_change: null` sets the field to what it is; Paddle refusing it as a
  subscription that has ended (`subscription_update_when_canceled`) or a scheduled change
  it won't take (`subscription_scheduled_change_invalid_update`) is `None`, logged at INFO,
  and any other failure stays `"failed"` at ERROR. A third argument, `now=None` (the
  current time), reads the period as `require_new_checkout` does. From keksdose's 0.33
  report.
* **billing:** the provider and Paddle's environment are compared by value, not identity:
  `billing_provider_client`, the switch-on check and `verify_webhook_signature` read
  `BillingProvider.PADDLE` and the plain text `"paddle"` alike, and
  `billing_api_environment()` a plain `"sandbox"` or `"live"` (an unknown one is 503
  `billing_not_configured`). Settings holding the text — a test's `monkeypatch.setattr`
  or `model_construct`, or an app's field typed `str` — got `NoProviderClient`, skipped
  Paddle's checks at start, or refused a sandbox key against `"sandbox"`. From kastlan's
  0.33 report.
* **billing:** `billing_app`'s shape (lowercase, a letter first) holds where the app sets
  its tag as the README shows, `billing_app: str | None = "keksdose"`: redeclaring the
  field replaced the mixin's annotation and its pattern with it, so `"Not Valid!"` passed.
  A model validator on the mixin checks it whatever the field's type, the subclass's
  default included. From keksdose's 0.33 report.
* **billing:** a hosted checkout's `locale` is Paddle's, not the app's: `PaddleClient`
  sent the app's `de-CH` as it was. It sends `paddle_locale(locale)` (below); a blank
  locale sends none. The pay page maps its own `lang` (ui-kit's `payPageUrl`). From
  keksdose's 0.33 report.
* **billing:** `PlanChangeResponse.comped_until` is required though it may be `null`: it
  is always sent, and its default made the generated TypeScript optional (keksdose
  redeclared it). From keksdose's 0.33 report.
* **mail:** `render_message`'s plain-text link line sets French's no-break space (U+00A0,
  as ui-kit's French catalogue) before the colon — `Choisir\u00a0: https://…` — for any
  French `lang` (`fr`, `fr-CH`); every other language keeps the plain colon. From
  keksdose's 0.33 report.

### Added

* **billing:** `paddle_locale(kit_locale)`, ui-kit's `paddleLocale` server-side: the kit's
  language → Paddle's checkout locale (`de-CH` → `de`, `zh` → `zh-Hans`, `hu` → `en`,
  Paddle having no Hungarian; the others as they are), any tag read as the kit reads it
  (`de-AT` the one German, `zh-TW` the one Chinese) and one the kit doesn't ship English.
* **billing:** `AdminPlan` `{code, limits, sort}` (`.from_spec(plan)`) and
  `admin_plans_out(catalogue)` for `GET /admin/plans` (§14.12), lifted from keksdose: every
  plan of the catalogue in `sort` order, the ones not sold any more included, no prices —
  the operator's plan picker and the roster's plan filter. From keksdose's 0.33 report.
* **billing:** `OwedNotice.row`, the candidate's subscription row, so the notice job sets
  `owed.row.billing_notice_sent = owed.key` without carrying `(user, row)` as the payer.
  `NoticeCandidate` and `OwedNotice` take the row's type as a second parameter, inferred
  from `row` and defaulting to `SubscriptionRow` (`NoticeCandidate[User]` still reads), so
  the row comes back as the app's own. From keksdose's 0.33 report.

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
