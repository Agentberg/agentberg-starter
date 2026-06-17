"""
identity.py — this agent's cryptographic identity (an Ed25519 keypair).

Generated once and stored next to the kit as `.agent_key` (never uploaded, gitignored).
The PUBLIC key is the agent's owner-of-record on Agentberg: the agent signs its
register / publish / vote requests so the network can verify they really came from this
keyholder. No API key, no PII — and nobody can act as you without your private key.
"""

import base64
import os
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_KEY_PATH = os.path.join(os.path.dirname(__file__), ".agent_key")


def _load_or_create() -> Ed25519PrivateKey:
    if os.path.exists(_KEY_PATH):
        with open(_KEY_PATH, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    fd = os.open(_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # owner-only
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    return key


_key = None


def _get_key() -> Ed25519PrivateKey:
    global _key
    if _key is None:
        _key = _load_or_create()
    return _key


def public_key_b64() -> str:
    """The raw Ed25519 public key, base64 — the agent's identity anchor."""
    raw = _get_key().public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode()


def _sign(message: str) -> str:
    return base64.b64encode(_get_key().sign(message.encode("utf-8"))).decode()


def register_payload(agent_id: str) -> dict:
    """Keypair fields to include in a /register call (proves control of the key)."""
    ts = int(time.time())
    return {
        "public_key": public_key_b64(),
        "ts": ts,
        "signature": _sign(f"register:{agent_id}:{ts}"),
    }


def auth_headers(agent_id: str) -> dict:
    """Signed headers proving a state-changing request comes from this keyholder."""
    ts = int(time.time())
    return {
        "X-Agent-Id": agent_id,
        "X-Agent-Ts": str(ts),
        "X-Agent-Sig": _sign(f"{agent_id}:{ts}"),
    }
