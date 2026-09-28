from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


ENCRYPTED_PREFIX = "enc:v1:"


class TextCipher:
    def __init__(self, secret: str):
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
        self._fernet = Fernet(key)

    def encrypt(self, value: str | None) -> str | None:
        if value is None:
            return None
        if value.startswith(ENCRYPTED_PREFIX):
            return value
        token = self._fernet.encrypt(value.encode("utf-8")).decode("ascii")
        return ENCRYPTED_PREFIX + token

    def decrypt(self, value: str | None) -> str:
        if not value:
            return ""
        if not value.startswith(ENCRYPTED_PREFIX):
            return value
        token = value.removeprefix(ENCRYPTED_PREFIX)
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("invalid encrypted value") from exc
