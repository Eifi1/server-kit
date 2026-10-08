"""The read-only gate: a lapsed payer's data can be read, never changed.

``docs/billing-harmonization.md`` §3.3 and §12.1–§12.5, §12.13–§12.14 in ``Eifi1/ui-kit``.
**The gate is the app's choice of two equal shapes** (§12.2):

* :func:`billing_write_allowed` at the auth dependency — the demo's shape
  (:func:`~eifi1_server_kit.demo.demo_write_allowed`), with the payer's standing as an
  input, because the payer is not always the caller (§12.1): keksdose's budget owner,
  Kurvenschmiede's row owner, kastlan's acting company;
* :func:`refuse_billing_read_only` at the app's own write choke points, which already
  resolve the owner (keksdose ``require_write_access``, Kurvenschmiede ``get_owner`` and
  ``access.claim``).

Either way a compute-only POST (Kurvenschmiede's five previews) is not a write, the demo's
403 comes first and billing's 402 second (§12.3), and scheduled jobs are outside the HTTP
gate — they skip a lapsed payer's data themselves (§12.4).
"""

from __future__ import annotations

from collections.abc import Collection

from eifi1_server_kit.billing.errors import BillingError, BillingErrorCode
from eifi1_server_kit.demo import demo_write_allowed

__all__ = ["billing_write_allowed", "refuse_billing_read_only"]


def billing_write_allowed(method: str, path: str, *, standing: bool, allow: Collection[str] = frozenset()) -> bool:
    """May this request go on, given the payer's ``standing``? §3.3's gate, at the auth
    dependency::

        standing = settings.billing_standing(payer_row, now)
        if not billing_write_allowed(request.method, request.url.path, standing=standing, allow=BILLING_WRITES):
            raise BillingError(BillingErrorCode.BILLING_READ_ONLY)

    * A payer in good standing — or billing switched off, which
      :meth:`~eifi1_server_kit.billing.BillingSettings.billing_standing` answers as good
      standing — always may.
    * GET, HEAD and OPTIONS always may: reading, exporting and signing in always work.
    * Anything else only when ``allow`` names it, in the demo's syntax: ``"METHOD /path"``
      with the app's prefix, ``{name}`` for one path segment
      (:func:`~eifi1_server_kit.demo.demo_write_allowed`). The list is the app's (§12.13,
      §12.14): billing itself, the account's own settings, signing out; feedback with
      attachments and crash reports; removing access (revoking a share, leaving a shared
      item, offboarding, sessions, API tokens, passkeys, 2FA); taking the data and
      leaving; admin routes; receiving sync. **Adding seats or guests stays blocked.**

    **Sync endpoints go on the allow-list** (§12.5): a 402 would stop the reply that
    carries the updates. Inside them, refuse a lapsed payer's changes one by one and say
    so in the reply (:class:`~eifi1_server_kit.billing.SyncRefusal`).
    """
    return standing or demo_write_allowed(method, path, allow)


def refuse_billing_read_only(in_good_standing: bool, what: str) -> None:
    """Refuse a write at the app's own choke point while the payer is not in good standing:
    402 ``billing_read_only``, naming ``what`` for the log (§3.3, §12.2)::

        refuse_billing_read_only(settings.billing_standing(owner_row, now), "a new budget")

    The detail never names the payer's status or the payer (§12.6): a guest in a lapsed
    owner's budget reads it too, and learns only that the budget is read-only for now.
    """
    if not in_good_standing:
        raise BillingError(
            BillingErrorCode.BILLING_READ_ONLY, f"Read-only for now: changes need an active plan ({what})"
        )
