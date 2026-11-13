"""The apps' stored files in an object store: one port, three backends, one purge (0.26).

keksdose keeps its uploads — receipts, transaction and feedback attachments, support files —
behind ``InvoiceFileStore`` (``adapters/external/invoice_file_store.py:109``): the local disk
in development, an S3-compatible store in production (minio against
storage.googleapis.com with an HMAC pair). Kurvenschmiede moves its feedback attachments out
of its database into a Cloud Storage bucket reached without a key, and the store is written
once, here (Marcel, 2026-11-12: "mehr im ui und server kit sharen"):

* :class:`ObjectStore` — the port: ``save``, ``read``, ``remove``, ``list_objects``, and
  (0.27) ``exists`` and ``check``, each async. An app's adapters take one: its feedback
  :class:`~eifi1_server_kit.feedback.AttachmentStorage`, its sweep, its erasure, its
  readiness probe;
* :class:`LocalObjectStore` — files under a root directory: development and tests;
* :class:`GcsObjectStore` — a Cloud Storage bucket through Google's own client and
  Application Default Credentials, on Cloud Run the runtime service account: no key to
  keep or rotate (the ``gcs`` extra);
* :class:`S3ObjectStore` — any S3 API through minio, keksdose's production path today (the
  ``s3`` extra), kept until keksdose's move is decided;
* :func:`purge_objects` — many removes, bounded, never raising, the keys it left returned.

**The app mints the key.** The store takes any key the rule below accepts and makes none:
keksdose's ``feedback/<sha256[:12]>.<ext>``, Kurvenschmiede's keyed digest
``feedback/1ed84e1bc3c7.webp`` and kastlan's ``feedback/<uuid32>_<sha12>.<ext>`` stay each
app's own, as the feedback port says (``AttachmentStorage``). There is no locator beside the
key: the key is the one name of an object on every backend.

**Stored as given.** No backend seals a file: the bytes saved are the bytes in the bucket,
under the provider's encryption at rest (Marcel, 2026-11-12). An app that seals its files
(kastlan's ``at_rest``) does so before ``save`` and after ``read``.

**A key** is what every backend can hold under one name, so a key that works on the local
disk works in the bucket: 1 to :data:`MAX_KEY_BYTES` bytes of UTF-8 (Cloud Storage's and
S3's limit), split on ``/`` into segments of at most :data:`MAX_SEGMENT_BYTES` bytes (a file
name's limit on disk: keksdose's ENAMETOOLONG, ``key_file_name``), none empty (no leading,
trailing or double ``/``), none starting with a dot (no ``.``, no ``..``, no hidden name:
nothing climbs out of the local root, and the local store's half-written files are its own),
and no backslash or control character. Anything else is a :class:`ValueError` before any
store is asked. A listing names only such keys, so a sweep is never handed one it cannot
remove (a folder placeholder ``feedback/``, a ``.keep``).

**One failure type.** Every failure is an :class:`ObjectStoreError`, an :class:`OSError`,
with the backend's own exception chained: minio's ``S3Error`` and urllib3's errors are no
``OSError``, so keksdose's ``except OSError`` could not fire in production until
``_as_os_error`` (``invoice_file_store.py:211``) relabelled them. A missing object on
``read`` is an :class:`ObjectNotFoundError`, a :class:`FileNotFoundError` too: the app's
404. ``remove`` of a missing object is no failure: what it promises holds.

**No bucket is made behind the app's back** (0.27). ``check`` probes the store for a
readiness probe and makes nothing; ``ensure_bucket``, on the local and the S3 store only, makes
the root or the bucket when the app asks — a development MinIO's, as keksdose's made its own
on the first upload. No save ever does: in production the deploy makes the bucket.

**Calls run in a worker thread.** Google's and minio's clients are synchronous, so each call
runs in :func:`asyncio.to_thread`, as keksdose's do; so does the local store's file I/O. Build
one store per process and share it: each holds its client and that client's connection pool
(keksdose ``_minio_client``, ``invoice_file_store.py:247``: a store per call was a TLS
handshake per attachment).
"""

from __future__ import annotations

import asyncio
import errno
import importlib
import io
import logging
import os
import tempfile
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from operator import attrgetter
from pathlib import Path
from stat import S_ISREG
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from google.cloud.storage import Bucket, Client
    from minio import Minio

__all__ = [
    "MAX_KEY_BYTES",
    "MAX_SEGMENT_BYTES",
    "PURGE_CONCURRENCY",
    "GcsObjectStore",
    "LocalObjectStore",
    "ObjectNotFoundError",
    "ObjectStore",
    "ObjectStoreError",
    "S3ObjectStore",
    "StoredObject",
    "purge_objects",
]

logger = logging.getLogger("eifi1_server_kit.objects")

#: The most bytes of UTF-8 in a key: Cloud Storage's and S3's own limit on an object name.
MAX_KEY_BYTES = 1024
#: The most bytes of UTF-8 in one segment of a key: a file name's limit on disk (NAME_MAX),
#: which the local store's last segment is.
MAX_SEGMENT_BYTES = 255
#: How many removes :func:`purge_objects` keeps in flight by default (keksdose
#: ``_PURGE_CONCURRENCY``, ``invoice_file_store.py:62``): a wipe is hundreds of objects and
#: each a round trip, so not one at a time; and each a worker thread, so not all at once.
PURGE_CONCURRENCY = 16

#: The type an object is stored with when ``save`` is given none.
_OCTET_STREAM = "application/octet-stream"
#: minio's error codes for "no such object" (keksdose ``_ABSENT_CODES``,
#: ``invoice_file_store.py:68``): Cloud Storage behind the S3 API answers a DELETE of a
#: missing key with 404 ``NoSuchKey`` where AWS answers 204. On Cloud Storage's own API a
#: missing object and a missing bucket are one ``NotFound`` too, so both backends read a
#: missing bucket as a missing object.
_ABSENT_CODES = frozenset({"NoSuchKey", "NoSuchBucket", "NotFound"})


class ObjectStoreError(OSError):
    """The store could not do what it was asked: unreachable, refused, no credentials, a
    file system's error. The backend's own exception is chained as ``__cause__``. An
    :class:`OSError`, so an app's one ``except OSError`` (keksdose's extraction and statement
    pollers, its feedback download) holds for every backend."""


class ObjectNotFoundError(ObjectStoreError, FileNotFoundError):
    """``read`` found no object under the key: the app's 404. ``errno`` is ``ENOENT`` and
    ``filename`` the key, as a missing file's are."""


def _not_found(key: str) -> ObjectNotFoundError:
    return ObjectNotFoundError(errno.ENOENT, "No such object", key)


# --- the key ----------------------------------------------------------------------------------


def _key_problem(key: str) -> str | None:
    """What makes ``key`` no key every backend can hold, or ``None`` (the module docstring)."""
    try:
        size = len(key.encode())
    except UnicodeEncodeError:
        return "not UTF-8"
    if not 0 < size <= MAX_KEY_BYTES:
        return f"empty, or over {MAX_KEY_BYTES} bytes"
    for segment in key.split("/"):
        if not segment:
            return "an empty segment: a leading, trailing or double /"
        if segment.startswith("."):
            return "a segment starting with a dot"
        if len(segment.encode()) > MAX_SEGMENT_BYTES:
            return f"a segment over {MAX_SEGMENT_BYTES} bytes"
        if "\\" in segment or any(ord(char) < 0x20 or ord(char) == 0x7F for char in segment):
            return "a backslash or a control character"
    return None


def _check_key(key: str) -> str:
    problem = _key_problem(key)
    if problem is not None:
        raise ValueError(f"not an object key: {key!r} ({problem})")
    return key


def _check_prefix(prefix: str) -> str:
    """A listing's prefix: empty for everything, else a key, perhaps ending in ``/``."""
    problem = _key_problem(prefix.removesuffix("/")) if prefix else None
    if problem is not None:
        raise ValueError(f"not an object key prefix: {prefix!r} ({problem})")
    return prefix


async def _in_thread[T](action: str, key: str, call: Callable[[], T]) -> T:
    """``call`` in a worker thread, every failure an :class:`ObjectStoreError` with its cause
    chained (keksdose ``_as_os_error``). Deliberately broad: an ``OSError`` too, so
    ``except ObjectStoreError`` is every failure of every backend."""

    def guarded() -> T:
        try:
            return call()
        except ObjectStoreError:
            raise
        except Exception as exc:
            raise ObjectStoreError(f"The object store could not {action} {key!r}: {exc}") from exc

    return await asyncio.to_thread(guarded)


def _require(module: str, store: str, package: str, extra: str) -> None:
    try:
        importlib.import_module(module)
    except ImportError as exc:
        raise ImportError(
            f"{store} needs {package}: install the kit with its {extra} extra, eifi1-server-kit[{extra}]."
        ) from exc


# --- the port ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredObject:
    """One object :meth:`ObjectStore.list_objects` found (keksdose ``StoredObject``,
    ``invoice_file_store.py:98``)."""

    #: Its key, as ``save`` took it.
    key: str
    #: When it was last written, aware UTC: a sweep's age (keksdose's day for an upload no
    #: row names yet). Saving the same key again moves it.
    modified: datetime


class ObjectStore(Protocol):
    """Where an app's files live, by the key the app minted. Every method is async and runs
    the backend in a worker thread; a key the rule refuses is a :class:`ValueError` before
    anything is asked; every failure is an :class:`ObjectStoreError`."""

    async def save(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        """Store ``data`` under ``key``, replacing whatever was there. ``content_type`` is
        kept where the backend keeps one (``application/octet-stream`` without it); the
        local store keeps none, and an app that serves the type keeps it on its row or reads
        it off the key's extension."""

    async def read(self, key: str) -> bytes:
        """The object's bytes; :class:`ObjectNotFoundError` when there is none."""

    async def remove(self, key: str) -> None:
        """Delete the object, raising :class:`ObjectStoreError` when the store could not. An
        object already absent is no error: the delete's postcondition holds (keksdose
        ``InvoiceFileStore.remove``)."""

    async def list_objects(self, prefix: str = "") -> list[StoredObject]:
        """Every object whose key starts with ``prefix`` (a string's start, as an object
        store's prefix is: ``feedback/1e`` finds ``feedback/1ed84e1bc3c7.webp``), in key
        order. For a sweep: an object no row names has no name anywhere but the store."""

    async def exists(self, key: str) -> bool:
        """Is there an object under ``key``? (0.27; keksdose ``object_exists``,
        ``invoice_file_store.py:127``.) Its metadata, never its bytes: asked before a row is
        hung on a key the client sent, it must not cost what serving the file does.
        ``False`` means absent, and only that: a store that cannot say raises
        :class:`ObjectStoreError` (keksdose's swallowed every failure, and an outage told a
        user their own upload did not exist)."""

    async def check(self) -> None:
        """Raise :class:`ObjectStoreError` unless the store can be reached and its bucket (the
        local root) is there to use (0.27; keksdose ``healthcheck``): one cheap request, for
        a readiness probe or an admin page. It reads no object's bytes, writes nothing and
        makes nothing."""


# --- the local disk -----------------------------------------------------------------------------


class LocalObjectStore:
    """Objects as files under ``root``, each at its key's path (keksdose
    ``LocalInvoiceFileStore``, ``invoice_file_store.py:148``): development and tests. The root
    is made on the first save.

    A key cannot leave the root: the rule refuses ``..``, a leading ``/`` and a backslash
    before any path is built. A write goes to a temporary file in the object's directory and
    replaces the object in one rename, so a reader sees the old bytes or the new, never half
    a file, and a write that fails leaves the object as it was. The files are its owner's
    alone (``0600``), and no type is kept. A file system cannot hold ``a`` and ``a/b`` at
    once, as a bucket can; saving the second is an :class:`ObjectStoreError`.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root)

    def _path(self, key: str) -> Path:
        return self._root.joinpath(*_check_key(key).split("/"))

    async def save(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        path = self._path(key)

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Named with a leading dot: no key is, so a listing never names it.
            descriptor, temporary = tempfile.mkstemp(prefix=".", suffix=".part", dir=path.parent)
            try:
                with os.fdopen(descriptor, "wb") as file:
                    file.write(data)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, path)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise

        await _in_thread("save", key, write)

    async def read(self, key: str) -> bytes:
        path = self._path(key)

        def load() -> bytes:
            try:
                return path.read_bytes()
            except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
                raise _not_found(key) from exc

        return await _in_thread("read", key, load)

    async def remove(self, key: str) -> None:
        path = self._path(key)

        def unlink() -> None:
            try:
                path.unlink()
            except FileNotFoundError, IsADirectoryError, NotADirectoryError:
                return  # no object at the key: already removed

        await _in_thread("remove", key, unlink)

    async def list_objects(self, prefix: str = "") -> list[StoredObject]:
        root = self._root
        # The prefix's directory part is walked and the rest matched on the key, as an object
        # store's prefix matches (keksdose ``LocalInvoiceFileStore.list_objects``).
        directory = root.joinpath(*_check_prefix(prefix).rpartition("/")[0].split("/"))

        def walk() -> list[StoredObject]:
            if not directory.is_dir():
                return []
            found: list[StoredObject] = []
            for path in directory.rglob("*"):
                key = path.relative_to(root).as_posix()
                if not key.startswith(prefix) or _key_problem(key) is not None:
                    continue
                try:
                    status = path.stat()
                except FileNotFoundError:
                    continue  # removed while the walk ran, or a link to nothing
                if S_ISREG(status.st_mode):
                    found.append(StoredObject(key, datetime.fromtimestamp(status.st_mtime, UTC)))
            return sorted(found, key=attrgetter("key"))

        return await _in_thread("list", prefix, walk)

    async def exists(self, key: str) -> bool:
        path = self._path(key)

        def regular() -> bool:
            try:
                return S_ISREG(path.stat().st_mode)
            except FileNotFoundError, NotADirectoryError:
                return False

        return await _in_thread("find", key, regular)

    async def check(self) -> None:
        """The root, or before the first save has made it the nearest directory above it, is
        a directory this process may write in: the first save can succeed. Makes nothing."""
        root = self._root

        def probe() -> None:
            path = root
            while not path.exists() and path != path.parent:
                path = path.parent
            if not path.is_dir():
                raise NotADirectoryError(errno.ENOTDIR, "Not a directory", str(path))
            if not os.access(path, os.W_OK | os.X_OK):
                raise ObjectStoreError(errno.EACCES, "The local store's root is not writable", str(path))

        await _in_thread("check", str(root), probe)

    async def ensure_bucket(self) -> None:
        """Make the root now (0.27), for a development setup that wants it before the first
        save; a save makes it anyway. A file in its place is an :class:`ObjectStoreError`."""
        root = self._root
        await _in_thread("make the bucket", str(root), lambda: root.mkdir(parents=True, exist_ok=True))


# --- Cloud Storage ----------------------------------------------------------------------------


class GcsObjectStore:
    """A Cloud Storage bucket through Google's own client (the ``gcs`` extra,
    ``google-cloud-storage``), with no key: without ``client``, the first call builds
    ``storage.Client()``, which reads Application Default Credentials — on Cloud Run the
    service's runtime account, locally ``gcloud auth application-default login``. Its project
    is the environment's; the store's calls name the bucket and need none. Built once, in the
    worker, however many calls race for it; a build that fails (no credentials) is an
    :class:`ObjectStoreError`, and the next call tries again.

    The account needs ``roles/storage.objectUser`` on the bucket and nothing on the bucket
    itself: the store never creates, configures or deletes a bucket (Kurvenschmiede
    ``deploy/gcp/README.md``, "The uploads bucket"), so it has no ``ensure_bucket``, and
    ``check`` lists one object rather than reading the bucket's metadata, which that role
    does not grant. ``client`` is an app's own
    ``storage.Client`` (another project, an emulator) or a test's stub; the client's own
    retry and timeout defaults apply.
    """

    def __init__(self, bucket: str, *, client: Client | None = None) -> None:
        _require("google.cloud.storage", "GcsObjectStore", "google-cloud-storage", "gcs")
        if not bucket:
            raise ValueError("GcsObjectStore needs a bucket name")
        self._name = bucket
        self._client = client
        self._bucket: Bucket | None = None
        self._lock = threading.Lock()

    def _handle(self) -> Bucket:
        """The bucket's handle (no request), with the client built on the first call."""
        with self._lock:
            if self._bucket is None:
                if self._client is None:
                    from google.cloud.storage import Client

                    self._client = Client()
                self._bucket = self._client.bucket(self._name)
            return self._bucket

    async def save(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        _check_key(key)

        def upload() -> None:
            # The client's own default type is text/plain: always say one.
            self._handle().blob(key).upload_from_string(data, content_type=content_type or _OCTET_STREAM)

        await _in_thread("save", key, upload)

    async def read(self, key: str) -> bytes:
        _check_key(key)

        def download() -> bytes:
            from google.api_core.exceptions import NotFound

            try:
                data: bytes = self._handle().blob(key).download_as_bytes()
            except NotFound as exc:
                raise _not_found(key) from exc
            return data

        return await _in_thread("read", key, download)

    async def remove(self, key: str) -> None:
        _check_key(key)

        def delete() -> None:
            from google.api_core.exceptions import NotFound

            try:
                self._handle().blob(key).delete()
            except NotFound:
                return  # already gone

        await _in_thread("remove", key, delete)

    async def list_objects(self, prefix: str = "") -> list[StoredObject]:
        _check_prefix(prefix)

        def listing() -> list[StoredObject]:
            # The iterator pages through the listing itself, in this thread.
            blobs = self._handle().list_blobs(prefix=prefix or None)
            found = [StoredObject(blob.name, blob.updated) for blob in blobs if _key_problem(blob.name) is None]
            return sorted(found, key=attrgetter("key"))

        return await _in_thread("list", prefix, listing)

    async def exists(self, key: str) -> bool:
        _check_key(key)
        # The object's metadata; the client answers False for NotFound and raises the rest.
        return await _in_thread("find", key, lambda: bool(self._handle().blob(key).exists()))

    async def check(self) -> None:
        def probe() -> None:
            # At most one object (``storage.objects.list``); a missing bucket is NotFound.
            list(self._handle().list_blobs(max_results=1))

        await _in_thread("check", self._name, probe)


# --- S3 -----------------------------------------------------------------------------------------


def _minio(endpoint: str, *, access_key: str | None, secret_key: str | None, region: str | None) -> Minio:
    """keksdose's ``_minio_client`` (``invoice_file_store.py:247``), the scheme required: a
    bare host was plain HTTP there, and a store's traffic is a user's files."""
    from minio import Minio

    parts = urlsplit(endpoint)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or "@" in parts.netloc
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            f"not an S3 endpoint: {endpoint!r} (an http(s) origin, such as https://storage.googleapis.com)"
        )
    return Minio(
        parts.netloc,
        access_key=access_key or None,
        secret_key=secret_key or None,
        secure=parts.scheme == "https",
        region=region or None,
    )


class S3ObjectStore:
    """Any S3 API through minio (the ``s3`` extra), as keksdose's ``S3InvoiceFileStore``
    (``invoice_file_store.py:265``) reaches storage.googleapis.com with an HMAC pair today.

    ``endpoint`` is an http(s) origin — ``https://storage.googleapis.com``, a development
    MinIO's ``http://127.0.0.1:9000`` — with ``access_key`` and ``secret_key`` (blank: none)
    and ``region`` (``europe-west4`` spares minio asking the bucket where it is). Or
    ``client``, an app's own ``Minio`` or a test's stub, in place of the four. No save creates
    a bucket: a cloud bucket is made by its deploy, and a development MinIO's by
    :meth:`ensure_bucket` when the app asks (0.27), or once beside the container
    (``mc mb --ignore-existing``).
    """

    def __init__(
        self,
        bucket: str,
        *,
        endpoint: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str | None = None,
        client: Minio | None = None,
    ) -> None:
        _require("minio", "S3ObjectStore", "minio", "s3")
        if not bucket:
            raise ValueError("S3ObjectStore needs a bucket name")
        if client is None:
            if endpoint is None:
                raise ValueError("S3ObjectStore needs an endpoint or a client")
            client = _minio(endpoint, access_key=access_key, secret_key=secret_key, region=region)
        elif endpoint is not None:
            raise ValueError("S3ObjectStore takes an endpoint or a client, not both")
        self._bucket = bucket
        self._client = client

    async def save(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        _check_key(key)

        def put() -> None:
            self._client.put_object(
                self._bucket, key, io.BytesIO(data), length=len(data), content_type=content_type or _OCTET_STREAM
            )

        await _in_thread("save", key, put)

    async def read(self, key: str) -> bytes:
        _check_key(key)

        def get() -> bytes:
            from minio.error import S3Error

            try:
                response = self._client.get_object(self._bucket, key)
            except S3Error as exc:
                if exc.code in _ABSENT_CODES:
                    raise _not_found(key) from exc
                raise
            try:
                return response.read()
            finally:
                # Back to the pool, read or not (minio's own instruction for get_object).
                response.close()
                response.release_conn()

        return await _in_thread("read", key, get)

    async def remove(self, key: str) -> None:
        _check_key(key)

        def delete() -> None:
            from minio.error import S3Error

            try:
                self._client.remove_object(self._bucket, key)
            except S3Error as exc:
                if exc.code not in _ABSENT_CODES:
                    raise

        await _in_thread("remove", key, delete)

    async def list_objects(self, prefix: str = "") -> list[StoredObject]:
        _check_prefix(prefix)

        def listing() -> list[StoredObject]:
            # Recursive: every key under the prefix and no "directory"; minio pages itself.
            found = [
                StoredObject(item.object_name, item.last_modified)
                for item in self._client.list_objects(self._bucket, prefix=prefix or None, recursive=True)
                if item.object_name is not None
                and item.last_modified is not None
                and _key_problem(item.object_name) is None
            ]
            return sorted(found, key=attrgetter("key"))

        return await _in_thread("list", prefix, listing)

    async def exists(self, key: str) -> bool:
        _check_key(key)

        def stat() -> bool:
            from minio.error import S3Error

            try:
                self._client.stat_object(self._bucket, key)
            except S3Error as exc:
                if exc.code in _ABSENT_CODES:
                    return False
                raise
            return True

        return await _in_thread("find", key, stat)

    async def check(self) -> None:
        """keksdose's probe (``bucket_exists``, a HEAD on the bucket), with a missing bucket a
        failure: keksdose's passed it."""

        def probe() -> None:
            if not self._client.bucket_exists(self._bucket):
                raise ObjectStoreError(f"The object store has no bucket {self._bucket!r}")

        await _in_thread("check", self._bucket, probe)

    async def ensure_bucket(self) -> None:
        """Make the bucket when it is missing (0.27): for a development MinIO, called by the
        app at its start, never by a save (keksdose made its bucket on the first upload). A
        bucket another process made first is no failure."""

        def make() -> None:
            from minio.error import S3Error

            if self._client.bucket_exists(self._bucket):
                return
            try:
                self._client.make_bucket(self._bucket)
            except S3Error as exc:
                if exc.code != "BucketAlreadyOwnedByYou":
                    raise

        await _in_thread("make the bucket", self._bucket, make)


# --- the purge --------------------------------------------------------------------------------


async def purge_objects(
    store: ObjectStore, keys: Iterable[str], *, reason: str, concurrency: int = PURGE_CONCURRENCY
) -> list[str]:
    """Remove many objects; return the keys left behind, in the order given (keksdose
    ``purge_objects``, ``invoice_file_store.py:404``).

    For a row's files once its delete committed — a wipe's hundreds, an erasure's, a sweep's
    orphans — when nothing but the key names the file any more, so two rules hold:

    * **Nothing raises.** A store outage must not turn "erase everything" into a 500 over a
      database already emptied. A key that fails, a refused key included, is collected.
    * **Every key left is in the log**, each with its exception and then all in one line
      with ``reason``: they are the instructions for removing them by hand.

    At most ``concurrency`` removes are in flight. Duplicates are asked once and empty keys
    not at all. An app's own placeholders (keksdose's ``(pending)`` and ``(manual)``) are
    the app's to leave out; when to purge (keksdose's ``delete_after_commit``) stays the
    app's too.
    """
    if concurrency < 1:
        raise ValueError("purge_objects needs a concurrency of at least 1")
    wanted = [key for key in dict.fromkeys(keys) if key]
    if not wanted:
        return []
    limit = asyncio.Semaphore(concurrency)
    failed: set[str] = set()

    async def remove(key: str) -> None:
        async with limit:
            try:
                await store.remove(key)
            except Exception:
                failed.add(key)
                logger.error("purge (%s): could not remove %r", reason, key, exc_info=True)

    await asyncio.gather(*(remove(key) for key in wanted))
    left = [key for key in wanted if key in failed]
    if left:
        logger.error(
            "purge (%s): %d of %d object(s) left in the store, remove them by hand: %s",
            reason,
            len(left),
            len(wanted),
            ", ".join(left),
        )
    else:
        logger.info("purge (%s): removed %d object(s)", reason, len(wanted))
    return left
