"""Custom SQLAlchemy column types.

Currently just `EncryptedString`, which transparently encrypts a value on the
way into Postgres and decrypts it on the way out.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import Text, TypeDecorator

from app.core.config import get_settings


class TokenEncryptionError(RuntimeError):
    """Raised when a value cannot be encrypted or decrypted."""


@lru_cache
def _cipher() -> MultiFernet:
    """Build the cipher from configured keys.

    WHY MultiFernet rather than a single Fernet: it decrypts with ANY key in
    the list but always encrypts with the FIRST. That is what makes key
    rotation possible without downtime:

        1. Prepend a new key:  TOKEN_ENCRYPTION_KEYS="new,old"
           -> new writes use `new`, existing rows still decrypt with `old`.
        2. Re-encrypt existing rows at leisure (MultiFernet.rotate).
        3. Drop `old` from the list once nothing needs it.

    With a single key, step 1 is impossible -- rotating means every stored
    token becomes unreadable at once, which for refresh tokens means every
    doctor has to re-authorize.

    WHY lazily built and cached rather than constructed at import: importing
    a model must not require the encryption key to be present. Alembic
    autogenerate, `--help`, and unit tests that never touch a token column
    would otherwise all need production secrets just to import the app.
    """
    keys = get_settings().token_encryption_key_list
    if not keys:
        raise TokenEncryptionError(
            "TOKEN_ENCRYPTION_KEYS is not set. Generate one with:\n"
            "  python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'"
        )
    return MultiFernet([Fernet(k.encode()) for k in keys])


class EncryptedString(TypeDecorator):
    """A Text column whose contents are encrypted at rest with Fernet.

    THREAT MODEL -- be honest about what this does and does not buy you:

      Protects against: a leaked database backup, a read-only SQL injection,
      a snapshot shared with a contractor, an engineer browsing prod tables.
      In all of those, the attacker gets ciphertext and no key.

      Does NOT protect against: compromise of the application process or its
      environment, because the key lives in the app's env vars. An attacker
      with code execution reads the key and the rows together.

    The real upgrade is a KMS (AWS KMS / GCP KMS / Vault) where the key never
    enters the app's memory in raw form and every decrypt is an audited API
    call. That is the right answer for production PHI-adjacent systems and is
    FLAGGED as a Phase 3+ task. Fernet-with-an-env-key is a genuine
    improvement over plaintext and a reasonable portfolio-stage choice, but
    it is not "encrypted" in the sense a hospital security review means.

    OPERATIONAL CAVEAT: Fernet ciphertext is non-deterministic (random IV +
    timestamp), so the same plaintext encrypts differently every time. That
    means you CANNOT index this column, use it in a WHERE clause, or enforce
    uniqueness on it. Always look these rows up by some other key (doctor_id).
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect) -> str | None:
        if value is None:
            return None
        return _cipher().encrypt(value.encode()).decode()

    def process_result_value(self, value: str | None, dialect) -> str | None:
        if value is None:
            return None
        try:
            return _cipher().decrypt(value.encode()).decode()
        except InvalidToken as exc:
            # Almost always means a key was removed from TOKEN_ENCRYPTION_KEYS
            # before the rows encrypted with it were rotated. Fail loudly --
            # silently returning None here would look like "doctor never
            # connected their calendar" and trigger a spurious re-auth prompt.
            raise TokenEncryptionError(
                "Could not decrypt value -- is the original key still in "
                "TOKEN_ENCRYPTION_KEYS?"
            ) from exc
