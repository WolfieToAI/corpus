"""API key format, generation and verification.

Format: ``crp_<prefix>_<secret>``

* ``prefix`` (12 chars) is stored in clear and indexed: it identifies the key in O(1) and is
  safe to show in logs and dashboards.
* ``secret`` (43 chars, 256 bits of entropy) is only ever stored as a SHA-256 digest.

A slow password hash (argon2, bcrypt) is unnecessary here: those exist to resist brute force on
*low-entropy* human passwords. A 256-bit random secret cannot be brute-forced, so a fast,
constant-time comparison of digests is the right trade-off for a hot authentication path.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

KEY_PREFIX = "crp"
_PREFIX_LEN = 12
_FORMAT = re.compile(rf"^{KEY_PREFIX}_([A-Za-z0-9]{{{_PREFIX_LEN}}})_([A-Za-z0-9_-]{{43}})$")
_ALPHANUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


@dataclass(frozen=True, slots=True)
class GeneratedKey:
    plaintext: str  # shown to the user exactly once
    prefix: str
    secret_hash: str


@dataclass(frozen=True, slots=True)
class ParsedKey:
    prefix: str
    secret: str


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def generate_api_key() -> GeneratedKey:
    prefix = "".join(secrets.choice(_ALPHANUM) for _ in range(_PREFIX_LEN))
    secret = secrets.token_urlsafe(32)  # 32 bytes -> 43 url-safe characters
    return GeneratedKey(
        plaintext=f"{KEY_PREFIX}_{prefix}_{secret}", prefix=prefix, secret_hash=hash_secret(secret)
    )


def parse_api_key(value: str) -> ParsedKey | None:
    match = _FORMAT.match(value.strip())
    return ParsedKey(match.group(1), match.group(2)) if match else None


def verify_secret(secret: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(secret), expected_hash)
