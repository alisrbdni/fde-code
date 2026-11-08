"""Optional Ed25519 webhook signature verification (Telnyx signed webhooks).

Uses the `cryptography` package when installed; when it is not, verification
is reported as unavailable and (VERIFY_SIGNATURES=false) skipped.
"""
from __future__ import annotations

import base64
from typing import Optional

from .log import log

available = False
try:  # optional at runtime
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # type: ignore
    from cryptography.hazmat.primitives.serialization import load_pem_public_key  # type: ignore
    from cryptography.exceptions import InvalidSignature  # type: ignore

    available = True
except Exception:  # pragma: no cover
    Ed25519PublicKey = None  # type: ignore


def verify_ed25519(public_key_pem: str, timestamp: str, payload: bytes, signature_b64: str) -> bool:
    """Verify `base64(signature) over timestamp + payload` with an Ed25519 key."""
    if not available or not public_key_pem:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # type: ignore
        from cryptography.exceptions import InvalidSignature  # type: ignore

        key = load_pem_public_key(public_key_pem.encode("utf-8"))
        if not isinstance(key, Ed25519PublicKey):
            return False
        raw = base64.b64decode(signature_b64)
        key.verify(raw, timestamp.encode("utf-8") + payload)
        return True
    except InvalidSignature:
        return False
    except Exception as exc:
        log("warn", "signature.verify_error", error=str(exc))
        return False


def check_webhook_signature(headers: dict, body: bytes, public_key_pem: str, enabled: bool) -> tuple[bool, str]:
    """Return (ok, reason). Disabled local mode always passes."""
    if not enabled:
        return True, "disabled"
    if not available:
        return False, "no_crypto_lib"
    timestamp = headers.get("telnyx-timestamp", "")
    signature = headers.get("telnyx-signature-ed25519", headers.get("webhook-signature", ""))
    if not timestamp or not signature:
        return False, "missing_headers"
    if verify_ed25519(public_key_pem, timestamp, body, signature):
        return True, "verified"
    return False, "bad_signature"
