"""What an uploaded attachment IS, decided by its bytes — and how it may be served back.

The contract's attachment policy (§3.5): ``image/png``, ``image/jpeg``, ``image/webp``,
``image/gif``, ``application/pdf``, ``text/plain``; 10 MB per file; an empty file is a
400, an oversized one a 413, a file claiming to be a picture that is not one a 400,
anything else a 415. Images are served ``inline``, everything else as ``attachment``.

**The bytes decide, not the header** — Kurvenschmiede's approach, lifted from
``kurvenschmiede/adapters/api/feedback_router.py:54-116``: the declared type is the
client's word for it, and a file labelled ``image/png`` containing a script would be a
stored cross-site script against the admin who opens it. A picture or a PDF is named by
its signature; plain text has none, so it needs the declaration AND UTF-8 without NUL.
No Pillow: a magic-number table is smaller than the dependency. What that gives up — a
truncated PNG passes, because only the head is read — is the trade Kurvenschmiede made
knowingly; keksdose's Pillow ``verify()`` (``upload_guards.py:136``) stays an app-side
extra for an app that wants it.

Nothing here stores anything. :func:`check_upload` answers what the file is;
:func:`store_attachment` runs the check, mints keksdose's content-addressed key and hands
the bytes to the app's own ``save`` — the object store, the disk or a table stays the app's.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal
from urllib.parse import quote

#: Per-file ceiling (contract §3.5; keksdose ``upload_guards.py:197`` ``ATTACHMENT_MAX_BYTES``).
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

#: Accepted media type → the extension a stored key carries (keksdose ``upload_guards.py:198``
#: ``ATTACHMENT_EXT``). No SVG, no HTML: nothing that can carry script onto the app's origin.
ACCEPTED_MEDIA_TYPES: Mapping[str, str] = MappingProxyType(
    {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
        "image/gif": "gif",
        "application/pdf": "pdf",
        "text/plain": "txt",
    }
)

#: Extension → the ``Content-Type`` to serve it with (keksdose ``upload_guards.py:206``
#: ``EXT_MEDIA_TYPE``). ``jpeg`` is read as well as written ``jpg`` for keys minted elsewhere.
EXTENSION_MEDIA_TYPES: Mapping[str, str] = MappingProxyType(
    {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "gif": "image/gif",
        "pdf": "application/pdf",
        "txt": "text/plain; charset=utf-8",
    }
)

#: The ONLY media types any download may serve ``inline`` (keksdose ``upload_guards.py:227``).
IMAGE_MEDIA_TYPES: frozenset[str] = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})

#: The accepted types as an ``<input accept>`` / dialog list, in the contract's order.
ACCEPT_LIST: tuple[str, ...] = tuple(ACCEPTED_MEDIA_TYPES)

Disposition = Literal["inline", "attachment"]


# ── refusals ────────────────────────────────────────────────────────────────


class UploadRejectedError(ValueError):
    """Base class of every upload refusal; ``status_code`` is the contract's answer (§3.7)."""

    status_code: int = 400


class EmptyUploadError(UploadRejectedError):
    """The file has no bytes (→ 400)."""

    status_code = 400


class UploadTooLargeError(UploadRejectedError):
    """The file is over the per-file ceiling (→ 413)."""

    status_code = 413


class NotAnImageError(UploadRejectedError):
    """Declared as one of the accepted pictures, and the bytes are not one (→ 400)."""

    status_code = 400


class UnsupportedUploadTypeError(UploadRejectedError):
    """Not one of the accepted types, whatever it is called (→ 415)."""

    status_code = 415


# ── detection ───────────────────────────────────────────────────────────────

#: The first bytes of each type that has them. WEBP is a RIFF container and needs two
#: reads (see :func:`signature_type`); plain text has no signature at all.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
)


def bare_media_type(content_type: str | None) -> str:
    """The declared type without parameters, lower-cased (``text/plain; charset=…`` → ``text/plain``).

    keksdose ``upload_guards.py:86`` ``bare_media_type``.
    """
    return (content_type or "").split(";", 1)[0].strip().lower()


def signature_type(data: bytes) -> str | None:
    """What these bytes say they are by their first few, or ``None``.

    Kurvenschmiede ``feedback_router.py:69`` ``signature_type``.
    """
    for signature, media_type in _MAGIC:
        if data.startswith(signature):
            return media_type
    # "RIFF" <four bytes of length> "WEBP" — the length says nothing about the format,
    # so the two ends are read and the middle is skipped.
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def is_plain_text(data: bytes) -> bool:
    """UTF-8 with no NUL in it (Kurvenschmiede ``feedback_router.py:81`` ``_is_text``)."""
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def sniffed_type(data: bytes, declared: str = "") -> str | None:
    """What a file IS, or ``None`` for anything this feature is not for.

    Kurvenschmiede ``feedback_router.py:91`` ``sniffed_type``: the signature first, and
    only when there is none, ``text/plain`` — if it was declared so and reads as UTF-8
    without NUL. ``declared`` may carry parameters; they are dropped.
    """
    found = signature_type(data)
    if found is not None:
        return found
    if bare_media_type(declared) == "text/plain" and is_plain_text(data):
        return "text/plain"
    return None


# ── the check ───────────────────────────────────────────────────────────────


def inline_or_attachment(media_type: str | None) -> Disposition:
    """The ``Content-Disposition`` a stored file may be served with: ``inline`` for the four
    picture types, ``attachment`` for everything else — PDFs and text included, so neither is
    ever interpreted in the app's origin (keksdose ``upload_guards.py:230``, feedback #87).
    Unknown or absent is the safe answer, not the convenient one."""
    return "inline" if bare_media_type(media_type) in IMAGE_MEDIA_TYPES else "attachment"


@dataclass(frozen=True, slots=True)
class CheckedUpload:
    """An accepted file: what it is, the extension its key carries, how it is served."""

    media_type: str
    extension: str
    disposition: Disposition
    size: int


def check_upload(
    data: bytes,
    declared_type: str | None,
    *,
    max_bytes: int = MAX_ATTACHMENT_BYTES,
) -> CheckedUpload:
    """Accept or refuse one uploaded file, by its bytes.

    In order — the contract's statuses (§3.5, §3.7) on Kurvenschmiede's detection
    (``feedback_router.py:123`` ``_read_upload``):

    1. no bytes → :class:`EmptyUploadError` (400);
    2. over ``max_bytes`` → :class:`UploadTooLargeError` (413) — read the upload with
       ``await file.read(max_bytes + 1)`` and one byte past the cap is enough to know;
    3. :func:`sniffed_type` names it → accepted, as the DETECTED type (a PNG declared
       ``image/jpeg`` is stored and served as a PNG);
    4. otherwise, declared as one of the accepted pictures → :class:`NotAnImageError`
       (400: "renamed to .png"); anything else → :class:`UnsupportedUploadTypeError` (415).

    One deliberate difference from Kurvenschmiede: there a declared ``image/*`` OUTSIDE the
    list (``image/svg+xml``, ``image/heic``) was a 400; the contract (§3.7) and keksdose's
    test (``test_feedback_attachments.py:125``) call a type outside the list a 415.
    """
    if not data:
        raise EmptyUploadError("The file is empty.")
    if len(data) > max_bytes:
        raise UploadTooLargeError(f"The file is larger than {max_bytes // (1024 * 1024)} MB.")
    declared = bare_media_type(declared_type)
    found = sniffed_type(data, declared)
    if found is None:
        if declared in IMAGE_MEDIA_TYPES:
            raise NotAnImageError(
                "That file says it is an image but can't be read as one. If you renamed it, "
                "send it under its original name instead."
            )
        raise UnsupportedUploadTypeError("Only images, PDF or text files are allowed.")
    return CheckedUpload(
        media_type=found,
        extension=ACCEPTED_MEDIA_TYPES[found],
        disposition=inline_or_attachment(found),
        size=len(data),
    )


# ── keys and storing ────────────────────────────────────────────────────────


def content_addressed_key(data: bytes, extension: str) -> str:
    """keksdose's key: ``sha256(bytes)[:12].<ext>`` (``upload_guards.py:272``).

    The same paste twice is one URL — and, for erasure, one object two reports may share
    (see :func:`eifi1_server_kit.feedback.attachment_keys_to_purge`). kastlan mints
    ``<uuid32>_<sha12>.<ext>`` and Kurvenschmiede an HMAC of the same shape; they pass
    their own ``mint_key`` to :func:`store_attachment`, or call :func:`check_upload` alone.
    """
    return f"{hashlib.sha256(data).hexdigest()[:12]}.{extension}"


@dataclass(frozen=True, slots=True)
class StoredUpload:
    """What :func:`store_attachment` stored: the key, and the checked file it was minted for."""

    key: str
    media_type: str
    extension: str
    disposition: Disposition
    size: int


SaveFn = Callable[[str, bytes, str], Awaitable[object]]
MintKeyFn = Callable[[bytes, str], str]


async def store_attachment(
    data: bytes,
    declared_type: str | None,
    *,
    save: SaveFn,
    mint_key: MintKeyFn = content_addressed_key,
    max_bytes: int = MAX_ATTACHMENT_BYTES,
) -> StoredUpload:
    """The whole upload policy for one attachment, with the storing left to the app.

    keksdose ``upload_guards.py:250`` ``store_attachment_bytes`` without its object store:
    :func:`check_upload`, then ``mint_key(data, extension)``, then
    ``await save(key, data, media_type)`` — ``save`` adds whatever prefix or folder the app
    keeps its attachments under. The caller keeps what is genuinely its own: the throttle
    (charge it before or after this, see :mod:`eifi1_server_kit.limiter`), any per-user
    refusal (keksdose's demo account), and the URL it answers with.
    """
    checked = check_upload(data, declared_type, max_bytes=max_bytes)
    key = mint_key(data, checked.extension)
    await save(key, data, checked.media_type)
    return StoredUpload(
        key=key,
        media_type=checked.media_type,
        extension=checked.extension,
        disposition=checked.disposition,
        size=checked.size,
    )


# ── serving ─────────────────────────────────────────────────────────────────


def media_type_for_extension(extension: str) -> str | None:
    """The ``Content-Type`` to serve a key's extension with, or ``None`` for an unknown one."""
    return EXTENSION_MEDIA_TYPES.get(extension.lower())


def content_disposition(filename: str, disposition: Disposition = "attachment") -> str:
    """A ``Content-Disposition`` value whose filename cannot break the header.

    The name is whatever the client called the file. For the plain ``filename=`` quotes,
    backslashes and anything not printable are dropped, it is cut to 100 characters and
    narrowed to ASCII — Starlette encodes header values as latin-1, so an en dash or a CJK
    character there was a 500 out of a plain GET (keksdose ``upload_guards.py:277``, on its
    object-store branch only). The ORIGINAL name follows in the RFC 5987 ``filename*``,
    percent-encoded, so nothing is lost; every browser prefers that one. keksdose's
    ``content_disposition`` and Kurvenschmiede's ``_disposition``
    (``feedback_router.py:156``), merged.
    """
    kept = "".join(c for c in filename if c.isprintable() and c not in '"\\')[:100]
    plain = kept.encode("ascii", "replace").decode("ascii").strip() or "attachment"
    return f"{disposition}; filename=\"{plain}\"; filename*=UTF-8''{quote(filename or plain, safe='')}"
