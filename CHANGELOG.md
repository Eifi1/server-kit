# Changelog

All notable changes to `eifi1-server-kit` — the backend sibling of `@eifi1/ui-kit`.

The contract until 1.0 is the kit's: a **minor** (`0.x.0`) may remove or rename a public
name or change a rule's behaviour, and says so under **⚠ BREAKING CHANGES**; a **patch**
(`0.x.y`) fixes behaviour without changing the surface. Apps pin an exact tag (its commit
hash lands in their `uv.lock`), so nothing reaches an app until it asks for it. Write the
entry in the Conventional Commit; this file is assembled from them at release.

## [0.1.0] (unreleased — published by tagging `v0.1.0`)

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
