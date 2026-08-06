"""Token generation and hashing.

Tokens carry their own high entropy (32+ bytes from `secrets`), so a plain
SHA-256 digest -- not a slow password hash -- is the right tool: fast lookups,
no per-token salt needed, and brute-forcing the digest is infeasible given the
token's entropy.
"""

import hashlib
import secrets

OWNER_TOKEN_PREFIX = "brnown_"
MACHINE_TOKEN_PREFIX = "brn_"


def _generate_token(prefix: str) -> str:
    # token_urlsafe(32) yields 43 base64url characters -- comfortably over the
    # "32+ chars of cryptographic randomness" requirement.
    return f"{prefix}{secrets.token_urlsafe(32)}"


def generate_owner_token() -> str:
    return _generate_token(OWNER_TOKEN_PREFIX)


def generate_machine_token() -> str:
    return _generate_token(MACHINE_TOKEN_PREFIX)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
