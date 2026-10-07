"""The sign-in, sign-up and reset budgets: :class:`AuthLimiters`, one instance per app.

``docs/auth-harmonization.md`` §5.2 in ``Eifi1/ui-kit``. Built on
:class:`~eifi1_server_kit.limiter.SlidingWindowRateLimiter`, so everything its docstring
says holds here too: **process-local** (each worker and each instance counts on its own),
and built per app — in ``create_app``, on ``app.state`` — never as a module global.

**Per IP, a hard limit. Per address, only a delay.** The IP windows answer ``429`` with a
``Retry-After``. An address is different: anybody can type anybody's address, so a lock
per address would let a stranger lock a known user out of their own account by failing
on purpose — a denial-of-service lever handed to whoever knows the address (§5.2, §10.10).
So per address only FAILURES are counted, and the answer slows down instead:
:class:`LoginFailureThrottle`. The owner always gets in, at worst after
:data:`LOGIN_FAILURE_MAX_DELAY` seconds.

**The IP is a coarse signal.** ``X-Forwarded-For`` is client-supplied and Cloud Run
appends to it rather than replacing it, so a client rotating that header gets a fresh IP
window per request wherever the LEFT-most entry is read (keksdose ``auth_router.py:150``).
Key every IP window by :func:`~eifi1_server_kit.limiter.client_ip`, which counts from the
right. Even then many people share an address and one person may hold many, which is why
the 2FA and set-password steps also count per challenge SUBJECT — a key no header changes,
and safe to count, because only someone who passed the password step can drive it.

The numbers are keksdose's, except registration (Kurvenschmiede's; keksdose has none) and
the reset windows, which kastlan runs too. Each is a :class:`Budget` an app may replace.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from eifi1_server_kit.auth.accounts import normalise_email
from eifi1_server_kit.limiter import Clock, SlidingWindowRateLimiter


@dataclass(frozen=True, slots=True)
class Budget:
    """At most ``max_hits`` per key in any ``window_seconds``."""

    max_hits: int
    window_seconds: float

    def limiter(self, clock: Clock) -> SlidingWindowRateLimiter:
        return SlidingWindowRateLimiter(max_hits=self.max_hits, window_seconds=self.window_seconds, clock=clock)


#: ``/auth/login`` and the passkey finish, per IP — the hard limit, charged BEFORE the
#: password check so a shed request costs no bcrypt (keksdose ``_login_ip_limiter``).
LOGIN_PER_IP = Budget(30, 300)
#: ``/auth/login/2fa`` and ``/auth/login/set-password``, per IP (keksdose
#: ``_challenge_ip_limiter``): a six-digit code behind an open endpoint is 10^6 guesses.
CHALLENGE_PER_IP = Budget(20, 300)
#: The same two, per challenge subject — the window a header cannot re-key (keksdose
#: ``_challenge_subject_limiter``).
CHALLENGE_PER_SUBJECT = Budget(10, 900)
#: ``/auth/register``, per IP: the other endpoint that hashes (Kurvenschmiede
#: ``REGISTRATION_ATTEMPTS``).
REGISTER_PER_IP = Budget(5, 300)
#: ``/auth/password-reset/request``: 10 an hour per IP asking, 3 an hour per address
#: asked about (keksdose, kastlan). Two windows, because neither covers the other: the
#: first bounds one client trying address after address, the second keeps a victim's
#: inbox quiet when the requests come from everywhere. The second is keyed by the
#: SUBMITTED address, whether or not it has an account, so it is no existence oracle.
RESET_PER_IP = Budget(10, 3600)
RESET_PER_ADDRESS = Budget(3, 3600)
#: ``/auth/resend-verification``, per RECIPIENT address, not per IP (keksdose
#: ``_resend_email_limiter``): what it protects is that inbox and the sender's reputation.
VERIFICATION_RESEND_PER_RECIPIENT = Budget(10, 3600)

#: Failures per address that cost nothing: a person mistyping a password a few times.
LOGIN_FAILURES_FREE = 5
#: The delay after the first failure past the free ones, doubling with each further one…
LOGIN_FAILURE_BASE_DELAY = 1.0
#: …up to this, in seconds. The owner under attack waits at most this long, never more.
LOGIN_FAILURE_MAX_DELAY = 30.0
#: How long a failure is remembered.
LOGIN_FAILURE_WINDOW_SECONDS = 900.0


def login_failure_delay(
    failures: int,
    *,
    free: int = LOGIN_FAILURES_FREE,
    base: float = LOGIN_FAILURE_BASE_DELAY,
    cap: float = LOGIN_FAILURE_MAX_DELAY,
) -> float:
    """Seconds to hold a sign-in for an address with ``failures`` failures in the window.

    ``0`` for the first ``free``; then ``base``, doubling per failure, never above
    ``cap``. With the defaults: five free, then 1, 2, 4, 8, 16 and 30 seconds.
    """
    if failures < free:
        return 0.0
    # Past the cap the exponent stops mattering; bounding it keeps 2.0 ** n finite.
    exponent = min(failures - free, 64)
    return float(min(cap, base * 2.0**exponent))


def _failures_to_cap(*, free: int, base: float, cap: float) -> int:
    """The failure count at which :func:`login_failure_delay` reaches ``cap`` — the most a
    window needs to remember."""
    failures = free
    while login_failure_delay(failures, free=free, base=base, cap=cap) < cap:
        failures += 1
    return max(failures, 1)


class LoginFailureThrottle:
    """Failed sign-ins per address: counted, and answered with a delay, never a lockout.

    Per request to ``/auth/login``, with the SUBMITTED address — whether or not it has an
    account, so an unknown address and a known one cost the same (§5.2's "one error, in
    the same time")::

        await asyncio.sleep(limiters.login_failures.delay(payload.email))
        user = await authenticate(...)
        if user is None:
            limiters.login_failures.record_failure(payload.email)
            raise AuthError(AuthErrorCode.INVALID_CREDENTIALS)
        limiters.login_failures.clear(payload.email)

    **Wait BEFORE checking the password**, for every attempt on that address — not only
    after a failure. A delay on failures alone is visible as one: an attacker gives up
    on each answer after a few milliseconds and reads silence as "wrong". Waiting first
    holds the right answer back exactly as long as the wrong one.

    What it does not do: bound a botnet. Parallel requests each wait, and no
    per-address rule can stop them without becoming the lockout this avoids — the IP
    window is the hard limit. Addresses are normalised here, so ``Ada@Example.com`` and
    ``ada@example.com`` count together.
    """

    def __init__(
        self,
        *,
        free: int = LOGIN_FAILURES_FREE,
        base_delay: float = LOGIN_FAILURE_BASE_DELAY,
        max_delay: float = LOGIN_FAILURE_MAX_DELAY,
        window_seconds: float = LOGIN_FAILURE_WINDOW_SECONDS,
        clock: Clock = time.monotonic,
    ) -> None:
        if free < 0 or base_delay <= 0 or max_delay < base_delay:
            raise ValueError("free >= 0 and 0 < base_delay <= max_delay")
        self._free, self._base, self._cap = free, base_delay, max_delay
        # Remembering more failures than the cap needs changes no answer; bounding the
        # window bounds the memory an attacker's stream of failures can take.
        self._failures = SlidingWindowRateLimiter(
            max_hits=_failures_to_cap(free=free, base=base_delay, cap=max_delay),
            window_seconds=window_seconds,
            clock=clock,
        )

    @property
    def max_delay(self) -> float:
        return self._cap

    def delay(self, email: str) -> float:
        """Seconds to wait before answering this attempt on ``email``."""
        failures = self._failures.count(normalise_email(email))
        return login_failure_delay(failures, free=self._free, base=self._base, cap=self._cap)

    def record_failure(self, email: str) -> None:
        """Count a failed attempt: an unknown address, a wrong password, a deactivated account."""
        self._failures.hit(normalise_email(email))

    def clear(self, email: str) -> None:
        """A successful sign-in forgets the address's failures, so the owner's own typos
        stop costing them once they are in."""
        self._failures.forget(normalise_email(email))

    def reset(self) -> None:
        self._failures.reset()


class AuthLimiters:
    """Every budget of the signed-out routes, built once per app.

    ``login`` (per IP), ``login_failures`` (per address, a delay — see
    :class:`LoginFailureThrottle`), ``challenge`` (per IP) and ``challenge_subject`` (per
    user id of the challenge token) for the 2FA and set-password steps, ``register`` (per
    IP), ``reset_ip`` and ``reset_address`` for the reset request, and
    ``verification_resend`` (per recipient). Charge an IP window with ``hit`` and answer a
    non-``None`` result with ``429`` and ``Retry-After``
    (:func:`~eifi1_server_kit.limiter.retry_after_header`); charge it BEFORE any hashing,
    so a shed request costs no bcrypt. Key every address window by
    :func:`~eifi1_server_kit.auth.normalise_email` of what was submitted.
    """

    def __init__(
        self,
        *,
        login: Budget = LOGIN_PER_IP,
        challenge: Budget = CHALLENGE_PER_IP,
        challenge_subject: Budget = CHALLENGE_PER_SUBJECT,
        register: Budget = REGISTER_PER_IP,
        reset_ip: Budget = RESET_PER_IP,
        reset_address: Budget = RESET_PER_ADDRESS,
        verification_resend: Budget = VERIFICATION_RESEND_PER_RECIPIENT,
        login_failures: LoginFailureThrottle | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        self.login = login.limiter(clock)
        self.login_failures = login_failures or LoginFailureThrottle(clock=clock)
        self.challenge = challenge.limiter(clock)
        self.challenge_subject = challenge_subject.limiter(clock)
        self.register = register.limiter(clock)
        self.reset_ip = reset_ip.limiter(clock)
        self.reset_address = reset_address.limiter(clock)
        self.verification_resend = verification_resend.limiter(clock)

    def reset(self) -> None:
        """Empty every window — the test seam."""
        for limiter in (
            self.login,
            self.challenge,
            self.challenge_subject,
            self.register,
            self.reset_ip,
            self.reset_address,
            self.verification_resend,
        ):
            limiter.reset()
        self.login_failures.reset()
