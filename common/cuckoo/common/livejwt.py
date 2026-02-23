# Copyright (C) 2019-2024 Estonian Information System Authority.
# See the file 'LICENSE' for copying permission.

import base64
import hmac
import hashlib
import json
import time


class LiveJWTError(Exception):
    pass


def _b64_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64_decode(s: str) -> bytes:
    # Re-add padding
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def issue_token(task_id: str, secret: str, ttl: int = 3600) -> str:
    """Issue a JWT-like token for the given task_id.

    Uses HMAC-SHA256 with a simple header.payload.signature format.
    ttl is in seconds (default 1 hour).
    """
    now = int(time.time())
    header = _b64_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64_encode(
        json.dumps({"task_id": task_id, "iat": now, "exp": now + ttl}).encode()
    )
    signing_input = f"{header}.{payload}"
    sig = hmac.new(
        secret.encode(), signing_input.encode(), hashlib.sha256
    ).digest()
    return f"{signing_input}.{_b64_encode(sig)}"


def verify_token(token: str, secret: str) -> dict:
    """Verify a token issued by issue_token.

    Returns the payload dict if valid.
    Raises LiveJWTError if invalid or expired.
    """
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
    except ValueError:
        raise LiveJWTError("Invalid token format")

    signing_input = f"{header_b64}.{payload_b64}"
    expected_sig = hmac.new(
        secret.encode(), signing_input.encode(), hashlib.sha256
    ).digest()

    try:
        actual_sig = _b64_decode(sig_b64)
    except Exception:
        raise LiveJWTError("Invalid token signature encoding")

    if not hmac.compare_digest(expected_sig, actual_sig):
        raise LiveJWTError("Token signature mismatch")

    try:
        payload = json.loads(_b64_decode(payload_b64))
    except Exception:
        raise LiveJWTError("Invalid token payload")

    now = int(time.time())
    if payload.get("exp", 0) < now:
        raise LiveJWTError("Token has expired")

    return payload
