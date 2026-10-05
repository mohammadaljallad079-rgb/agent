"""Webhook signature verification.

Inbound webhooks are authenticated by an HMAC-SHA256 signature, not an API key:
the sender proves it knows the shared secret. The signed message binds the
timestamp as well as the body, and the timestamp is checked against a tolerance
window, so a captured delivery cannot be replayed later.

The signature scheme mirrors the common ``sha256=<hex>`` convention:
``signature == "sha256=" + hex(HMAC_SHA256(secret, f"{timestamp}.{body}"))``.
"""

from __future__ import annotations

import hashlib
import hmac
import time


def compute_signature(secret: str, *, timestamp: str, body: bytes) -> str:
    """Return the ``sha256=<hex>`` signature for a delivery."""
    message = timestamp.encode() + b"." + body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(
    secret: str,
    *,
    timestamp: str,
    body: bytes,
    signature: str,
    tolerance_seconds: int,
    now: float | None = None,
) -> bool:
    """Verify a delivery's signature and freshness.

    Returns ``False`` (never raises) for a missing/malformed timestamp, a
    timestamp outside the tolerance window, or a signature that does not match.
    """
    if not secret or not signature or not timestamp:
        return False
    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    if abs(current - sent_at) > tolerance_seconds:
        return False
    expected = compute_signature(secret, timestamp=timestamp, body=body)
    return hmac.compare_digest(expected, signature)
