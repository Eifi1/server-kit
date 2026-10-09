"""Billing's settings: the switch, the provider, its secrets, the price ids, the launch, and
where the provider's checkout opens.

``docs/billing-harmonization.md`` §3.1, §4, §10 and §14.2–§14.4 in ``Eifi1/ui-kit``. A mixin like
:class:`~eifi1_server_kit.demo.DemoSettings`, for the app's ``Settings`` to inherit, so each
reads its environment variables there (``KEKSDOSE_BILLING_ENABLED``,
``KASTLAN_BILLING_WEBHOOK_SECRET``).

**Off by default** (§4). While off, ``GET /billing/status`` answers ``{billing_enabled:
false}`` and nothing else of billing exists: every other billing route and the webhook
answer 404 ``billing_disabled`` (:meth:`BillingSettings.require_billing_enabled`), and the
read-only gate is off — everyone is in good standing
(:meth:`BillingSettings.billing_standing`). **Switching on requires the secrets** (§10): a
deployment with the switch on and a secret missing fails at start, not at its first
webhook. With Paddle it also needs the app's tag (``billing_app``, set in code, §14.3) and
an API environment — sandbox or live — that resolves and agrees with the key's prefix
(§14.2), so a sandbox key against the live API fails at start, not at the first checkout.

**An empty ``<APP>_BILLING_*`` variable is an unset one**: an ``.env`` template lists the
variables with nothing after ``=`` until billing goes on, and the defaults must hold for
it (keksdose's 0.32 report). Read through pydantic-settings, an empty
``BILLING_LAUNCH_AT=`` was no datetime, and an empty ``BILLING_PRICE_IDS=`` failed before
any validator ran: pydantic-settings decodes a dict-typed variable as JSON first. The
price ids therefore opt out of that decoding (its ``NoDecode`` marker) and are decoded
here.
"""

from __future__ import annotations

import enum
import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Annotated, Any, NamedTuple, Self
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    model_validator,
)

from eifi1_server_kit.billing.errors import BillingError, BillingErrorCode
from eifi1_server_kit.billing.plans import (
    BillingCurrency,
    BillingInterval,
    Currency,
    Interval,
    PlanCode,
    normalize_plan,
)
from eifi1_server_kit.billing.standing import SubscriptionRow, in_good_standing

__all__ = ["BillingProvider", "BillingSettings", "PaddleEnvironment", "PriceRef"]

#: How far, in seconds, Paddle's signature timestamp ``ts`` may be from now: Paddle's own
#: default (its docs: "Our SDKs have a default tolerance of five seconds between the
#: timestamp and the current time"). Checked both ways, so a clock running ahead is no
#: loophole either. The default of
#: :attr:`BillingSettings.billing_signature_tolerance`; exported by
#: :mod:`~eifi1_server_kit.billing.signatures`.
PADDLE_SIGNATURE_TOLERANCE = 5.0


class BillingProvider(enum.StrEnum):
    """The Merchants of Record the kit speaks (§2.2, §9). Paddle is the one Marcel chose
    (decision 16, 2026-10-09); the kit's provider client speaks Paddle only (§14.2).

    .. deprecated:: 0.7.0
       ``LEMONSQUEEZY``, its event mapper and statuses, its signature check and the
       numeric variant ids in ``billing_price_ids`` are deprecated and go in 0.8 (decision
       25, §14.13). Nothing sells through it.
    """

    PADDLE = "paddle"
    #: Deprecated since 0.7.0, removed in 0.8 (§14.13).
    LEMONSQUEEZY = "lemonsqueezy"


class PaddleEnvironment(enum.StrEnum):
    """Paddle's two APIs (§14.2, decision 24): ``sandbox`` (``sandbox-api.paddle.com``) and
    ``live`` (``api.paddle.com``). A sandbox key works only against the sandbox and a live
    key only against live, so the environment is read from the key's prefix
    (:func:`~eifi1_server_kit.billing.paddle_environment_of`) unless the settings name it."""

    SANDBOX = "sandbox"
    LIVE = "live"


class PriceRef(NamedTuple):
    """What a provider's price id stands for: one plan, in one currency, per one interval."""

    plan: str
    currency: BillingCurrency
    interval: BillingInterval


def _price_ids(value: object) -> object:
    """One id or a list of them; Lemon Squeezy's variant ids arrive as numbers in JSON (the
    number coercion is deprecated with Lemon Squeezy and goes in 0.8, §14.13)."""
    if isinstance(value, str | int) and not isinstance(value, bool):
        value = [value]
    if isinstance(value, list):
        return [str(item) if isinstance(item, int) and not isinstance(item, bool) else item for item in value]
    return value


_PriceId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
#: The current price id first, then any retired ones still running subscriptions.
_PriceIds = Annotated[list[_PriceId], BeforeValidator(_price_ids), Field(min_length=1)]


def _no_json_decoding() -> object:
    """pydantic-settings' ``NoDecode`` marker, so its sources hand the price ids over as
    the variable's text, an empty one included; ``None`` — no marker — without
    pydantic-settings, where no source decodes anything. The kit doesn't depend on
    pydantic-settings; every app reads its settings through it."""
    try:
        from pydantic_settings import NoDecode
    except ImportError:  # pragma: no cover - the kit's tests run with pydantic-settings
        return None
    return NoDecode


def _json_text(value: object) -> object:
    """The price ids as the environment holds them, JSON text, decoded; a mapping as it is."""
    return json.loads(value) if isinstance(value, str | bytes) else value


#: The hosts a page URL may name with plain ``http://``: this machine only.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _page_url(value: str) -> str:
    """A page the buyer is sent to: ``https://``, or ``http://`` for localhost only (§14.2)."""
    parts = urlsplit(value)
    host = parts.hostname
    if not host or parts.scheme not in ("https", "http") or (parts.scheme == "http" and host not in _LOCAL_HOSTS):
        raise ValueError("a checkout page is an https:// address (http:// for localhost only)")
    return value


_PageUrl = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500), AfterValidator(_page_url)]
#: The app's tag: lowercase, a letter first, as a plan code (``keksdose``).
_AppTag = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[a-z][a-z0-9_-]{0,63}$")]


class BillingSettings(BaseModel):
    """Billing's settings (§4, §10), named for ``<APP>_BILLING_*``.

    Meant to be INHERITED by the app's settings::

        class Settings(BaseSettings, DemoSettings, BillingSettings):
            model_config = SettingsConfigDict(env_prefix="KEKSDOSE_")

    An app that keeps its own fields builds one from them instead:
    ``BillingSettings.model_validate(settings, from_attributes=True)``.

    ``billing_price_ids`` is plan → currency → interval → the provider's price id (§3.1;
    Paddle's ``pri_…``, Lemon Squeezy's variant id), JSON in the environment::

        KEKSDOSE_BILLING_PRICE_IDS='{"pro": {"CHF": {"year": "pri_01h…", "month": "pri_01j…"}}}'

    A combination may hold a LIST instead: the current id first — what a new checkout uses
    — then retired ids whose subscriptions still run, so their webhooks still find the plan
    (``["pri_new", "pri_old"]``). An id may stand for one combination only.

    The one id becoming a list, the keys' normalisation and that check are the field's
    VALIDATION: they run when the settings are built or validated, not on assignment (no
    ``validate_assignment``). A test that monkeypatches ``billing_price_ids`` passes the
    parsed shape — ``{"pro": {"CHF": {"year": ["pri_test"]}}}`` — or a bare string is read
    character by character (:meth:`billing_price_id` answers ``"p"``).
    """

    model_config = ConfigDict(from_attributes=True)

    #: Off: billing doesn't exist for the app (§4). On: ``billing_provider``,
    #: ``billing_api_key`` and ``billing_webhook_secret`` must be set.
    billing_enabled: bool = False
    #: The Merchant of Record this deployment sells through (§9).
    billing_provider: BillingProvider | None = None
    #: The provider API key the kit's client creates checkouts, portal sessions and
    #: cancellations with (:func:`~eifi1_server_kit.billing.billing_provider_client`,
    #: §14.2). Paddle's 2025 keys (``pdl_sdbx_apikey_…``, ``pdl_live_apikey_…``) carry their
    #: environment and their permissions: transaction write, customer-portal-session write
    #: and subscription write.
    billing_api_key: SecretStr | None = None
    #: The webhook's signing secret (§5): Paddle's endpoint secret key (``pdl_ntfset_…``),
    #: Lemon Squeezy's signing secret.
    billing_webhook_secret: SecretStr | None = None
    #: How far, in seconds, Paddle's signature timestamp may be from now (§5): the
    #: ``tolerance`` the app passes to
    #: :func:`~eifi1_server_kit.billing.verify_webhook_signature`. Paddle's default, five
    #: seconds (:data:`PADDLE_SIGNATURE_TOLERANCE`), unless the deployment widens it: on a
    #: scale-to-zero host (Cloud Run at min-instances 0) a cold start can eat the five
    #: seconds, and every first delivery after a quiet spell answers 400 until Paddle's
    #: retry lands warm. keksdose runs 60 (``KEKSDOSE_BILLING_SIGNATURE_TOLERANCE=60``). A
    #: replay inside the window is still a duplicate to the event store (§5). Lemon
    #: Squeezy signs no timestamp, so it ignores this.
    billing_signature_tolerance: float = Field(default=PADDLE_SIGNATURE_TOLERANCE, gt=0)
    #: Plan → currency → interval → price id(s); see the class docstring.
    billing_price_ids: Annotated[
        dict[PlanCode, dict[Currency, dict[Interval, _PriceIds]]],
        _no_json_decoding(),
        BeforeValidator(_json_text),
    ] = Field(default_factory=dict)
    #: When billing went on for this app (§2.4): the beta's 12 months run from it
    #: (:func:`~eifi1_server_kit.billing.beta_comped_until`), and an invitation created
    #: before it makes a beta payer (:func:`~eifi1_server_kit.billing.is_beta`). Unset until
    #: Marcel names the date, and NOT required to switch on: a beta row stored without an
    #: end reads as no end until it is set, then as this date plus 12 months
    #: (:func:`~eifi1_server_kit.billing.effective_comped_until`), so moving the date moves
    #: every such row's end with it.
    billing_launch_at: datetime | None = None
    #: The app's name in every checkout's custom data (``{"app": …, "payer_ref": …}``,
    #: §14.3): one Paddle account sells for all three apps, and every app's webhook receives
    #: every app's events, so the webhook drops the others' and the untagged
    #: (:func:`~eifi1_server_kit.billing.parse_webhook_event`'s ``app``). Set as the DEFAULT
    #: in the app's ``Settings`` subclass (``billing_app: str | None = "keksdose"``), not per
    #: deployment; ``<APP>_BILLING_APP`` exists but is listed as "set in code". Lowercase, a
    #: letter first. Required to switch on with Paddle.
    billing_app: _AppTag | None = None
    #: Paddle's API, ``sandbox`` or ``live`` (decision 24). Unset: read from the API key's
    #: prefix (``pdl_sdbx_apikey_`` / ``pdl_live_apikey_``); a legacy key, from before
    #: 2025-05-06, needs it set. Set and the key's prefix says the other: refused at start.
    #: The API's address is the kit's constant
    #: (:data:`~eifi1_server_kit.billing.PADDLE_API_BASES`): no host can be configured.
    billing_environment: PaddleEnvironment | None = None
    #: The kit's pay page on the app's pay host, ``https://pay.<app domain>/`` (§14.4), sent
    #: as the transaction's ``checkout.url``; Paddle answers it with ``?_ptxn=<txn>``. Unset:
    #: the account's default payment link — one page for the whole account, so each app
    #: sets its own. ``https://``, or ``http://`` for localhost only.
    billing_checkout_page_url: _PageUrl | None = None
    #: A Paddle hosted checkout's launch URL (``https://pay.paddle.io/checkout/hsc_…``),
    #: which wins over ``billing_checkout_page_url``: the checkout answers
    #: ``<this>?transaction_id=<txn>&locale=<locale>``, and the dashboard sets its redirect.
    #: Every sandbox has one; live only with Paddle's approval (§14.4).
    #: ``KASTLAN_BILLING_HOSTED_CHECKOUT_URL`` keeps its name.
    billing_hosted_checkout_url: _PageUrl | None = None

    @model_validator(mode="before")
    @classmethod
    def _an_empty_variable_is_unset(cls, data: Any) -> Any:
        """Drop a billing field given as blank text, so its default applies. Only the
        mixin's own fields: what an app's own empty variable means is the app's call."""
        if not isinstance(data, dict):
            return data  # model_validate(settings, from_attributes=True): typed already
        return {
            name: value
            for name, value in data.items()
            if not (name in _OWN_FIELDS and isinstance(value, str) and not value.strip())
        }

    @model_validator(mode="after")
    def _switching_on_needs_the_secrets(self) -> Self:
        if self.billing_enabled:
            missing = [
                name
                for name, value in (
                    ("billing_provider", self.billing_provider),
                    ("billing_api_key", self.billing_api_key),
                    ("billing_webhook_secret", self.billing_webhook_secret),
                )
                if value is None or (isinstance(value, SecretStr) and not value.get_secret_value().strip())
            ]
            if missing:
                raise ValueError(f"billing_enabled needs {', '.join(missing)}")
            if self.billing_provider is BillingProvider.PADDLE:
                self._paddle_can_switch_on()
        self._price_index()  # an id standing for two combinations is refused here, at start
        return self

    def _paddle_can_switch_on(self) -> None:
        """Paddle needs the app's tag (§14.3) and an environment that resolves and agrees
        with the key (§14.2): a sandbox key on live fails here, at start. The environment's
        refusal is a :class:`BillingError`, a :class:`ValueError`, so the settings refuse
        with its words."""
        if self.billing_app is None:
            raise ValueError("billing_enabled with Paddle needs billing_app, the app's tag (set it in code)")
        self.billing_api_environment()

    def _price_index(self) -> dict[str, PriceRef]:
        index: dict[str, PriceRef] = {}
        for plan, currencies in self.billing_price_ids.items():
            for currency, intervals in currencies.items():
                for interval, ids in intervals.items():
                    for price_id in ids:
                        ref = PriceRef(plan, currency, interval)
                        if index.setdefault(price_id, ref) != ref:
                            raise ValueError(f"the price id {price_id!r} stands for two plans or periods")
        return index

    def require_billing_enabled(self) -> None:
        """Refuse when the switch is off: 404 ``billing_disabled`` (§2.9). First thing in
        every billing route but ``GET /billing/status`` (which answers a demo user too,
        §12.19), and in the webhook."""
        if not self.billing_enabled:
            raise BillingError(BillingErrorCode.BILLING_DISABLED)

    def billing_price_id(self, plan: str, currency: BillingCurrency | str, interval: BillingInterval | str) -> str:
        """The provider's CURRENT price id for a checkout (§4 ``POST /billing/checkout``).

        Missing — the plan isn't sold that way, or the setting lacks it — is 503
        ``billing_not_configured``: check the plan's own ``prices`` first for a 422 on a
        combination the catalogue doesn't sell.
        """
        key = normalize_plan(plan)
        try:
            return self.billing_price_ids[key][BillingCurrency(currency.strip().upper())][
                BillingInterval(interval.strip().lower())
            ][0]
        except KeyError, ValueError:
            raise BillingError(
                BillingErrorCode.BILLING_NOT_CONFIGURED, f"No price id for {key}, {currency}, {interval}"
            ) from None

    def billing_price_ref(self, price_id: str) -> PriceRef | None:
        """What a provider's price id stands for — a webhook's plan — or ``None`` for an id
        the settings don't name, current or retired."""
        return self._price_index().get(price_id)

    def billing_plan_for_price(self, price_id: str) -> str | None:
        """The plan code a provider's price id stands for, or ``None``: the ``plan_for_price``
        :func:`~eifi1_server_kit.billing.dispatch` takes."""
        ref = self.billing_price_ref(price_id)
        return None if ref is None else ref.plan

    def billing_webhook_key(self) -> str:
        """The webhook secret's value, for the signature check (§5); 503
        ``billing_not_configured`` without one (only reachable with the switch off)."""
        if self.billing_webhook_secret is None:
            raise BillingError(BillingErrorCode.BILLING_NOT_CONFIGURED, "No webhook secret")
        return self.billing_webhook_secret.get_secret_value()

    def billing_api_environment(self) -> PaddleEnvironment:
        """Paddle's API for this deployment (§14.2): ``billing_environment`` where set, else
        the API key's prefix. Neither, or the two disagreeing: 503
        ``billing_not_configured`` — reachable only with the switch off, since switching on
        with Paddle checks both."""
        from eifi1_server_kit.billing.paddle import paddle_environment_of

        of_key = (
            None if self.billing_api_key is None else paddle_environment_of(self.billing_api_key.get_secret_value())
        )
        environment = self.billing_environment or of_key
        if environment is None:
            raise BillingError(
                BillingErrorCode.BILLING_NOT_CONFIGURED,
                "Set billing_environment: the API key's prefix says neither sandbox nor live (a key from before 2025-05-06)",
            )
        if of_key is not None and of_key is not environment:
            raise BillingError(
                BillingErrorCode.BILLING_NOT_CONFIGURED,
                f"The API key is a {of_key} key, but billing_environment is {environment}",
            )
        return environment

    def verify_billing_webhook(
        self,
        provider: BillingProvider | str,
        raw_body: bytes,
        headers: Mapping[str, str],
        *,
        now: float | None = None,
    ) -> None:
        """:func:`~eifi1_server_kit.billing.verify_webhook_signature` with this deployment's
        secret AND its tolerance (§14.2), or 400 ``invalid_signature``::

            settings.require_billing_enabled()                       # 404 billing_disabled
            raw = await request.body()
            settings.verify_billing_webhook("paddle", raw, request.headers)

        The right call made the easy one: kastlan and Kurvenschmiede called the signature
        check without ``tolerance=``, so Paddle's five seconds applied whatever
        ``<APP>_BILLING_SIGNATURE_TOLERANCE`` said, and a cold start answered 400 until
        Paddle's retry landed warm. No secret: 503 ``billing_not_configured``.
        """
        from eifi1_server_kit.billing.signatures import verify_webhook_signature

        verify_webhook_signature(
            provider, raw_body, headers, self.billing_webhook_key(), now=now, tolerance=self.billing_signature_tolerance
        )

    def billing_standing(
        self, row: SubscriptionRow | None, now: datetime, *, retry_grace: timedelta | None = None
    ) -> bool:
        """:func:`~eifi1_server_kit.billing.in_good_standing`, behind the switch: with
        billing off everyone is in good standing (§4). The standing the read-only gate
        takes (:func:`~eifi1_server_kit.billing.billing_write_allowed`). A beta row stored
        without an end ends at :attr:`billing_launch_at` plus 12 months, once that is set
        (§3.2)."""
        return not self.billing_enabled or in_good_standing(
            row, now, retry_grace=retry_grace, launch=self.billing_launch_at
        )


#: The mixin's fields, the ones an empty variable leaves at their default.
_OWN_FIELDS = frozenset(BillingSettings.model_fields)
