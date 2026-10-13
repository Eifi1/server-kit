"""The sign-in's timing: an address with no account costs what an address with one costs.

``docs/auth-harmonization.md`` §5.2 in ``Eifi1/ui-kit``: an unknown address, a wrong
password and a deactivated account are one refusal (``invalid_credentials``) "in the same
time (a dummy hash for unknown addresses)". Returning early when the address has no
account answers in microseconds instead of a quarter of a second, which tells the address
apart as surely as a different message would: Kurvenschmiede measured **262.2 ms** for an
address with an account and **3.7 ms** for one without, and with a dummy check in place
297.6 ms and 283.4 ms (``infrastructure/security.py:75-101``).

Each app wrote that dummy check itself — keksdose (``domain/services/auth_service.py:129-140``,
the canon), kastlan (``domain/services/auth_service.py:80-82``) and Kurvenschmiede
(``infrastructure/security.py:63-101``) — three spellings of a rule whose whole point is
that two paths cost the same (KS S3). :func:`absent_account_check` holds it once.

The kit hashes no password and depends on no hasher: each app keeps its own (bcrypt in
keksdose and Kurvenschmiede, passlib in kastlan) and hands its two functions in.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Hashable

__all__ = ["ABSENT_ACCOUNT_SECRET_BYTES", "absent_account_check"]

#: The random bytes behind the dummy hash's password (keksdose ``secrets.token_urlsafe(32)``).
ABSENT_ACCOUNT_SECRET_BYTES = 32


def absent_account_check[H](
    hash_password: Callable[[str], H],
    verify_password: Callable[[str, H], object],
    *,
    cost: Callable[[], Hashable] | None = None,
) -> Callable[[str], bool]:
    """A check for the address with no account: the same work a real check does, and
    ``False``, always. Built from the app's own two functions::

        check_absent = absent_account_check(hash_password, verify_password)
        ...
        user = await get_user_by_email(session, email)
        if user is None:
            check_absent(password)  # False, after one verify against the dummy hash
            return None
        password_ok = verify_password(password, user.password_hash)

    Four rules, which the three apps each kept in their own way (KS S3):

    * the dummy is the hash of a **random** secret (:data:`ABSENT_ACCOUNT_SECRET_BYTES`,
      keksdose's), never a string in a source file (kastlan's
      ``"kastlan-no-such-account"``), which a seed or a fixture could one day make
      somebody's password (Kurvenschmiede: "so it can never be mistaken for a
      credential");
    * it is built **lazily**, at the first unknown address, so it carries the cost
      ``hash_password`` hashes at then, not at import — keksdose's suite lowers bcrypt's
      rounds once it has started. ``cost`` is the app's cost, read at every call
      (``cost=lambda: get_settings().bcrypt_rounds``, Kurvenschmiede's): when it changes
      the dummy is built again, so a dummy at a cost the real rows aren't checked at can't
      bring the difference back. Without it, the dummy is built once;
    * it goes through the app's **own** ``verify_password``, so whatever that refuses
      without hashing — a password past bcrypt's 72 bytes — is refused here without
      hashing too: a short-circuit on one path only is the same oracle in a narrower
      doorway;
    * it answers **False**, whatever the password, so it can stand where the real check
      stands.

    Two threads at the first unknown address may each build a dummy; the last one stays,
    and both are random. The returned callable holds one dummy at a time.
    """
    dummy: tuple[Hashable, H] | None = None

    def check(password: str) -> bool:
        nonlocal dummy
        key = None if cost is None else cost()
        if dummy is None or dummy[0] != key:
            dummy = (key, hash_password(secrets.token_urlsafe(ABSENT_ACCOUNT_SECRET_BYTES)))
        verify_password(password, dummy[1])
        return False

    return check
