"""What an upload is, by its bytes (contract §3.5), and how it is served back."""

from __future__ import annotations

from io import BytesIO
from typing import Any

import pytest
from starlette.datastructures import UploadFile
from starlette.responses import Response

from eifi1_server_kit.uploads import (
    ACCEPT_LIST,
    ACCEPTED_MEDIA_TYPES,
    EXTENSION_MEDIA_TYPES,
    MAX_ATTACHMENT_BYTES,
    CheckedUpload,
    EmptyUploadError,
    NotAnImageError,
    UnsupportedUploadTypeError,
    UploadRejectedError,
    UploadTooLargeError,
    bare_media_type,
    check_upload,
    content_addressed_key,
    content_disposition,
    inline_or_attachment,
    is_plain_text,
    media_type_for_extension,
    read_capped_upload,
    signature_type,
    sniffed_type,
    store_attachment,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
GIF87 = b"GIF87a" + b"\x00" * 8
GIF89 = b"GIF89a" + b"\x00" * 8
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 8
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"
TEXT = "Kontoauszug – März\n".encode()


def test_the_accept_list_is_the_contracts() -> None:
    assert ACCEPT_LIST == ("image/png", "image/jpeg", "image/webp", "image/gif", "application/pdf", "text/plain")
    for hostile in ("text/html", "image/svg+xml", "application/xhtml+xml"):
        assert hostile not in ACCEPTED_MEDIA_TYPES
    assert MAX_ATTACHMENT_BYTES == 10 * 1024 * 1024


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (PNG, "image/png"),
        (JPEG, "image/jpeg"),
        (GIF87, "image/gif"),
        (GIF89, "image/gif"),
        (WEBP, "image/webp"),
        (PDF, "application/pdf"),
        (b"RIFF\x24\x00\x00\x00WAVEfmt ", None),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", None),
        (b"", None),
    ],
)
def test_the_signature_table(data: bytes, expected: str | None) -> None:
    """Kurvenschmiede ``feedback_router.py:58-78``."""
    assert signature_type(data) == expected


def test_text_needs_the_declaration_and_no_nul() -> None:
    """Plain text has no signature: the declaration names it, and a NUL byte refuses it."""
    assert sniffed_type(TEXT, "text/plain; charset=utf-8") == "text/plain"
    assert sniffed_type(TEXT, "application/octet-stream") is None
    assert sniffed_type(b"a\x00b", "text/plain") is None
    # UTF-16 carries a NUL beside every ASCII character.
    assert sniffed_type("log".encode("utf-16"), "text/plain") is None
    assert is_plain_text(b"") and not is_plain_text(b"\x00")
    # A signature wins over any declaration.
    assert sniffed_type(PDF, "text/plain") == "application/pdf"


def test_text_in_any_encoding_is_text_unless_utf8_is_required() -> None:
    """keksdose's policy is the default: it accepted any declared ``text/plain``, and its
    support chat shares the policy, so a Windows-1252 export is a text file. Text is only
    ever served as an ``attachment``, so its encoding is no security property.
    Kurvenschmiede keeps its UTF-8 rule (``feedback_router.py:81-116``) with the flag."""
    export = "Grüsse – März 2026\r\n".encode("cp1252")
    assert is_plain_text(export) and not is_plain_text(export, require_utf8_text=True)
    assert is_plain_text(TEXT, require_utf8_text=True)
    assert sniffed_type(export, "text/plain") == "text/plain"
    assert sniffed_type(export, "text/plain", require_utf8_text=True) is None
    assert sniffed_type(b"\xff\xfe broken", "text/plain", require_utf8_text=True) is None
    assert check_upload(export, "text/plain") == CheckedUpload("text/plain", "txt", "attachment", len(export))
    with pytest.raises(UnsupportedUploadTypeError) as strict:
        check_upload(export, "text/plain", require_utf8_text=True)
    assert strict.value.status_code == 415
    # The NUL check holds either way, and the flag changes nothing for a signature.
    with pytest.raises(UnsupportedUploadTypeError):
        check_upload(b"MZ\x90\x00\x03", "text/plain")
    assert check_upload(PNG, "text/plain", require_utf8_text=True).media_type == "image/png"


def test_each_accepted_type_passes_as_what_it_is() -> None:
    for data, declared, media, ext, disposition in (
        (PNG, "image/png", "image/png", "png", "inline"),
        (JPEG, "image/jpeg", "image/jpeg", "jpg", "inline"),
        (WEBP, "image/webp", "image/webp", "webp", "inline"),
        (GIF89, "image/gif", "image/gif", "gif", "inline"),
        (PDF, "application/pdf", "application/pdf", "pdf", "attachment"),
        (TEXT, "text/plain", "text/plain", "txt", "attachment"),
    ):
        assert check_upload(data, declared) == CheckedUpload(media, ext, disposition, len(data))  # type: ignore[arg-type]


def test_the_bytes_decide_not_the_header() -> None:
    """A paste carries whatever the clipboard says; the stored type is the detected one."""
    assert check_upload(PNG, "image/jpeg").media_type == "image/png"
    assert check_upload(PNG, None).extension == "png"
    assert check_upload(PDF, "application/octet-stream").media_type == "application/pdf"


def test_the_refusals_in_order_with_their_statuses() -> None:
    """keksdose ``test_feedback_attachments.py:118``: wrong type 415, empty 400, oversized 413,
    and a file that CLAIMS to be an image but is not one 400 (dev#510)."""
    with pytest.raises(UnsupportedUploadTypeError) as svg:
        check_upload(b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml")
    assert svg.value.status_code == 415
    with pytest.raises(EmptyUploadError) as empty:
        check_upload(b"", "image/png")
    assert empty.value.status_code == 400
    with pytest.raises(UploadTooLargeError) as huge:
        check_upload(b"%PDF" + b"x" * MAX_ATTACHMENT_BYTES, "application/pdf")
    assert huge.value.status_code == 413
    with pytest.raises(NotAnImageError) as lying:
        check_upload(b"not a png at all", "image/png")
    assert lying.value.status_code == 400 and "renamed" in str(lying.value)
    # A HEIC is no accepted picture: outside the list is 415 (contract §3.7), not 400.
    with pytest.raises(UnsupportedUploadTypeError):
        check_upload(b"\x00\x00\x00\x18ftypheic", "image/heic")
    with pytest.raises(UnsupportedUploadTypeError):
        check_upload(b"MZ\x90\x00", "application/x-msdownload")
    # Exactly at the cap is allowed; a custom cap applies.
    assert check_upload(b"%PDF-" + b"x" * (MAX_ATTACHMENT_BYTES - 5), "application/pdf").size == MAX_ATTACHMENT_BYTES
    with pytest.raises(UploadTooLargeError, match="larger than 4 MB"):
        check_upload(PNG + b"x" * (4 * 1024 * 1024), "image/png", max_bytes=4 * 1024 * 1024)
    for error in (EmptyUploadError, UploadTooLargeError, NotAnImageError, UnsupportedUploadTypeError):
        assert issubclass(error, UploadRejectedError) and issubclass(error, ValueError)


def test_the_download_disposition_is_derived_from_the_media_type_alone() -> None:
    """keksdose ``test_upload_guards.py:177`` (feedback #87)."""
    assert inline_or_attachment("image/png") == "inline"
    assert inline_or_attachment("image/jpeg; charset=binary") == "inline"
    assert inline_or_attachment("IMAGE/WEBP") == "inline"
    assert inline_or_attachment("application/pdf") == "attachment"
    assert inline_or_attachment("text/plain; charset=utf-8") == "attachment"
    assert inline_or_attachment(None) == "attachment"
    assert inline_or_attachment("image/svg+xml") == "attachment"
    assert {ext: inline_or_attachment(EXTENSION_MEDIA_TYPES[ext]) for ext in sorted(EXTENSION_MEDIA_TYPES)} == {
        "gif": "inline",
        "jpeg": "inline",
        "jpg": "inline",
        "pdf": "attachment",
        "png": "inline",
        "txt": "attachment",
        "webp": "inline",
    }
    assert media_type_for_extension("TXT") == "text/plain; charset=utf-8"
    assert media_type_for_extension("svg") is None
    assert bare_media_type(" Text/Plain ; charset=utf-8") == "text/plain"
    assert bare_media_type(None) == ""


def test_a_filename_cannot_break_the_content_disposition_header() -> None:
    """keksdose ``test_upload_guards.py:34``: quotes, backslashes and control characters leave
    the plain form; the original is repeated in the RFC 5987 form so nothing is lost."""
    header = content_disposition('my "report".pdf')
    assert header.startswith('attachment; filename="my report.pdf"')
    assert "filename*=UTF-8''my%20%22report%22.pdf" in header
    assert 'filename="ab.pdf"' in content_disposition("a\\b\r\n.pdf")
    assert 'filename="attachment"' in content_disposition('""')
    assert "filename*=UTF-8''Kontoausz%C3%BCge.pdf" in content_disposition("Kontoauszüge.pdf")
    assert content_disposition("shot.png", "inline").startswith('inline; filename="shot.png"')
    assert content_disposition("") == "attachment; filename=\"attachment\"; filename*=UTF-8''attachment"
    # Kurvenschmiede caps the plain name at 100 characters.
    assert 'filename="' + "a" * 100 + '"' in content_disposition("a" * 300)


def test_a_name_outside_latin1_still_serves() -> None:
    """keksdose ``test_upload_guards.py:103``: Starlette encodes header values as latin-1, so
    an en dash or a CJK character in the plain parameter was a 500 while the Response was
    being CONSTRUCTED."""
    for name in ("Kontoauszug 1 – 2026.pdf", "发票.pdf", "Beleg ’ März.pdf"):
        response = Response(b"%PDF-1.4", headers={"Content-Disposition": content_disposition(name)})
        assert response.raw_headers, name
        assert "filename*=utf-8''" in response.headers["content-disposition"].lower()


def test_the_content_addressed_key() -> None:
    """keksdose ``upload_guards.py:272``: the same bytes are the same key."""
    key = content_addressed_key(b"abc", "png")
    assert key == "ba7816bf8f01.png"
    assert content_addressed_key(b"abc", "png") == key and content_addressed_key(b"abd", "png") != key


async def test_storing_checks_mints_and_hands_the_bytes_to_the_app() -> None:
    saved: list[tuple[str, bytes, str]] = []

    async def save(key: str, data: bytes, media_type: str) -> None:
        saved.append((key, data, media_type))

    stored = await store_attachment(PNG, "image/jpeg", save=save)
    assert stored.key == content_addressed_key(PNG, "png") and stored.disposition == "inline"
    assert saved == [(stored.key, PNG, "image/png")]

    # kastlan mints its own key shape.
    def uuid_key(data: bytes, extension: str) -> str:
        return f"{'0' * 32}_{content_addressed_key(data, extension)}"

    custom = await store_attachment(PDF, "application/pdf", save=save, mint_key=uuid_key)
    assert custom.key.startswith("0" * 32 + "_") and custom.key.endswith(".pdf")

    # A refused file never reaches `save`.
    with pytest.raises(NotAnImageError):
        await store_attachment(b"nope", "image/png", save=save)
    assert len(saved) == 2

    # The text policy is passed through: any encoding by default, UTF-8 on request.
    latin = "Grüsse".encode("latin-1")
    assert (await store_attachment(latin, "text/plain", save=save)).extension == "txt"
    with pytest.raises(UnsupportedUploadTypeError):
        await store_attachment(latin, "text/plain", save=save, require_utf8_text=True)
    assert len(saved) == 3


def test_the_refusal_statuses_map_without_a_table() -> None:
    def status_of(data: bytes, declared: str) -> Any:
        try:
            check_upload(data, declared)
        except UploadRejectedError as exc:
            return exc.status_code
        return 201

    assert [
        status_of(*case) for case in ((PNG, "image/png"), (b"", "x"), (b"x", "image/gif"), (b"x", "font/woff"))
    ] == [
        201,
        400,
        400,
        415,
    ]


class _CountingFile:
    """A file that records how much it was asked for."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.asked: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.asked.append(size)
        return self.data if size < 0 else self.data[:size]


async def test_a_capped_read_stops_one_byte_past_the_cap() -> None:
    """keksdose ``upload_guards.py:254``: one byte past the cap is enough to know."""
    huge = _CountingFile(b"%PDF-" + b"x" * (3 * 1024 * 1024))
    with pytest.raises(UploadTooLargeError, match="larger than 1 MB") as over:
        await read_capped_upload(huge, 1024 * 1024)
    assert over.value.status_code == 413
    assert huge.asked == [1024 * 1024 + 1]

    with pytest.raises(EmptyUploadError) as empty:
        await read_capped_upload(_CountingFile(b""), 1024 * 1024)
    assert empty.value.status_code == 400 and str(empty.value) == "The file is empty."

    # Exactly at the cap is the whole file; the default cap is the contract's 10 MB.
    exact = _CountingFile(b"y" * 1024 * 1024)
    assert await read_capped_upload(exact, 1024 * 1024) == exact.data
    default = _CountingFile(PNG)
    assert await read_capped_upload(default) == PNG and default.asked == [MAX_ATTACHMENT_BYTES + 1]


async def test_a_capped_read_takes_starlettes_upload_file() -> None:
    """The type an app actually hands it — FastAPI's ``UploadFile`` is this class."""
    upload = UploadFile(BytesIO(PNG), filename="shot.png")
    data = await read_capped_upload(upload)
    assert check_upload(data, upload.content_type).media_type == "image/png"
    with pytest.raises(UploadTooLargeError):
        await read_capped_upload(UploadFile(BytesIO(b"x" * 11)), 10)
