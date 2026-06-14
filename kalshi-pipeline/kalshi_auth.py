"""
Kalshi request signing.

Kalshi's trade API authenticates every request with an RSA key pair instead of
an API key/secret. You create an API key in the Kalshi dashboard, which gives
you a key ID (a UUID) and a downloadable RSA private key (PEM). Each request is
signed by RSA-PSS-SHA256 signing the string `timestamp + METHOD + path`.

Three headers are sent on every request:
    KALSHI-ACCESS-KEY        the key ID (UUID)
    KALSHI-ACCESS-SIGNATURE  base64(RSA-PSS-SHA256(timestamp + method + path))
    KALSHI-ACCESS-TIMESTAMP  unix epoch milliseconds (as a string)

IMPORTANT: the signed `path` must be the full API path including
`/trade-api/v2/...` (and the websocket path for the WS handshake), NOT just the
query string.
"""
from __future__ import annotations

import base64
import functools
import time

import config


@functools.lru_cache(maxsize=1)
def _private_key():
    """Load and cache the RSA private key from the configured PEM file."""
    from cryptography.hazmat.primitives import serialization

    with open(config.KALSHI_PRIVATE_KEY_PATH, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def sign(method: str, path: str, timestamp_ms: str | None = None) -> dict[str, str]:
    """
    Build the three Kalshi auth headers for a request.

    `path` must include the full API prefix, e.g. "/trade-api/v2/markets".
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    ts = timestamp_ms or str(int(time.time() * 1000))
    message = (ts + method.upper() + path).encode()

    signature = _private_key().sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            # Class attribute (not the module-level constant) for broad
            # cryptography-version compatibility; this is what Kalshi's
            # reference clients use.
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )

    return {
        "KALSHI-ACCESS-KEY": config.KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }


def is_configured() -> bool:
    """True if a key ID and a readable private key file are both present."""
    import os

    return bool(config.KALSHI_API_KEY_ID) and os.path.exists(config.KALSHI_PRIVATE_KEY_PATH)
