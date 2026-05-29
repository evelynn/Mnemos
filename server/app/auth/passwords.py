"""Password hashing + policy (PR-39 closes audit E5).

PR-135 — hash via the ``bcrypt`` library directly rather than
through passlib's ``CryptContext``. passlib 1.7.4 (its last release,
2020) runs a one-time backend self-test that feeds bcrypt a 73-byte
probe; bcrypt ≥ 4.1 raises ``ValueError`` on >72 bytes instead of
truncating, so passlib's bcrypt backend fails to initialise on any
modern install — breaking *all* hashing. Calling bcrypt directly
sidesteps the dead self-test. The on-disk format is unchanged
(standard ``$2b$`` bcrypt), so existing passlib-produced hashes
verify without migration.
"""

from __future__ import annotations

import re

import bcrypt

# bcrypt only consumes the first 72 bytes of the password; bcrypt 5
# *raises* on longer input rather than truncating, so we truncate
# explicitly. The LoginRequest/UserCreate bounds (PR-133) keep real
# inputs far below this, but defence-in-depth.
_BCRYPT_MAX_BYTES = 72
_BCRYPT_ROUNDS = 12  # matches the passlib default this replaces


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

MIN_LENGTH = 12
# A small list of the most-tried weak passwords. Not a substitute
# for a full breach corpus, but stops the easiest mistakes
# (123456789012, password1234, qwerty123456 …). The user-supplied
# password is lower-cased before this check so case variants don't
# slip through.
_WEAK_PASSWORDS = frozenset(
    {
        "password1234",
        "password12345",
        "password123456",
        "letmein12345",
        "qwerty123456",
        "12345678abcd",
        "abcdefghijkl",
        "passwordpassword",
        "administrator",
        "iloveyou1234",
    }
)

_DIGIT = re.compile(r"\d")
_LETTER = re.compile(r"[A-Za-z]")


class PasswordPolicyError(ValueError):
    """Raised when a password fails the policy. Carries a stable
    ``code`` so the GUI can translate the message."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate_password_policy(password: str) -> None:
    """Enforce the platform-wide password policy.

    Layered checks, in order — the first failure raises so the
    operator sees one clear reason at a time:

    1. ``MIN_LENGTH`` characters.
    2. Contains at least one letter and one digit. (A 12-char
       alphabetic-only string is a passphrase; a 12-char digit
       string is a PIN. Both are bad on their own.)
    3. Not on the small known-weak list.
    """
    if password is None or len(password) < MIN_LENGTH:
        raise PasswordPolicyError(
            "password_too_short",
            f"password must be at least {MIN_LENGTH} characters",
        )
    if not _LETTER.search(password) or not _DIGIT.search(password):
        raise PasswordPolicyError(
            "password_missing_letter_or_digit",
            "password must contain at least one letter and one digit",
        )
    if password.lower() in _WEAK_PASSWORDS:
        raise PasswordPolicyError(
            "password_too_common",
            "this password is on the well-known weak list; pick a different one",
        )


def _to_bcrypt_bytes(plain: str) -> bytes:
    """UTF-8 encode + truncate to bcrypt's 72-byte working limit."""
    return plain.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(plain: str) -> str:
    """Hash a password *after* running it through the policy.

    Centralising the check here means every entry point (CLI
    create-user, REST POST /users, change-password) gets the same
    rules.
    """
    validate_password_policy(plain)
    digest = bcrypt.hashpw(_to_bcrypt_bytes(plain), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS))
    return digest.decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time bcrypt verify. Tolerant of a malformed stored
    hash (returns False rather than raising) so a corrupted row can't
    500 the login path."""
    try:
        return bcrypt.checkpw(_to_bcrypt_bytes(plain), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False
