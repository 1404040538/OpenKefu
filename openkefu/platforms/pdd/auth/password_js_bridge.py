import json
import subprocess
from pathlib import Path


DEFAULT_JS_TIMEOUT_SECONDS = 30


class PasswordJsBridge:
    """Thin wrapper around login_js/password_login.js.

    Provides password-login-specific operations that need the JS runtime:
    - Anti-content generation
    - Auth request building
    - Public key query request building
    - RiskSign generation
    """

    def __init__(self, js_file: Path):
        self.js_file = js_file

    def run(self, action: str, payload: dict | None = None) -> dict:
        try:
            proc = subprocess.run(
                ["node", str(self.js_file), action],
                input=json.dumps(payload or {}, ensure_ascii=False),
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
                timeout=DEFAULT_JS_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"password JS bridge timed out: action={action}") from exc
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
        return json.loads(proc.stdout)

    def build_auth_request(self, **kwargs) -> dict:
        return self.run("build-auth-request", kwargs)

    def build_risk_sign(self, username: str, encrypted_password: str, timestamp: int | None = None) -> dict:
        payload = {"username": username, "encryptedPassword": encrypted_password}
        if timestamp is not None:
            payload["timestamp"] = timestamp
        return self.run("build-risk-sign", payload)

    def query_password_encrypt_request(self, **kwargs) -> dict:
        return self.run("query-password-encrypt", kwargs)

    def anti_content(self, cookies: dict, fingerprint_env: dict | None = None) -> str:
        payload = {"cookies": cookies}
        if fingerprint_env:
            payload["fingerprintEnv"] = fingerprint_env
        return self.run("anti-content", payload)["antiContent"]
