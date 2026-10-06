"""The account: the address, the tag, the gate, names and erasure (§3, §4, §8)."""

from __future__ import annotations

import pytest

from eifi1_server_kit.auth import (
    RegistrationDecision,
    addresses_for_reset,
    env_list_match,
    erasure_identifiers,
    full_name,
    invitation_accepts,
    name_incomplete,
    normalise_email,
    parse_env_list,
    registration_decision,
    tagged_variant,
    verified_by_invitation,
)
from eifi1_server_kit.feedback import ERASED_MARK, anonymise_feedback


def test_normalise_trims_and_lower_cases_and_keeps_the_tag() -> None:
    assert normalise_email("  Ada+Kastlan@Example.COM\t") == "ada+kastlan@example.com"
    assert normalise_email("a.d.a@example.com") == "a.d.a@example.com", "no provider aliasing"
    assert normalise_email("ada@example.com") != normalise_email("ada+kastlan@example.com")


def test_tagged_variant_is_keksdoses_tagged_email() -> None:
    """keksdose ``email-tag.ts``: one click with the exact result, never stacked."""
    assert tagged_variant(" Ada@Example.com ", "kastlan") == "ada+kastlan@example.com"
    assert tagged_variant("ada@example.com", " Kurvenschmiede ") == "ada+kurvenschmiede@example.com"
    assert tagged_variant("ada+news@example.com", "kastlan") is None, "their own scheme wins"
    for incomplete in ("", "ada", "@example.com", "ada@", "ada@example", "ada@.example.com", "ada@example."):
        assert tagged_variant(incomplete, "kastlan") is None, incomplete
    # The LAST @ splits, as the frontend's lastIndexOf does.
    assert tagged_variant('"a@b"@example.com', "kastlan") == '"a@b"+kastlan@example.com'
    for bad in ("", "  ", "has space", "a+b", "x@y", "-lead"):
        with pytest.raises(ValueError, match="address tag"):
            tagged_variant("ada@example.com", bad)


def test_an_invitation_accepts_the_exact_address_or_the_apps_own_tag() -> None:
    invited = "ada@example.com"
    assert invitation_accepts(invited, " ADA@example.com", "kastlan")
    assert invitation_accepts(invited, "ada+kastlan@example.com", "kastlan")
    assert invitation_accepts(invited, "Ada+Kastlan@Example.com", "kastlan")
    assert not invitation_accepts(invited, "ada+keksdose@example.com", "kastlan"), "another app's tag"
    assert not invitation_accepts(invited, "ada+kastlan@example.org", "kastlan"), "another domain"
    assert not invitation_accepts(invited, "ada2@example.com", "kastlan")
    assert not invitation_accepts(invited, "ada+kastlan+x@example.com", "kastlan")
    # A tagged invitation accepts itself only, not the bare address.
    assert invitation_accepts("ada+news@example.com", "ada+news@example.com", "kastlan")
    assert not invitation_accepts("ada+news@example.com", "ada@example.com", "kastlan")
    assert not invitation_accepts("ada+news@example.com", "ada+news+kastlan@example.com", "kastlan")


def test_only_the_exact_invited_address_counts_as_verified() -> None:
    """§4.3 / §10.7: the token reached you@x, which proves nothing about you+app@x."""
    assert verified_by_invitation("ada@example.com", " Ada@Example.com ")
    assert not verified_by_invitation("ada@example.com", "ada+kastlan@example.com")


def test_a_reset_falls_back_to_the_tagged_address() -> None:
    assert addresses_for_reset(" Ada@Example.com", "kastlan") == ("ada@example.com", "ada+kastlan@example.com")
    assert addresses_for_reset("ada+kastlan@example.com", "kastlan") == ("ada+kastlan@example.com",)
    assert addresses_for_reset("not-an-address", "kastlan") == ("not-an-address",)


def test_the_env_list_is_parsed_like_keksdoses() -> None:
    assert parse_env_list(" Ada@Example.com, bob@example.com\n carol@example.com ,,") == {
        "ada@example.com",
        "bob@example.com",
        "carol@example.com",
    }
    assert parse_env_list("") == frozenset() and parse_env_list(None) == frozenset()
    assert env_list_match("ADA@example.com", parse_env_list("ada@example.com")) is True
    assert env_list_match("bob@example.com", ["ada@example.com"]) is False
    assert env_list_match("ada@example.com", []) is None, "no list set"


def test_the_gate() -> None:
    """§4.3 / §2.12: first user = admin; then an invitation (an allow-list entry is one)."""

    def decide(first: bool, env: bool | None, invited: bool) -> RegistrationDecision:
        return registration_decision(is_first_user=first, on_env_list=env, has_valid_invitation=invited)

    first_admin = RegistrationDecision.FIRST_ADMIN
    assert decide(True, None, False) is first_admin, "no env list: the first registration is open"
    assert decide(True, True, False) is first_admin
    assert decide(True, False, False) is RegistrationDecision.CLOSED, "the env list guards the bootstrap"
    assert decide(False, None, True) is RegistrationDecision.INVITED
    assert decide(False, True, True) is RegistrationDecision.INVITED
    for env in (None, True, False):
        assert decide(False, env, False) is RegistrationDecision.CLOSED, "the env list grants nothing later"
    assert first_admin.allowed and first_admin.first_admin
    assert RegistrationDecision.INVITED.allowed and not RegistrationDecision.INVITED.first_admin
    assert not RegistrationDecision.CLOSED.allowed and not RegistrationDecision.CLOSED.first_admin


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("de-CH", "Ada Example"),
        ("en", "Ada Example"),
        ("fr", "Ada Example"),
        ("it", "Ada Example"),
        ("es", "Ada Example"),
        ("hu", "Example Ada"),
        ("hu-HU", "Example Ada"),
        # A Latin name stays as written in zh; only a CJK name is family-first, no space.
        ("zh", "Ada Example"),
        ("zh_Hans", "Ada Example"),
        ("ZH-CN", "Ada Example"),
        ("xx", "Ada Example"),
        (None, "Ada Example"),
        ("", "Ada Example"),
    ],
)
def test_full_name_follows_the_language(locale: str | None, expected: str) -> None:
    assert full_name(" Ada ", " Example ", locale) == expected


def test_full_name_in_zh_runs_a_cjk_name_together_family_first() -> None:
    assert full_name("小龙", "李", "zh") == "李小龙"
    assert full_name("小龙", "李", "zh-CN") == "李小龙"
    assert full_name("小龙", "李", "en") == "小龙 李", "outside zh the order is the reader's"
    assert full_name("Ada", "李", "zh") == "Ada 李", "a mixed name stays as written"


def test_full_name_with_a_part_missing_is_the_other_alone() -> None:
    assert full_name("Ada Lovelace", "", "hu") == "Ada Lovelace", "a migrated account"
    assert full_name("Ada", None, "zh") == "Ada"
    assert full_name("  ", "Example", "en") == "Example"
    assert full_name(None, None, "en") == ""


def test_name_incomplete() -> None:
    assert not name_incomplete("Ada", "Example")
    assert name_incomplete("Ada Lovelace", "")
    assert name_incomplete("Ada", "   ")
    assert name_incomplete(None, "Example")
    assert not name_incomplete("Ada Lovelace", "", is_demo=True), "a demo is never incomplete"


def test_erasure_identifiers_are_the_email_the_old_name_and_the_full_name_in_every_order() -> None:
    ids = erasure_identifiers("ada@example.com", "Ada", "Example", "Ada E.")
    assert set(ids) == {"ada@example.com", "Ada E.", "Ada Example", "Example Ada"}
    # A CJK name also gets zh's run-together form, the one its stamp holds.
    assert "李小龙" in erasure_identifiers(None, "小龙", "李", None)
    assert "Ada" not in ids and "Example" not in ids, "never a bare first or last name"
    assert [len(i) for i in ids] == sorted((len(i) for i in ids), reverse=True), "longest first"


def test_erasure_identifiers_drop_empties_duplicates_and_half_names() -> None:
    assert erasure_identifiers("ada@example.com", "Ada Lovelace", "", "Ada Lovelace") == [
        "ada@example.com",
        "Ada Lovelace",
    ]
    assert erasure_identifiers(None, "Mai", "", None) == [], "a bare first name is never a needle"
    assert erasure_identifiers("", "Ada", "Example", " ada example ") == [
        "ada example",
        "Example Ada",
    ]


def test_anonymise_feedback_takes_them_unchanged() -> None:
    """§8: the identifiers go straight into the feedback erasure, longest first, so an
    old display name inside the new full name leaves nothing behind."""
    ids = erasure_identifiers("ada@example.com", "Ada", "Lovelace-Byron", "Ada Lovelace")
    body = (
        "User: ada@example.com\nReported by Ada Lovelace-Byron (Lovelace-Byron Ada), "
        "once Ada Lovelace. Ada says the Bank of May is down."
    )
    changes = anonymise_feedback(
        title="Ada Lovelace-Byron cannot log in",
        body=body,
        context=None,
        screenshot_url=None,
        attachment_urls=None,
        identifiers=ids,
    ).changes
    assert changes["title"] == f"{ERASED_MARK} cannot log in"
    assert changes["body"] == (
        f"User: {ERASED_MARK}\nReported by {ERASED_MARK} ({ERASED_MARK}), once {ERASED_MARK}. "
        "Ada says the Bank of May is down."
    )
