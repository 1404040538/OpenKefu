import json
import subprocess


DEFAULT_JS_TIMEOUT_SECONDS = 30


class QrcodeJsBridge:
    """对 login_js/qrcode_api.js 的轻量封装。

    前端兼容的请求构造与 Anti-Content 生成仍由 JS 侧负责。
    Python 侧只保留这个窄接口，避免 login.py 关心 subprocess 细节。
    """

    def __init__(self, js_file):
        self.js_file = js_file

    def run(self, action, payload=None):
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
            raise TimeoutError(f"qrcode JS bridge timed out: action={action}") from exc
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
        return json.loads(proc.stdout)

    def build_cookies(self, cookies):
        return self.run("cookies", {"cookies": cookies})

    def anti_content(self, cookies, fingerprint_env=None):
        payload = {"cookies": cookies}
        if fingerprint_env:
            payload["fingerprintEnv"] = fingerprint_env
        return self.run("anti-content", payload)["antiContent"]

    def randomize_fingerprint(self):
        return self.run("randomize-fingerprint")
