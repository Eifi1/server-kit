"""The ``images`` extra: keksdose's dev#510 guard, Pillow ``verify()`` after the signature check."""

from __future__ import annotations

import sys
from io import BytesIO

import pytest

from eifi1_server_kit.uploads import (
    ACCEPTED_MEDIA_TYPES,
    IMAGE_MEDIA_TYPES,
    UNDECODABLE_IMAGE_TYPES,
    NotAnImageError,
    check_upload,
    ensure_decodable_image,
)


def picture(fmt: str, size: tuple[int, int] = (64, 64)) -> bytes:
    """A real picture, made by Pillow (keksdose ``tests/api/_helpers.py:87`` ``jpeg_bytes``)."""
    image_module = pytest.importorskip("PIL.Image")
    buffer = BytesIO()
    image_module.new("RGB", size, "red").save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("fmt", "media_type"), [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("GIF", "image/gif"), ("WEBP", "image/webp")]
)
def test_every_accepted_picture_that_is_one_passes(fmt: str, media_type: str) -> None:
    data = picture(fmt)
    checked = check_upload(data, media_type)
    assert checked.media_type == media_type
    ensure_decodable_image(data, checked.media_type)


def test_a_picture_the_signature_check_lets_through_is_refused_here() -> None:
    """keksdose ``test_support_chat.py:509``: the PNG signature is there and no image behind
    it — a magic-byte check passes it, which is why the guard parses the container."""
    pytest.importorskip("PIL")
    fake = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    assert check_upload(fake, "image/png").media_type == "image/png"
    with pytest.raises(NotAnImageError) as refused:
        ensure_decodable_image(fake, "image/png")
    assert refused.value.status_code == 400 and "renamed" in str(refused.value)

    # A truncated PNG (its chunk CRCs) and a truncated WebP are refused…
    for fmt, media_type in (("PNG", "image/png"), ("WEBP", "image/webp")):
        whole = picture(fmt, (256, 256))
        with pytest.raises(NotAnImageError):
            ensure_decodable_image(whole[: len(whole) // 2], media_type)
    # …and so is anything Pillow cannot identify, whatever it was declared as.
    with pytest.raises(NotAnImageError):
        ensure_decodable_image(b"not a picture", "IMAGE/JPEG; charset=binary")
    with pytest.raises(NotAnImageError):
        ensure_decodable_image(b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml")


def test_verify_parses_the_container_and_does_not_decode_the_raster() -> None:
    """Exactly keksdose's trade, pinned so a change to it is a decision: ``verify()``, no
    ``load()`` — a JPEG or GIF cut off mid-raster still passes."""
    for fmt, media_type in (("JPEG", "image/jpeg"), ("GIF", "image/gif")):
        whole = picture(fmt, (256, 256))
        ensure_decodable_image(whole[: len(whole) // 2], media_type)


def test_a_decompression_bomb_is_refused_at_pillows_own_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """No limit of the kit's own: ``open`` raises over twice ``MAX_IMAGE_PIXELS`` (→ 400) and
    only warns between one and two times it, which passes."""
    image_module = pytest.importorskip("PIL.Image")
    monkeypatch.setattr(image_module, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(NotAnImageError):
        ensure_decodable_image(picture("PNG", (20, 20)), "image/png")  # 400 px > 2 × 100
    with pytest.warns(image_module.DecompressionBombWarning):
        ensure_decodable_image(picture("PNG", (10, 15)), "image/png")  # 150 px


def test_what_is_not_a_drawable_picture_passes_unparsed() -> None:
    """A PDF or a text file is a download and never drawn; a HEIC is a picture Pillow cannot
    read (keksdose ``test_upload_guards.py:151``)."""
    pytest.importorskip("PIL")
    for media_type in sorted(set(ACCEPTED_MEDIA_TYPES) - IMAGE_MEDIA_TYPES):
        ensure_decodable_image(b"\x00 anything", media_type)
    ensure_decodable_image(b"anything", None)
    heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heicmif1" + b"\x00" * 64
    for media_type in sorted(UNDECODABLE_IMAGE_TYPES):
        ensure_decodable_image(heic, media_type)


def test_without_the_extra_every_call_says_what_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """``None`` in ``sys.modules`` makes ``import PIL`` fail as if it were not installed."""
    monkeypatch.setitem(sys.modules, "PIL", None)
    for media_type in ("image/png", "text/plain"):
        with pytest.raises(ImportError, match=r"eifi1-server-kit\[images\]"):
            ensure_decodable_image(b"\x89PNG\r\n\x1a\n", media_type)
