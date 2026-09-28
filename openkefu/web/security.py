from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any


def hash_password(password: str, *, iterations: int = 260_000) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return (
        "pbkdf2_sha256"
        f"${iterations}"
        f"${base64.urlsafe_b64encode(salt).decode('ascii')}"
        f"${base64.urlsafe_b64encode(digest).decode('ascii')}"
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_text, digest_text = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            int(iterations),
        )
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * ((4 - len(text) % 4) % 4))


def create_token(payload: dict[str, Any], *, secret: str, expire_minutes: int) -> str:
    now = int(time.time())
    body = dict(payload)
    body.update({"iat": now, "exp": now + expire_minutes * 60})
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = ".".join(
        [
            _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            _b64url(json.dumps(body, separators=(",", ":")).encode("utf-8")),
        ]
    )
    signature = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def decode_token(token: str, *, secret: str) -> dict[str, Any]:
    try:
        header_text, payload_text, signature_text = token.split(".", 2)
    except ValueError as exc:
        raise ValueError("invalid token format") from exc

    try:
        header = json.loads(_b64url_decode(header_text).decode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid token header") from exc
    if not isinstance(header, dict) or header.get("alg") != "HS256" or header.get("typ") != "JWT":
        raise ValueError("unsupported token algorithm")

    signing_input = f"{header_text}.{payload_text}"
    expected = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    actual = _b64url_decode(signature_text)
    if not hmac.compare_digest(expected, actual):
        raise ValueError("invalid token signature")

    try:
        payload = json.loads(_b64url_decode(payload_text).decode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid token payload") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid token payload")
    if int(payload.get("exp") or 0) < int(time.time()):
        raise ValueError("token expired")
    if not payload.get("sub"):
        raise ValueError("token missing subject")
    return payload
