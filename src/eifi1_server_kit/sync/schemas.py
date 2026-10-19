"""The ``/sync`` wire shapes (``docs/local-first-harmonization.md`` §1.5 D1, §1.6 in
``Eifi1/ui-kit``): keksdose's, which are canon, with kastlan's ``scope`` optional, the
device's edit time on a change (D2) and the per-change ``rejections`` beside the one
``refused`` (D1).

Pydantic only. Like the other kit schemas, meant to be used as they are or SUBCLASSED: no
model forbids extra fields, so keksdose adds its ``fx_*``, ``valuation_*`` and ``tenant_*``
fields and kastlan its ``pk``. A subclass narrows a type covariantly without a
``type: ignore``: ``id: uuid.UUID`` (keksdose) or ``id: str`` (kastlan's ``sync_uuid``) on
:class:`SyncChange`, ``changes: list[AppChange]`` on :class:`SyncRequest` and
:class:`SyncResponse` (their ``changes`` are a ``Sequence``), ``scope: Literal["handover"]``.

**The old shape stays accepted for one release** (D1, D8): a request without ``refusals``
or a change without ``updated_at`` parses, and the app answers it as before (the legacy
status codes, server-now).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Annotated, Any, Final, Literal

from pydantic import AfterValidator, AwareDatetime, BaseModel, BeforeValidator, Field

from eifi1_server_kit.sync.merge import as_utc

__all__ = [
    "SYNC_PLAN_LIMIT",
    "SYNC_REJECTED",
    "SyncChange",
    "SyncPageCursor",
    "SyncRefusal",
    "SyncRejection",
    "SyncRequest",
    "SyncResponse",
]

#: :attr:`SyncRejection.code` for a change a scope rule refuses: a fixed field, a finalised
#: protocol, an entity not writable in the scope (kastlan's ``EntityRule``).
SYNC_REJECTED: Final = "rejected"
#: :attr:`SyncRejection.code` for a create past the plan's limit (kastlan's photos over the
#: plan, today's 402 ``planRefusal``); billing's ``plan_limit`` code, the same text.
SYNC_PLAN_LIMIT: Final = "plan_limit"


def _id_text(value: object) -> object:
    return str(value) if isinstance(value, uuid.UUID) else value


_ChangeId = Annotated[str, BeforeValidator(_id_text), Field(min_length=1, max_length=200)]

#: An instant with a zone on the wire, stored in UTC; a naive value is a 422.
_EditTime = Annotated[AwareDatetime, AfterValidator(as_utc)]


class SyncChange(BaseModel):
    """One row's delta on the wire. ``data`` is the full row (JSON-safe: UUIDs and money as
    strings) for an upsert, and omitted for a delete."""

    entity: str
    #: The client-minted id of the row (keksdose ``id``, kastlan ``sync_uuid``): a UUID or
    #: text of 1-200 characters. The union is smart: JSON (``model_validate_json``) gives a
    #: ``UUID`` for a UUID's text, a Python dict (``model_validate``) keeps it a ``str``. An
    #: app whose lookups key on UUIDs (keksdose) narrows to ``id: uuid.UUID``.
    id: uuid.UUID | Annotated[str, Field(min_length=1, max_length=200)]
    op: Literal["upsert", "delete"]
    data: dict[str, Any] | None = None
    #: The device's edit time (D2), the LWW stamp of an upsert; ``None`` (an older client)
    #: falls back to server-now for one release. Shifted by the push's clock skew
    #: (:attr:`SyncRequest.client_now`), then a future stamp is clamped
    #: (:func:`~eifi1_server_kit.sync.clamp_stamp`).
    updated_at: _EditTime | None = None
    #: The row's server cursor, on replies; ignored on requests.
    knowledge: int | None = None


class SyncPageCursor(BaseModel):
    """Where a paged pull stopped, and the window it is walking (keksdose's LP-3 fix).

    ``knowledge`` alone is not an exact cursor: one flush stamps every row it touches with
    ONE knowledge (a bulk import gives 20,000 rows one value), so resuming "after the last
    knowledge" would step past the rest of that value. Hence ``id`` as the tiebreak within a
    knowledge value, and ``entity`` because entities are walked in the app's order, each
    finished before the next begins, which keeps a paged pull FK-ordered.

    ``window_end`` pins the walk to the tenant knowledge when the walk STARTED: a write
    landing mid-walk has ``knowledge > window_end`` and arrives on the next sync. The helpers
    are :mod:`~eifi1_server_kit.sync.paging`.
    """

    entity: str
    #: The last row emitted from ``entity``. Both ``None`` means "resume at the START of that
    #: entity" (a page that filled up exactly on an entity boundary).
    knowledge: int | None = Field(default=None, ge=0)
    id: str | None = None
    window_end: int = Field(ge=0)


class SyncRequest(BaseModel):
    """``POST /sync``: the device's cursor, its pushed changes and what it understands."""

    #: What the device syncs, where an app has more than one scope (kastlan:
    #: ``"handover"``); keksdose has one and sends none.
    scope: str | None = None
    #: The device's last-seen tenant knowledge; the pull returns rows newer than this.
    last_knowledge: int = Field(default=0, ge=0)
    changes: Sequence[SyncChange] = Field(default_factory=list)
    #: This client keeps REFUSED changes queued and reads :attr:`SyncResponse.refused` and
    #: :attr:`SyncResponse.rejections`. Without it the server refuses the way it always did,
    #: the whole request, for one release.
    refusals: bool = False
    #: This client understands a PAGED pull and follows ``page_cursor`` to the end. Opt-in:
    #: a client that ignored the cursor would record ``server_knowledge`` after page one.
    paged: bool = False
    #: Where the previous page stopped; ``None`` starts a fresh walk.
    page_cursor: SyncPageCursor | None = None
    #: The device's clock when it SENDS this push (0.16.0), spelt as ``updated_at``. The
    #: server corrects every change's stamp by its own now minus this
    #: (:func:`~eifi1_server_kit.sync.clock_skew`), so a slow or fast device clock neither
    #: loses nor wins conflicts it should not. Read it fresh for every request, retries
    #: included. ``None`` (an older client): no correction, the clamp alone.
    client_now: _EditTime | None = None


class SyncRefusal(BaseModel):
    """The changes a sync reply refused (§12.5): ``{code, detail, change_ids}``, on the
    app's sync answer beside the updates it still sends.

    **A 402 is not a refusal of one change.** Sync endpoints stay on the gate's allow-list;
    inside them, a lapsed payer's changes are refused as ``billing_read_only`` and not
    applied, and the reply still carries the updates — keksdose's E2EE gate is the
    precedent. The client keeps the refused changes queued, as "N changes waiting for a
    plan", and sends them after payment::

        class SyncResponse(BaseModel):
            server_knowledge: int
            changes: list[SyncChange]
            refused: SyncRefusal | None = None

    ``change_ids`` are the client-minted ids of the refused changes (keksdose's and
    kastlan's ``SyncChange.id``; a UUID is written as its string). ``code`` stays
    ``billing_read_only``: a create past the plan's limit is a per-change
    :class:`SyncRejection` with :data:`SYNC_PLAN_LIMIT`, never this refusal.

    Since 0.15 it lives in :mod:`eifi1_server_kit.sync` (local-first §1.6) and
    :mod:`eifi1_server_kit.billing` re-exports the same class. It is ONE refusal for the
    WHOLE push: a per-change refusal is a :class:`SyncRejection`, never a code here
    (keksdose's client blocks its queue for good on a code it does not know).
    """

    #: ``BillingErrorCode.BILLING_READ_ONLY`` (written out: billing imports this module).
    code: str = "billing_read_only"
    #: ``BILLING_ERROR_DETAIL[BILLING_READ_ONLY]``, written out for the same reason.
    detail: str = "Read-only for now: changes need an active plan"
    change_ids: list[_ChangeId] = Field(min_length=1)


class SyncRejection(BaseModel):
    """One pushed change the server did not apply, with its own reason (D1): the other
    changes of the push apply, and the device keeps ``detail`` on that outbox row.
    ``code`` is :data:`SYNC_REJECTED` or :data:`SYNC_PLAN_LIMIT`."""

    change_id: _ChangeId
    code: str = Field(min_length=1)
    detail: str


class SyncResponse(BaseModel):
    """The reply: the pull (``changes`` since ``last_knowledge``, losers echoed), and what
    of the push was not applied."""

    server_knowledge: int
    changes: Sequence[SyncChange]
    #: The WHOLE push refused (a lapsed plan, a read-only demo), the pull served all the
    #: same. Only for a request that sent ``refusals: true``.
    refused: SyncRefusal | None = None
    #: The pushed changes refused ONE BY ONE (D1); ``[]`` for a client that knows nothing
    #: of them. Only for a request that sent ``refusals: true``.
    rejections: list[SyncRejection] = Field(default_factory=list)
    #: Set when the delta did not fit in one page: hand it back to continue. ``None`` means
    #: this reply completed the walk, and ONLY then may the client record
    #: ``server_knowledge`` as its cursor.
    page_cursor: SyncPageCursor | None = None
