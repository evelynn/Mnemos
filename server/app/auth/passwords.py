"""Password hashing + policy (PR-39 closes audit E5)."""

from __future__ import annotations

import re

from passlib.context import CryptContext

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


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


def hash_password(plain: str) -> str:
    """Hash a password *after* running it through the policy.

    Centralising the check here means every entry point (CLI
    create-user, REST POST /users, change-password) gets the same
    rules. Tests and migrations that need to bypass the policy can
    call ``_pwd_context.hash(...)`` directly.
    """
    validate_password_policy(plain)
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_context.verify(plain, hashed)
