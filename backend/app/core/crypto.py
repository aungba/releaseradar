"""Secret handling.

* Provider access tokens / webhook secrets must be *recoverable* -> symmetric encryption (Fernet).
* ReleaseRadar user access tokens only need *verification* -> store a SHA-256 hash.
  SHA-256 (not bcrypt) is sufficient because tokens are 256-bit random values, not passwords.
"""

import hashlib
import secrets
from dataclasses import dataclass

from cryptography.fernet import Fernet

from app.core.config import get_settings

API_TOKEN_PREFIX = "rr_"
_DISPLAY_PREFIX_LEN = 8


def _fernet(key: str | None = None) -> Fernet:
    key = key if key is not None else get_settings().encryption_key
    if not key:
        raise RuntimeError("RR_ENCRYPTION_KEY is not configured")
    return Fernet(key.encode())


def encrypt_secret(plaintext: str, key: str | None = None) -> str:
    return _fernet(key).encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str, key: str | None = None) -> str:
    return _fernet(key).decrypt(ciphertext.encode()).decode()


@dataclass(frozen=True)
class IssuedApiToken:
    plaintext: str  # shown to the user exactly once
    display_prefix: str  # stored, e.g. "rr_8f3kQa"
    token_hash: str  # stored, SHA-256 hex


def hash_api_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def generate_api_token() -> IssuedApiToken:
    plaintext = API_TOKEN_PREFIX + secrets.token_urlsafe(32)
    return IssuedApiToken(
        plaintext=plaintext,
        display_prefix=plaintext[: len(API_TOKEN_PREFIX) + _DISPLAY_PREFIX_LEN],
        token_hash=hash_api_token(plaintext),
    )
