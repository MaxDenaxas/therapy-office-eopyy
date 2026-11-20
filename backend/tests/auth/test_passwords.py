"""Password policy and Argon2id hashing."""

from __future__ import annotations

import pytest
from argon2 import PasswordHasher, Type

from app.auth import passwords as pw


@pytest.mark.parametrize("username", ["abc", "office-admin", "a.b_c-1", "x" * 64])
def test_valid_usernames(username):
    assert pw.check_username(username) == username


@pytest.mark.parametrize("username", ["", "ab", "x" * 65, "with space", "Ελένη", "a/b", "a@b"])
def test_invalid_usernames(username):
    with pytest.raises(pw.PolicyError):
        pw.check_username(username)


def test_password_length_limits():
    pw.check_password("x" * 12, "admin")
    pw.check_password("x" * 128, "admin")
    with pytest.raises(pw.PolicyError, match="at least 12"):
        pw.check_password("x" * 11, "admin")
    with pytest.raises(pw.PolicyError, match="at most 128"):
        pw.check_password("x" * 129, "admin")


def test_password_must_not_equal_username():
    with pytest.raises(pw.PolicyError, match="username"):
        pw.check_password("Office-Admin-1", "office-admin-1")


def test_policy_errors_never_contain_the_password():
    secret = "short-pw"
    with pytest.raises(pw.PolicyError) as info:
        pw.check_password(secret, "admin")
    assert secret not in str(info.value)


def test_length_is_counted_after_nfc_normalisation():
    decomposed = "é" * 6  # six "é" written as two code points each: 12 code points
    with pytest.raises(pw.PolicyError, match="at least 12"):
        pw.check_password(decomposed, "admin")


def test_hash_is_argon2id_salted_and_verifies():
    a, b = pw.hash_password("correct horse battery"), pw.hash_password("correct horse battery")
    assert a.startswith("$argon2id$") and pw.is_argon2id(a)
    assert a != b  # random salt
    assert "correct horse battery" not in a
    assert pw.verify_password(a, "correct horse battery")
    assert not pw.verify_password(a, "correct horse batterY")


def test_normalised_forms_verify_identically():
    stored = pw.hash_password("café au lait 123")  # precomposed é
    assert pw.verify_password(stored, "café au lait 123")  # decomposed é


def test_malformed_or_foreign_hashes_never_verify():
    for bad in ["", "plain", "$argon2id$garbage", "$2b$12$abcdefghijklmnopqrstuv"]:
        assert not pw.verify_password(bad, "anything at all")


def test_overlong_passwords_are_rejected_without_error():
    stored = pw.hash_password("x" * 128)
    assert not pw.verify_password(stored, "x" * 5000)


def test_weaker_parameters_need_a_rehash():
    weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1, type=Type.ID)
    old = weak.hash("correct horse battery")
    assert pw.needs_rehash(old)
    assert pw.verify_password(old, "correct horse battery")  # still accepted
    assert not pw.needs_rehash(pw.hash_password("correct horse battery"))
    assert pw.needs_rehash("not a hash")


def test_dummy_verify_has_no_effect():
    pw.dummy_verify("whatever")
    pw.dummy_verify("x" * 5000)
