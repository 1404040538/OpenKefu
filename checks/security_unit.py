from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openkefu.web.config import load_config
from openkefu.web.crypto import ENCRYPTED_PREFIX, TextCipher
from openkefu.web.runtime_bus import RuntimeCommandBus


STRONG_A = "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"
STRONG_B = "z9Y8x7W6v5U4t3S2r1Q0p9O8n7M6l5K4"
STRONG_C = "m1N2b3V4c5X6z7A8s9D0f1G2h3J4k5L6"


class SecurityConfigTest(unittest.TestCase):
    def _write_config(
        self,
        path: Path,
        *,
        jwt_secret: str,
        command_secret: str,
        data_key: str,
        runtime_role: str = "both",
        redis_url: str = "redis://127.0.0.1:6379/0",
    ) -> None:
        path.write_text(
            json.dumps(
                {
                    "server": {"host": "127.0.0.1", "port": 8000, "frontend_dist": "web/dist"},
                    "mysql": {"host": "127.0.0.1", "port": 3306, "user": "root", "password": "", "database": "openkefu"},
                    "redis": {"url": redis_url},
                    "runtime": {"role": runtime_role, "command_secret": command_secret},
                    "security": {"jwt_secret": jwt_secret, "data_encryption_key": data_key},
                    "logging": {"path": "logs/test.log", "level": "INFO"},
                }
            ),
            encoding="utf-8",
        )

    def test_reads_secrets_from_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            self._write_config(path, jwt_secret=STRONG_A, command_secret=STRONG_B, data_key=STRONG_C)

            config = load_config(path)

        self.assertEqual(config.security.jwt_secret, STRONG_A)
        self.assertEqual(config.runtime.command_secret, STRONG_B)
        self.assertEqual(config.security.data_encryption_key, STRONG_C)

    def test_env_secret_values_override_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            self._write_config(path, jwt_secret=STRONG_A, command_secret=STRONG_B, data_key=STRONG_C)
            with patch.dict(
                os.environ,
                {
                    "OPENKEFU_JWT_SECRET": STRONG_C,
                    "OPENKEFU_RUNTIME_COMMAND_SECRET": STRONG_A,
                    "OPENKEFU_DATA_ENCRYPTION_KEY": STRONG_B,
                },
                clear=False,
            ):
                config = load_config(path)

        self.assertEqual(config.security.jwt_secret, STRONG_C)
        self.assertEqual(config.runtime.command_secret, STRONG_A)
        self.assertEqual(config.security.data_encryption_key, STRONG_B)

    def test_missing_secrets_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            self._write_config(path, jwt_secret="", command_secret="", data_key="")

            with self.assertRaises(RuntimeError):
                load_config(path)

    def test_rejects_distributed_runtime_without_redis_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            self._write_config(
                path,
                jwt_secret=STRONG_A,
                command_secret=STRONG_B,
                data_key=STRONG_C,
                runtime_role="api",
                redis_url="redis://127.0.0.1:6379/0",
            )

            with self.assertRaises(RuntimeError):
                load_config(path)


class RuntimeBusSecurityTest(unittest.TestCase):
    def test_command_payload_is_encrypted_and_replay_protected(self):
        bus = RuntimeCommandBus("redis://127.0.0.1:6379/0", STRONG_B)

        raw = bus._encode_message({"params": {"password": "plain-secret"}}, ttl_seconds=30)

        self.assertNotIn("plain-secret", raw)
        decoded = bus._decode_message(raw)
        self.assertEqual(decoded["params"]["password"], "plain-secret")
        with self.assertRaises(ValueError):
            bus._decode_message(raw)


class TextCipherTest(unittest.TestCase):
    def test_encrypts_with_prefix_and_accepts_plain_legacy_values(self):
        cipher = TextCipher(STRONG_C)

        encrypted = cipher.encrypt("secret-cookie")

        self.assertTrue(encrypted.startswith(ENCRYPTED_PREFIX))
        self.assertNotIn("secret-cookie", encrypted)
        self.assertEqual(cipher.decrypt(encrypted), "secret-cookie")
        self.assertEqual(cipher.decrypt("legacy-cookie"), "legacy-cookie")


if __name__ == "__main__":
    unittest.main()
