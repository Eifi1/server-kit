"""The account: its address, the address tag, who may register, and how a name is written.

``docs/auth-harmonization.md`` §3–§4 and §8 in ``Eifi1/ui-kit``. Pure functions over
plain values: the lookups, the invitation rows and the role an invitation names stay in
the app.

**The address** (§3.1, §4.5). Trimmed and lower-cased at every entry — sign-up, sign-in,
reset, invite, allow-list, admin — and nothing else: a ``+tag`` is part of the identity,
so ``you@example.com`` and ``you+kastlan@example.com`` are two accounts. keksdose's rule
(``auth_service.get_beta_allowlist_entry``: ``email.strip().lower()``).

**The address tag** (§4.5, keksdose ``frontend/src/features/auth/email-tag.ts``). The
sign-up form OFFERS ``you+<app>@example.com``; the server never applies it. What the
server does with it: an invitation for ``you@x`` also accepts ``you+<app>@x``
(:func:`invitation_accepts`), but only the exact address counts as proven by the
invitation (:func:`verified_by_invitation`); a reset that finds no account for ``you@x``
also tries ``you+<app>@x`` (:func:`addresses_for_reset`). Sign-in stays exact.

**The gate** (§4.3, §2.12). The first user ever becomes the admin; after that a
registration needs an invitation, and an allow-list entry IS one. The environment list
only bootstraps the first admin (:func:`registration_decision`).

**Names** (§3.2). First and last name, both required at sign-up, written in the order of
the reader's language: :func:`full_name`.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable

#: What stands between a local part and its tag (RFC 5233 sub-addressing).
TAG_SEPARATOR = "+"

_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def normalise_email(raw: str) -> str:
    """``raw`` trimmed and lower-cased — and nothing else (§3.1, §4.5).

    Never strips a ``+tag`` and never folds dots: the tagged address is the identity the
    user signs in with, and a provider's own aliasing rules are not ours to guess.
    """
    return raw.strip().lower()


def _split(email: str) -> tuple[str, str] | None:
    """``(local, domain)`` of an address complete enough to split, else ``None``.

    The same shape as keksdose's ``taggedEmail``: the LAST ``@``, a non-empty local part,
    and a domain with a dot that neither starts nor ends it.
    """
    local, at, domain = email.rpartition("@")
    if not at or not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        return None
    return local, domain


def _check_tag(tag: str) -> str:
    """The app's tag, lower-cased: its name (``keksdose``, ``kastlan``, ``kurvenschmiede``)."""
    normalised = tag.strip().lower()
    if not _TAG_RE.match(normalised):
        raise ValueError(f"an address tag is the app's name — letters, digits, '.', '_' or '-': {tag!r}")
    return normalised


def tagged_variant(email: str, tag: str) -> str | None:
    """``you@example.com`` → ``you+<tag>@example.com``, normalised; ``None`` when there is
    nothing to offer (keksdose ``email-tag.ts`` ``taggedEmail``, §4.5).

    ``None`` when the address already carries a ``+`` — their own scheme wins, and
    stacking two would be wrong — or is not complete enough to split. ``tag`` is the
    app's name; anything but letters, digits, ``.``, ``_`` and ``-`` is a
    :class:`ValueError` (a programming error, not a user's).
    """
    checked = _check_tag(tag)
    parts = _split(normalise_email(email))
    if parts is None or TAG_SEPARATOR in parts[0]:
        return None
    local, domain = parts
    return f"{local}{TAG_SEPARATOR}{checked}@{domain}"


def invitation_accepts(invited: str, registered: str, tag: str) -> bool:
    """May ``registered`` redeem an invitation for ``invited``? (§4.5)

    The exact address, or the same local part with the app's own tag on the same domain
    — ``you@example.com`` accepts ``you+kastlan@example.com`` for kastlan — and nothing
    else: not another tag, not another domain, not the untagged address of a tagged
    invitation. Both sides are normalised first.
    """
    invited_n, registered_n = normalise_email(invited), normalise_email(registered)
    return registered_n == invited_n or registered_n == tagged_variant(invited_n, tag)


def verified_by_invitation(invited: str, registered: str) -> bool:
    """Does registering through this invitation prove the mailbox? Only for the exact
    invited address (§4.3, §10.7).

    The token reached ``you@x``, which says nothing about whether ``you+app@x`` is
    delivered — not every server implements sub-addressing — so a tagged registration
    starts unverified and is sent its own verification mail (Kurvenschmiede's point).
    """
    return normalise_email(invited) == normalise_email(registered)


def addresses_for_reset(email: str, tag: str) -> tuple[str, ...]:
    """The addresses a password-reset request looks up, in order (§4.5).

    The submitted address first; if it finds no account, its tagged variant — a user who
    took the tag at sign-up and forgot it still gets the link. The mail goes to the
    account's OWN address (the tagged one), and the answer is ``204`` either way.
    """
    exact = normalise_email(email)
    tagged = tagged_variant(exact, tag)
    return (exact,) if tagged is None else (exact, tagged)


def parse_env_list(raw: str | None) -> frozenset[str]:
    """The bootstrap list from an environment variable: addresses separated by commas
    and/or whitespace, normalised (keksdose ``auth_service._registration_allowlist``)."""
    return frozenset(normalise_email(part) for part in re.split(r"[,\s]+", (raw or "").strip()) if part)


def env_list_match(email: str, env_list: Iterable[str]) -> bool | None:
    """``on_env_list`` for :func:`registration_decision`: ``None`` when no list is set,
    else whether the normalised address is on it."""
    listed = {normalise_email(entry) for entry in env_list}
    if not listed:
        return None
    return normalise_email(email) in listed


class RegistrationDecision(enum.Enum):
    """What :func:`registration_decision` answers."""

    #: The first account ever: it becomes the admin.
    FIRST_ADMIN = "first_admin"
    #: Let in by a valid invitation (an allow-list entry is one); role and associations
    #: come from it.
    INVITED = "invited"
    #: Refused: ``403 {code: "registration_closed"}``.
    CLOSED = "closed"

    @property
    def allowed(self) -> bool:
        return self is not RegistrationDecision.CLOSED

    @property
    def first_admin(self) -> bool:
        return self is RegistrationDecision.FIRST_ADMIN


def registration_decision(
    *,
    is_first_user: bool,
    on_env_list: bool | None,
    has_valid_invitation: bool,
) -> RegistrationDecision:
    """May this address register, and as what? (§4.3, §2.12)

    * **The first account ever** becomes the admin — unless the app has an environment
      list (``on_env_list`` is not ``None``) that does not name it. That is the list's one
      job now: who may claim a fresh deployment, so a stranger who finds it first
      cannot. ``None`` means the app has no list, and the first registration is open, as
      in keksdose today. Build the argument with :func:`env_list_match`.
    * **After that**, only a valid invitation lets anyone in — an allow-list entry is an
      invitation, so the app passes ``True`` for one. The environment list grants
      nothing any more.
    * Everyone else: :attr:`RegistrationDecision.CLOSED`, answered as
      ``AuthError(AuthErrorCode.REGISTRATION_CLOSED)``.

    The role and the associations never come from the form: from the invitation, the
    allow-list row, or the app's default (§4.3).
    """
    if is_first_user:
        return RegistrationDecision.CLOSED if on_env_list is False else RegistrationDecision.FIRST_ADMIN
    return RegistrationDecision.INVITED if has_valid_invitation else RegistrationDecision.CLOSED


#: The languages that write the family name first (§3.2): Hungarian with a space…
FAMILY_NAME_FIRST: frozenset[str] = frozenset({"hu"})
#: …and Chinese without one.
FAMILY_NAME_FIRST_NO_SPACE: frozenset[str] = frozenset({"zh"})


def _language(locale: str | None) -> str:
    """``"de-CH"`` → ``"de"``, ``"zh_Hans"`` → ``"zh"``; ``""`` for none."""
    return (locale or "").strip().lower().replace("_", "-").split("-", 1)[0]


def full_name(first: str | None, last: str | None, locale: str | None) -> str:
    """A person's name in the order of ``locale``'s language (§3.2).

    "First Last" in de-CH, en, fr, it and es (and any language not listed); "Last First"
    in hu; "LastFirst" with no space in zh. Both parts are trimmed; an empty part leaves
    the other alone, so a migrated account whose ``last_name`` is still empty shows its
    old display name as it was.

    Whose language: the READER's on screen (the kit formats there; the API's
    ``display_name`` is a fallback), the RECIPIENT's in a mail or a push, the AUTHOR's in
    a copy frozen when it was written — the feedback stamp (§3.2, §10.6).
    """
    given, family = (first or "").strip(), (last or "").strip()
    if not given or not family:
        return given or family
    language = _language(locale)
    if language in FAMILY_NAME_FIRST_NO_SPACE:
        return f"{family}{given}"
    if language in FAMILY_NAME_FIRST:
        return f"{family} {given}"
    return f"{given} {family}"


def name_incomplete(first: str | None, last: str | None, *, is_demo: bool = False) -> bool:
    """``/auth/me``'s ``name_incomplete`` (§3.3): either part empty after trimming.

    A demo session never counts as incomplete (keksdose's anonymous demo has nobody to
    ask), so ``is_demo=True`` is always ``False``.
    """
    if is_demo:
        return False
    return not (first or "").strip() or not (last or "").strip()


#: The orders :func:`full_name` writes a name in — its every form, for erasure.
_NAME_ORDER_LOCALES: tuple[str, ...] = ("en", "hu", "zh")


def erasure_identifiers(
    email: str | None,
    first: str | None,
    last: str | None,
    old_display_name: str | None = None,
) -> list[str]:
    """A person's own strings, for :func:`eifi1_server_kit.feedback.anonymise_feedback`'s
    ``identifiers`` (§8, §10.6).

    The email, the old ``display_name`` (what a stamp written before the switch holds),
    and the full name in every order :func:`full_name` writes it — "First Last",
    "Last First", and zh's "LastFirst" — because the feedback stamp is frozen in its
    AUTHOR's language. **Never a bare first or last name**: redacting "Mai" or "Bank"
    on its own would shred a report that mentions the month or the bank. With a part
    missing, the full-name forms are left out; the old display name covers a migrated
    account.

    De-duplicated case-insensitively and ordered LONGEST FIRST, because
    :func:`~eifi1_server_kit.feedback.scrub_text` replaces needles one after another: an
    old display name "Ada Lovelace" replaced before "Ada Lovelace-Byron" would leave
    "[erased]-Byron" behind.
    """
    candidates: list[str | None] = [email, old_display_name]
    if (first or "").strip() and (last or "").strip():
        candidates += [full_name(first, last, locale) for locale in _NAME_ORDER_LOCALES]
    seen: set[str] = set()
    out: list[str] = []
    for candidate in candidates:
        text = (candidate or "").strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return sorted(out, key=len, reverse=True)
