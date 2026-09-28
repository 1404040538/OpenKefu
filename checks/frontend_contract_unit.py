from __future__ import annotations

import unittest

from openkefu.platforms.pdd.config import PROJECT_ROOT
from openkefu.web.app import WS_AUTH_PROTOCOL_PREFIX


class FrontendRealtimeContractTest(unittest.TestCase):
    def test_frontend_uses_backend_websocket_subprotocol(self):
        source = (PROJECT_ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertIn(WS_AUTH_PROTOCOL_PREFIX, source)
        self.assertNotIn("/ws?token", source)


if __name__ == "__main__":
    unittest.main()
