"""The object store (0.26): one contract over the three backends — the local one on a real
directory, Cloud Storage and S3 through stub clients injected as ``client=`` (no network, no
bucket) — then each backend's own failures, and the purge."""

from __future__ import annotations

import asyncio
import errno
import io
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, cast

import google.auth.exceptions
import google.cloud.storage
import minio
import pytest
import urllib3
from google.api_core import exceptions as gcs_errors
from minio import Minio
from minio.error import S3Error
from urllib3.exceptions import MaxRetryError, ProtocolError

from eifi1_server_kit.objects import (
    PURGE_CONCURRENCY,
    GcsObjectStore,
    LocalObjectStore,
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
    S3ObjectStore,
    StoredObject,
    purge_objects,
)

BUCKET = "kurvenschmiede-uploads"
KEY = "feedback/1ed84e1bc3c7.webp"


# --- Cloud Storage's client, stubbed: the calls GcsObjectStore makes, on a dict -------------


class _GcsBucket:
    def __init__(self, name: str, calls: list[tuple[str, int]]) -> None:
        self.name = name
        self.objects: dict[str, tuple[bytes, str, datetime]] = {}
        self.fail: dict[str, Exception] = {}
        self.calls = calls

    def call(self, op: str) -> None:
        self.calls.append((op, threading.get_ident()))
        if op in self.fail:
            raise self.fail[op]

    def blob(self, name: str) -> _GcsBlob:
        return _GcsBlob(self, name)

    def list_blobs(self, *, prefix: str | None = None) -> Iterator[_Listed]:
        self.call("list")
        for name in sorted(self.objects):
            if name.startswith(prefix or ""):
                yield _Listed(name, self.objects[name][2])


@dataclass
class _Listed:
    name: str
    updated: datetime


@dataclass
class _GcsBlob:
    bucket: _GcsBucket
    name: str

    def upload_from_string(self, data: bytes, content_type: str) -> None:
        self.bucket.call("upload")
        self.bucket.objects[self.name] = (data, content_type, datetime.now(UTC))

    def download_as_bytes(self) -> bytes:
        self.bucket.call("download")
        if self.name not in self.bucket.objects:
            raise gcs_errors.NotFound(f"No such object: {self.bucket.name}/{self.name}")
        return self.bucket.objects[self.name][0]

    def delete(self) -> None:
        self.bucket.call("delete")
        if self.bucket.objects.pop(self.name, None) is None:
            raise gcs_errors.NotFound(f"No such object: {self.bucket.name}/{self.name}")


class _GcsClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.buckets: dict[str, _GcsBucket] = {}

    def bucket(self, name: str) -> _GcsBucket:
        return self.buckets.setdefault(name, _GcsBucket(name, self.calls))


# --- minio, stubbed: GCS behind the S3 API answers a DELETE of a missing key with NoSuchKey --


def _s3_error(code: str) -> S3Error:
    return S3Error(urllib3.HTTPResponse(status=404), code, f"{code}.", f"/{BUCKET}/{KEY}", "request", "host")


class _S3Response:
    def __init__(self, data: bytes, fail: Exception | None) -> None:
        self.data = data
        self.fail = fail
        self.closed = False
        self.released = False

    def read(self) -> bytes:
        if self.fail is not None:
            raise self.fail
        return self.data

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


@dataclass
class _S3Listed:
    object_name: str | None
    last_modified: datetime | None


class _MinioStub:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str, datetime]] = {}
        self.fail: dict[str, Exception] = {}
        self.calls: list[tuple[str, int]] = []
        self.responses: list[_S3Response] = []
        self.listings: list[dict[str, object]] = []
        self.broken_body: Exception | None = None
        self.extra: list[_S3Listed] = []

    def call(self, op: str, bucket_name: str) -> None:
        self.calls.append((op, threading.get_ident()))
        assert bucket_name == BUCKET
        if op in self.fail:
            raise self.fail[op]

    def put_object(
        self, bucket_name: str, object_name: str, data: io.BytesIO, length: int, content_type: str = ""
    ) -> None:
        self.call("put", bucket_name)
        payload = data.read()
        assert len(payload) == length
        self.objects[object_name] = (payload, content_type, datetime.now(UTC))

    def get_object(self, bucket_name: str, object_name: str) -> _S3Response:
        self.call("get", bucket_name)
        if object_name not in self.objects:
            raise _s3_error("NoSuchKey")
        response = _S3Response(self.objects[object_name][0], self.broken_body)
        self.responses.append(response)
        return response

    def remove_object(self, bucket_name: str, object_name: str) -> None:
        self.call("remove", bucket_name)
        if self.objects.pop(object_name, None) is None:
            raise _s3_error("NoSuchKey")

    def list_objects(self, bucket_name: str, prefix: str | None = None, recursive: bool = False) -> Iterator[_S3Listed]:
        self.listings.append({"prefix": prefix, "recursive": recursive})
        self.call("list", bucket_name)
        found = [_S3Listed(name, stamp) for name, (_, _, stamp) in self.objects.items()]
        for item in sorted(found + self.extra, key=lambda item: item.object_name or ""):
            if (item.object_name or "").startswith(prefix or ""):
                yield item


def _s3(stub: _MinioStub) -> S3ObjectStore:
    return S3ObjectStore(BUCKET, client=cast(Minio, stub))


# --- one contract, three backends ----------------------------------------------------------


@pytest.fixture(params=["local", "gcs", "s3"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> ObjectStore:
    if request.param == "local":
        return LocalObjectStore(tmp_path / "store")
    if request.param == "gcs":
        return GcsObjectStore(BUCKET, client=_GcsClient())
    return _s3(_MinioStub())


async def test_a_saved_object_reads_back_and_a_second_save_replaces_it(store: ObjectStore) -> None:
    await store.save(KEY, b"RIFF....WEBP", content_type="image/webp")
    assert await store.read(KEY) == b"RIFF....WEBP"
    await store.save(KEY, b"second")
    assert await store.read(KEY) == b"second"
    await store.save("feedback/empty.txt", b"")
    assert await store.read("feedback/empty.txt") == b""


async def test_a_missing_object_is_the_apps_404(store: ObjectStore) -> None:
    with pytest.raises(ObjectNotFoundError) as caught:
        await store.read(KEY)
    error = caught.value
    # One type for every failure, and a FileNotFoundError, as a missing file on disk is.
    assert isinstance(error, ObjectStoreError) and isinstance(error, FileNotFoundError)
    assert isinstance(error, OSError)
    assert error.errno == errno.ENOENT and error.filename == KEY


async def test_remove_deletes_and_a_missing_object_is_already_removed(store: ObjectStore) -> None:
    await store.save(KEY, b"x")
    await store.remove(KEY)
    with pytest.raises(ObjectNotFoundError):
        await store.read(KEY)
    await store.remove(KEY)
    await store.remove("feedback/never-there.png")


async def test_the_listing_is_every_key_under_the_prefix_in_key_order(store: ObjectStore) -> None:
    before = datetime.now(UTC) - timedelta(seconds=5)
    for key in ("support/aa.webp", "feedback/sub/c.pdf", "feedback/ab.png", "feedbackextra/x.txt", "feedback/aa.webp"):
        await store.save(key, key.encode())

    def keys(found: list[StoredObject]) -> list[str]:
        return [item.key for item in found]

    under = await store.list_objects("feedback/")
    assert keys(under) == ["feedback/aa.webp", "feedback/ab.png", "feedback/sub/c.pdf"]
    for item in under:
        assert item.modified.utcoffset() == timedelta(0) and before <= item.modified <= datetime.now(UTC) + timedelta(
            seconds=5
        )
    # A prefix is a string's start, as an object store's is, not a directory.
    assert keys(await store.list_objects("feedback/a")) == ["feedback/aa.webp", "feedback/ab.png"]
    assert keys(await store.list_objects("feedback")) == [
        "feedback/aa.webp",
        "feedback/ab.png",
        "feedback/sub/c.pdf",
        "feedbackextra/x.txt",
    ]
    assert len(await store.list_objects()) == 5
    assert await store.list_objects("nothing/") == []
    assert await store.list_objects("feedback/sub/c.pdf/") == []


BAD_KEYS = [
    "",
    "/etc/passwd",
    "../outside.txt",
    "feedback/../../outside.txt",
    "feedback/./x.png",
    "./x.png",
    "feedback//x.png",
    "feedback/",
    "feedback/.hidden",
    ".env",
    "feedback\\x.png",
    "feedback/x\x00.png",
    "feedback/x\n.png",
    "feedback/x\x7f.png",
    "feedback/\udc80.png",
    "x" * 256,
    "a/" * 512 + "b",
]


@pytest.mark.parametrize("key", BAD_KEYS)
async def test_a_key_no_backend_could_hold_is_refused_before_the_store_is_asked(
    store: ObjectStore, key: str, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="not an object key"):
        await store.save(key, b"x")
    with pytest.raises(ValueError, match="not an object key"):
        await store.read(key)
    with pytest.raises(ValueError, match="not an object key"):
        await store.remove(key)
    assert await store.list_objects() == []
    assert not (tmp_path / "outside.txt").exists()


@pytest.mark.parametrize(
    "key", ["x" * 255, "ä" * 127, "a/" * 511 + "b", "invoices/12/abcdef012345_Rechnung März (1).pdf"]
)
async def test_a_key_at_the_limits_is_held(store: ObjectStore, key: str) -> None:
    await store.save(key, b"x")
    assert await store.read(key) == b"x"


@pytest.mark.parametrize("prefix", ["/", "/feedback/", "../", "feedback//", "feedback/../", ".", "feedback\\"])
async def test_a_prefix_that_could_climb_is_refused(store: ObjectStore, prefix: str) -> None:
    with pytest.raises(ValueError, match="not an object key prefix"):
        await store.list_objects(prefix)


# --- the local store, on a real directory ----------------------------------------------------


async def test_the_local_store_keeps_each_object_at_its_key_under_the_root(tmp_path: Path) -> None:
    store = LocalObjectStore(str(tmp_path))
    await store.save(KEY, b"bytes", content_type="image/webp")
    assert (tmp_path / "feedback" / "1ed84e1bc3c7.webp").read_bytes() == b"bytes"
    # No half-written file is left beside it.
    assert sorted(path.name for path in (tmp_path / "feedback").iterdir()) == ["1ed84e1bc3c7.webp"]


async def test_a_failed_local_write_leaves_the_object_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bytes go to a temporary file first and replace the object in one rename, so a
    write that fails part way never leaves a truncated file under the key."""
    store = LocalObjectStore(tmp_path)
    await store.save(KEY, b"old")

    def full_disk(fd: int) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fsync", full_disk)
    with pytest.raises(ObjectStoreError, match="could not save") as caught:
        await store.save(KEY, b"new")
    assert isinstance(caught.value.__cause__, OSError) and caught.value.__cause__.errno == errno.ENOSPC
    monkeypatch.undo()
    assert await store.read(KEY) == b"old"
    assert sorted(path.name for path in (tmp_path / "feedback").iterdir()) == ["1ed84e1bc3c7.webp"]


async def test_the_local_store_cannot_hold_a_key_and_a_key_under_it(tmp_path: Path) -> None:
    """A file system's limit, not an object store's: ``a`` and ``a/b`` are a file and a
    directory of one name. Each way round is a failure, and no temporary file is left."""
    store = LocalObjectStore(tmp_path)
    await store.save("feedback/a/b.png", b"x")
    with pytest.raises(ObjectStoreError) as caught:
        await store.save("feedback/a", b"y")
    assert isinstance(caught.value.__cause__, IsADirectoryError)
    await store.save("support/c.png", b"z")
    with pytest.raises(ObjectStoreError):
        await store.save("support/c.png/d.png", b"z")
    assert list(tmp_path.rglob(".*")) == []
    # Neither is an object to read, and removing one is already done.
    for key in ("feedback/a", "support/c.png/d.png"):
        with pytest.raises(ObjectNotFoundError):
            await store.read(key)
        await store.remove(key)
    assert await store.read("feedback/a/b.png") == b"x"
    assert await store.read("support/c.png") == b"z"


async def test_a_local_read_or_remove_that_fails_is_a_store_failure_not_a_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalObjectStore(tmp_path)
    await store.save(KEY, b"x")

    def denied(self: Path, *args: Any, **kwargs: Any) -> Any:
        raise PermissionError(errno.EACCES, "Permission denied", str(self))

    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(ObjectStoreError, match="could not read") as read:
        await store.read(KEY)
    assert not isinstance(read.value, ObjectNotFoundError)
    assert isinstance(read.value.__cause__, PermissionError)

    monkeypatch.setattr(Path, "unlink", denied)
    with pytest.raises(ObjectStoreError, match="could not remove") as removed:
        await store.remove(KEY)
    assert isinstance(removed.value.__cause__, PermissionError)

    monkeypatch.setattr(Path, "rglob", denied)
    with pytest.raises(ObjectStoreError, match="could not list"):
        await store.list_objects("feedback/")


async def test_the_local_listing_names_only_objects_the_store_could_have_written(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    await store.save(KEY, b"x")
    folder = tmp_path / "feedback"
    (folder / ".a1b2c3.part").write_bytes(b"half")  # a write in flight
    (folder / ".hidden").mkdir()
    (folder / ".hidden" / "x.png").write_bytes(b"x")
    (folder / "sub").mkdir()  # a directory is no object
    (folder / "gone.webp").symlink_to(tmp_path / "nowhere")  # a link to nothing
    with open(os.fsencode(folder) + b"/\xff.txt", "wb") as file:  # no UTF-8 name, no key
        file.write(b"x")
    stamp = 1_762_000_000
    os.utime(folder / "1ed84e1bc3c7.webp", (stamp, stamp))
    assert await store.list_objects("feedback/") == [StoredObject(KEY, datetime.fromtimestamp(stamp, UTC))]


async def test_a_local_store_with_no_root_yet_lists_nothing(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "not-yet")
    assert await store.list_objects() == []
    with pytest.raises(ObjectNotFoundError):
        await store.read(KEY)
    await store.remove(KEY)
    await store.save(KEY, b"x")
    assert (tmp_path / "not-yet" / "feedback" / "1ed84e1bc3c7.webp").is_file()


# --- Cloud Storage -------------------------------------------------------------------------


async def test_gcs_stores_the_type_and_calls_the_client_off_the_event_loop() -> None:
    client = _GcsClient()
    store = GcsObjectStore(BUCKET, client=client)
    await store.save(KEY, b"x", content_type="image/webp")
    await store.save("feedback/raw.bin", b"y")
    objects = client.buckets[BUCKET].objects
    assert objects[KEY][1] == "image/webp"
    assert objects["feedback/raw.bin"][1] == "application/octet-stream"
    await store.read(KEY)
    await store.list_objects("feedback/")
    await store.remove(KEY)
    assert [op for op, _ in client.calls] == ["upload", "upload", "download", "list", "delete"]
    assert threading.get_ident() not in {thread for _, thread in client.calls}


@pytest.mark.parametrize(
    ("op", "error"),
    [
        ("upload", gcs_errors.Forbidden("storage.objects.create denied")),
        ("download", gcs_errors.Forbidden("storage.objects.get denied")),
        ("download", gcs_errors.ServiceUnavailable("backend error")),
        ("download", ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")),
        ("delete", gcs_errors.TooManyRequests("rate")),
        ("list", gcs_errors.Forbidden("storage.objects.list denied")),
    ],
)
async def test_every_gcs_failure_is_an_object_store_error_with_the_cause_chained(op: str, error: Exception) -> None:
    client = _GcsClient()
    store = GcsObjectStore(BUCKET, client=client)
    await store.save(KEY, b"x")
    client.bucket(BUCKET).fail[op] = error
    calls: dict[str, Callable[[], Awaitable[object]]] = {
        "upload": lambda: store.save(KEY, b"y"),
        "download": lambda: store.read(KEY),
        "delete": lambda: store.remove(KEY),
        "list": lambda: store.list_objects("feedback/"),
    }
    with pytest.raises(ObjectStoreError) as caught:
        await calls[op]()
    assert type(caught.value) is ObjectStoreError, "a failure, never the 404"
    assert caught.value.__cause__ is error


async def test_the_gcs_listing_skips_names_that_are_no_key() -> None:
    """A folder placeholder the console makes (``feedback/``) and a file no app writes
    (``.keep``) are not handed to a sweep that would then fail to remove them."""
    client = _GcsClient()
    store = GcsObjectStore(BUCKET, client=client)
    await store.save(KEY, b"x")
    stamp = datetime(2026, 11, 12, 9, 30, tzinfo=UTC)
    for name in ("feedback/", "feedback/.keep", "feedback//double.png"):
        client.bucket(BUCKET).objects[name] = (b"", "text/plain", stamp)
    assert [item.key for item in await store.list_objects("feedback/")] == [KEY]


async def test_gcs_builds_its_client_keyless_once_on_first_use(monkeypatch: pytest.MonkeyPatch) -> None:
    """No key and no client given: ``storage.Client()`` with no arguments, which reads
    Application Default Credentials — on Cloud Run, the runtime service account. Built in
    the worker on the first call, once, however many calls race for it."""
    built: list[tuple[tuple[object, ...], dict[str, object]]] = []
    client = _GcsClient()

    def factory(*args: object, **kwargs: object) -> _GcsClient:
        built.append((args, kwargs))
        time.sleep(0.01)
        return client

    monkeypatch.setattr(google.cloud.storage, "Client", factory)
    store = GcsObjectStore(BUCKET)
    assert built == [], "nothing is built before the first call"
    client.bucket(BUCKET).objects[KEY] = (b"x", "image/webp", datetime.now(UTC))
    assert await asyncio.gather(*(store.read(KEY) for _ in range(8))) == [b"x"] * 8
    assert built == [((), {})]


async def test_gcs_without_credentials_is_a_store_failure_and_tried_again(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []
    client = _GcsClient()

    def factory() -> _GcsClient:
        attempts.append(1)
        if len(attempts) == 1:
            raise google.auth.exceptions.DefaultCredentialsError("Your default credentials were not found.")
        return client

    monkeypatch.setattr(google.cloud.storage, "Client", factory)
    store = GcsObjectStore(BUCKET)
    with pytest.raises(ObjectStoreError, match="default credentials") as caught:
        await store.save(KEY, b"x")
    assert isinstance(caught.value.__cause__, google.auth.exceptions.DefaultCredentialsError)
    await store.save(KEY, b"x")
    assert len(attempts) == 2 and await store.read(KEY) == b"x"


def test_gcs_needs_a_bucket_name() -> None:
    with pytest.raises(ValueError, match="bucket"):
        GcsObjectStore("", client=_GcsClient())


def test_without_google_cloud_storage_the_store_says_which_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """``None`` in ``sys.modules`` makes the import fail as if it were not installed."""
    monkeypatch.setitem(sys.modules, "google.cloud.storage", None)
    with pytest.raises(ImportError, match=r"install the kit with its gcs extra, eifi1-server-kit\[gcs\]"):
        GcsObjectStore(BUCKET, client=_GcsClient())


# --- S3 (minio) ----------------------------------------------------------------------------


async def test_s3_puts_the_type_and_length_and_calls_the_client_off_the_event_loop() -> None:
    stub = _MinioStub()
    store = _s3(stub)
    await store.save(KEY, b"abc", content_type="image/webp")
    await store.save("feedback/raw.bin", b"y")
    assert stub.objects[KEY][:2] == (b"abc", "image/webp")
    assert stub.objects["feedback/raw.bin"][1] == "application/octet-stream"
    await store.read(KEY)
    await store.list_objects("feedback/")
    await store.list_objects()
    await store.remove(KEY)
    assert [op for op, _ in stub.calls] == ["put", "put", "get", "list", "list", "remove"]
    assert threading.get_ident() not in {thread for _, thread in stub.calls}
    # Recursive, so every key under the prefix and no "directory"; no prefix is None.
    assert stub.listings == [{"prefix": "feedback/", "recursive": True}, {"prefix": None, "recursive": True}]


async def test_s3_closes_and_releases_every_response() -> None:
    stub = _MinioStub()
    store = _s3(stub)
    await store.save(KEY, b"abc")
    assert await store.read(KEY) == b"abc"
    stub.broken_body = ProtocolError("Connection broken: IncompleteRead")
    with pytest.raises(ObjectStoreError) as caught:
        await store.read(KEY)
    assert isinstance(caught.value.__cause__, ProtocolError)
    assert [(response.closed, response.released) for response in stub.responses] == [(True, True), (True, True)]


@pytest.mark.parametrize("code", ["NoSuchKey", "NoSuchBucket", "NotFound"])
async def test_s3s_absent_codes_are_the_404_and_an_already_removed_object(code: str) -> None:
    """keksdose's ``_ABSENT_CODES``: GCS behind the S3 API answers a DELETE of a missing key
    with 404 ``NoSuchKey`` where AWS answers 204."""
    stub = _MinioStub()
    store = _s3(stub)
    stub.fail = {"get": _s3_error(code), "remove": _s3_error(code)}
    with pytest.raises(ObjectNotFoundError) as caught:
        await store.read(KEY)
    assert isinstance(caught.value.__cause__, S3Error)
    await store.remove(KEY)


@pytest.mark.parametrize(
    ("op", "error"),
    [
        ("put", _s3_error("AccessDenied")),
        ("get", _s3_error("AccessDenied")),
        ("remove", _s3_error("SignatureDoesNotMatch")),
        ("list", MaxRetryError(cast(Any, None), "/", reason=None)),
    ],
)
async def test_every_s3_failure_is_an_object_store_error_with_the_cause_chained(op: str, error: Exception) -> None:
    """minio's ``S3Error`` and urllib3's errors are no ``OSError`` (keksdose ``_as_os_error``):
    here every one is, so the app's one ``except`` fires in production too."""
    stub = _MinioStub()
    store = _s3(stub)
    await store.save(KEY, b"x")
    stub.fail[op] = error
    calls: dict[str, Callable[[], Awaitable[object]]] = {
        "put": lambda: store.save(KEY, b"y"),
        "get": lambda: store.read(KEY),
        "remove": lambda: store.remove(KEY),
        "list": lambda: store.list_objects("feedback/"),
    }
    with pytest.raises(ObjectStoreError) as caught:
        await calls[op]()
    assert type(caught.value) is ObjectStoreError and caught.value.__cause__ is error
    assert not isinstance(error, OSError), "the reason the mapping exists"


async def test_the_s3_listing_skips_entries_that_are_no_key() -> None:
    stub = _MinioStub()
    store = _s3(stub)
    await store.save(KEY, b"x")
    stamp = datetime(2026, 11, 12, 9, 30, tzinfo=UTC)
    stub.extra = [
        _S3Listed("feedback/", stamp),
        _S3Listed("feedback/.keep", stamp),
        _S3Listed("feedback/no-date.png", None),
        _S3Listed(None, stamp),
    ]
    assert [item.key for item in await store.list_objects("feedback/")] == [KEY]
    assert [item.key for item in await store.list_objects()] == [KEY]


class _MinioRecorder:
    made: ClassVar[list[tuple[str, dict[str, object]]]] = []

    def __init__(self, endpoint: str, **kwargs: object) -> None:
        _MinioRecorder.made.append((endpoint, kwargs))


@pytest.mark.parametrize(
    ("endpoint", "host", "secure"),
    [
        ("https://storage.googleapis.com", "storage.googleapis.com", True),
        ("https://storage.googleapis.com/", "storage.googleapis.com", True),
        ("http://127.0.0.1:9000", "127.0.0.1:9000", False),
    ],
)
def test_s3_builds_minio_from_the_endpoint_as_keksdose_does(
    monkeypatch: pytest.MonkeyPatch, endpoint: str, host: str, secure: bool
) -> None:
    _MinioRecorder.made = []
    monkeypatch.setattr(minio, "Minio", _MinioRecorder)
    S3ObjectStore(BUCKET, endpoint=endpoint, access_key="GOOG1EXAMPLE", secret_key="secret", region="europe-west4")
    S3ObjectStore(BUCKET, endpoint=endpoint, access_key="", secret_key="")
    assert _MinioRecorder.made == [
        (host, {"access_key": "GOOG1EXAMPLE", "secret_key": "secret", "secure": secure, "region": "europe-west4"}),
        (host, {"access_key": None, "secret_key": None, "secure": secure, "region": None}),
    ]


def test_s3_builds_a_real_minio_client_without_a_request() -> None:
    store = S3ObjectStore(BUCKET, endpoint="http://127.0.0.1:9", access_key="keksdose", secret_key="keksdose")
    assert isinstance(store, S3ObjectStore)


@pytest.mark.parametrize(
    "endpoint",
    [
        "storage.googleapis.com",
        "ftp://storage.googleapis.com",
        "https://",
        "https://storage.googleapis.com/bucket",
        "https://storage.googleapis.com?x=1",
        "https://storage.googleapis.com#x",
        "https://key:secret@storage.googleapis.com",
    ],
)
def test_s3_refuses_an_endpoint_that_is_no_http_origin(endpoint: str) -> None:
    with pytest.raises(ValueError, match="not an S3 endpoint"):
        S3ObjectStore(BUCKET, endpoint=endpoint)


def test_s3_takes_an_endpoint_or_a_client_and_a_bucket() -> None:
    with pytest.raises(ValueError, match="an endpoint or a client"):
        S3ObjectStore(BUCKET)
    with pytest.raises(ValueError, match="not both"):
        S3ObjectStore(BUCKET, endpoint="https://storage.googleapis.com", client=cast(Minio, _MinioStub()))
    with pytest.raises(ValueError, match="bucket"):
        S3ObjectStore("", client=cast(Minio, _MinioStub()))


def test_without_minio_the_store_says_which_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "minio", None)
    with pytest.raises(ImportError, match=r"install the kit with its s3 extra, eifi1-server-kit\[s3\]"):
        S3ObjectStore(BUCKET, client=cast(Minio, _MinioStub()))


def test_importing_the_store_loads_no_cloud_client() -> None:
    """Both clients are imported lazily, by their store: an app on the local store, or on one
    of the two, installs and loads nothing of the other."""
    code = (
        "import sys; import eifi1_server_kit.objects; "
        "loaded = sorted(name for name in sys.modules if name.split('.')[0] in {'google', 'minio', 'urllib3'}); "
        "assert loaded == [], loaded"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# --- the purge -----------------------------------------------------------------------------


class _CountingStore:
    """A store whose ``remove`` counts what is in flight, fails for the keys named and, as
    every backend, refuses a key that climbs with a ``ValueError``."""

    def __init__(self, failing: Iterable[str] = ()) -> None:
        self.failing = set(failing)
        self.removed: list[str] = []
        self.in_flight = 0
        self.most = 0

    async def save(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        raise AssertionError("not called")

    async def read(self, key: str) -> bytes:
        raise AssertionError("not called")

    async def list_objects(self, prefix: str = "") -> list[StoredObject]:
        raise AssertionError("not called")

    async def remove(self, key: str) -> None:
        self.in_flight += 1
        self.most = max(self.most, self.in_flight)
        try:
            await asyncio.sleep(0.001)
            if key.startswith(".."):
                raise ValueError(f"not an object key: {key!r}")
            if key in self.failing:
                raise ObjectStoreError(f"could not remove {key!r}")
            self.removed.append(key)
        finally:
            self.in_flight -= 1


async def test_the_purge_removes_each_key_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    store = LocalObjectStore(tmp_path)
    for key in ("feedback/a.png", "feedback/b.png"):
        await store.save(key, b"x")
    with caplog.at_level(logging.INFO, logger="eifi1_server_kit.objects"):
        failed = await purge_objects(
            store, ["feedback/a.png", "", "feedback/b.png", "feedback/a.png", "feedback/gone.png"], reason="erasure"
        )
    assert failed == []
    assert await store.list_objects() == []
    assert [record.getMessage() for record in caplog.records] == ["purge (erasure): removed 3 object(s)"]


async def test_the_purge_never_raises_and_names_every_key_it_left(caplog: pytest.LogCaptureFixture) -> None:
    store = _CountingStore(failing={"feedback/b.png", "feedback/d.png"})
    keys = ["feedback/d.png", "feedback/a.png", "../climbs.png", "feedback/b.png", "feedback/c.png"]
    with caplog.at_level(logging.INFO, logger="eifi1_server_kit.objects"):
        failed = await purge_objects(store, keys, reason="feedback_unfiled_upload")
    # In the order given; a key no store could hold is one it left, not a raise.
    assert failed == ["feedback/d.png", "../climbs.png", "feedback/b.png"]
    assert sorted(store.removed) == ["feedback/a.png", "feedback/c.png"]
    messages = [record.getMessage() for record in caplog.records]
    assert sorted(messages[:3]) == [
        "purge (feedback_unfiled_upload): could not remove '../climbs.png'",
        "purge (feedback_unfiled_upload): could not remove 'feedback/b.png'",
        "purge (feedback_unfiled_upload): could not remove 'feedback/d.png'",
    ]
    assert all(record.exc_info for record in caplog.records[:3]), "each with its exception"
    assert messages[3] == (
        "purge (feedback_unfiled_upload): 3 of 5 object(s) left in the store, remove them by hand: "
        "feedback/d.png, ../climbs.png, feedback/b.png"
    )
    assert caplog.records[3].levelno == logging.ERROR


async def test_the_purge_keeps_at_most_its_concurrency_in_flight() -> None:
    store = _CountingStore()
    assert await purge_objects(store, [f"feedback/{n}.png" for n in range(10)], reason="wipe", concurrency=3) == []
    assert store.most == 3 and len(store.removed) == 10
    store = _CountingStore()
    await purge_objects(store, [f"feedback/{n}.png" for n in range(40)], reason="wipe")
    assert store.most == PURGE_CONCURRENCY == 16


async def test_a_purge_of_nothing_asks_nothing(caplog: pytest.LogCaptureFixture) -> None:
    store = _CountingStore()
    with caplog.at_level(logging.INFO, logger="eifi1_server_kit.objects"):
        assert await purge_objects(store, ["", ""], reason="wipe") == []
        assert await purge_objects(store, iter(()), reason="wipe") == []
    assert store.removed == [] and caplog.records == []
    with pytest.raises(ValueError, match="concurrency"):
        await purge_objects(store, ["feedback/a.png"], reason="wipe", concurrency=0)
