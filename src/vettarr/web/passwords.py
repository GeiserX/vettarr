"""The admin password: how it is hashed, and how a hash is checked.

``web.auth.password_hash`` holds one line, written by
``vettarr web hash-password``::

    scrypt:<n>:<r>:<p>:<salt hex>:<key hex>

No ``$`` anywhere in it, so the line survives a compose ``.env`` file and
``${VAR}`` expansion untouched. scrypt is the standard library's
(``hashlib.scrypt``); the cost written here is OWASP's N=2^15, r=8, p=3,
about 33 MB and a tenth of a second per check.

Standard library only: the config schema imports this to validate the field.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

SCHEME = "scrypt"
N, R, P = 2**15, 8, 3
SALT_BYTES = 16
KEY_BYTES = 32
MIN_LENGTH = 8
# scrypt needs 128 * n * r bytes. A hash asking for more than this is refused
# before it is computed, so a hand-written line cannot make a login take the
# container's memory.
MAX_MEMORY = 256 * 1024 * 1024

NOT_A_HASH = (
    "not a password hash: it takes the line `vettarr web hash-password` prints "
    "(scrypt:<n>:<r>:<p>:<salt>:<key>)"
)


@dataclass(frozen=True)
class Hash:
    n: int
    r: int
    p: int
    salt: bytes
    key: bytes


def _derive(password: str, salt: bytes, n: int, r: int, p: int, length: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=length,
        maxmem=128 * n * r * 2 + 1024 * 1024,
    )


def hash_password(password: str, *, n: int = N, r: int = R, p: int = P, salt: bytes | None = None) -> str:
    """The line to put in ``web.auth.password_hash``."""
    salt = secrets.token_bytes(SALT_BYTES) if salt is None else salt
    key = _derive(password, salt, n, r, p, KEY_BYTES)
    return f"{SCHEME}:{n}:{r}:{p}:{salt.hex()}:{key.hex()}"


def parse(line: str) -> Hash:
    """Read a hash line. Raises ``ValueError(NOT_A_HASH)``; the message never carries the line."""
    parts = line.strip().split(":")
    if len(parts) != 6 or parts[0] != SCHEME:
        raise ValueError(NOT_A_HASH)
    try:
        n, r, p = (int(x) for x in parts[1:4])
        salt, key = bytes.fromhex(parts[4]), bytes.fromhex(parts[5])
    except ValueError:
        raise ValueError(NOT_A_HASH) from None
    power_of_two = n > 1 and n & (n - 1) == 0
    if not power_of_two or not (1 <= r <= 32) or not (1 <= p <= 16) or 128 * n * r > MAX_MEMORY:
        raise ValueError(NOT_A_HASH)
    if len(salt) < 8 or len(key) < 16:
        raise ValueError(NOT_A_HASH)
    return Hash(n, r, p, salt, key)


def problem(line: str) -> str | None:
    """Why ``line`` is not a usable hash, or None."""
    try:
        parse(line)
    except ValueError as exc:
        return str(exc)
    return None


def verify(password: str, line: str) -> bool:
    """True when ``password`` is the one ``line`` was made from. Blocking: about 0.1 s."""
    try:
        stored = parse(line)
    except ValueError:
        return False
    candidate = _derive(password, stored.salt, stored.n, stored.r, stored.p, len(stored.key))
    return hmac.compare_digest(candidate, stored.key)
