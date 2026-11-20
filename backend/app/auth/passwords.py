"""Password policy and Argon2id hashing (argon2-cffi, RFC 9106 low-memory profile).

The policy is deliberately simple: length (12–128 characters, counted after
Unicode NFC normalisation) and not equal to the username. No composition rules.
"""

from __future__ import annotations

import re
import unicodedata

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128
USERNAME_PATTERN = re.compile(r"[A-Za-z0-9._-]{3,64}")

# argon2-cffi's defaults (RFC 9106 second recommendation): t=3, m=64 MiB, p=4, Argon2id.
_hasher = PasswordHasher(type=Type.ID)

# Verified when the username is unknown, so a miss costs the same time as a hit.
_DUMMY_HASH = _hasher.hash("dummy password for timing equalisation only")


class PolicyError(ValueError):
    """A username or password that the policy refuses. The message never contains it."""


def normalise(password: str) -> str:
    return unicodedata.normalize("NFC", password)


def check_username(username: str) -> str:
    if not USERNAME_PATTERN.fullmatch(username):
        raise PolicyError("username must be 3–64 characters: letters, digits, '.', '_' or '-'")
    return username


def check_password(password: str, username: str) -> str:
    password = normalise(password)
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PolicyError(f"password must have at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PolicyError(f"password must have at most {MAX_PASSWORD_LENGTH} characters")
    if password.casefold() == username.casefold():
        raise PolicyError("password must not equal the username")
    return password


def hash_password(password: str) -> str:
    return _hasher.hash(normalise(password))


def verify_password(stored_hash: str, password: str) -> bool:
    """True only for a matching Argon2 hash. Malformed hashes count as a mismatch."""
    if len(password) > MAX_PASSWORD_LENGTH:  # can never match the policy; same cost anyway
        dummy_verify(password)
        return False
    try:
        return _hasher.verify(stored_hash, normalise(password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def dummy_verify(password: str) -> None:
    """Spend the same time as a real verification (unknown username)."""
    try:
        _hasher.verify(_DUMMY_HASH, normalise(password[:MAX_PASSWORD_LENGTH]))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        pass


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


def is_argon2id(stored_hash: str) -> bool:
    return stored_hash.startswith("$argon2id$")
