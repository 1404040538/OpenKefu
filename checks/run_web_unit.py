from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from run_web import frontend_build_issue


class FrontendBuildFreshnessTest(unittest.TestCase):
    def test_missing_build_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            issue = frontend_build_issue("web/dist", project_root=Path(tmp))

        self.assertIn("missing", issue or "")

    def test_stale_build_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dist_index = root / "web" / "dist" / "index.html"
            source = root / "web" / "src" / "App.tsx"
            dist_index.parent.mkdir(parents=True)
            source.parent.mkdir(parents=True)
            dist_index.write_text("old", encoding="utf-8")
            source.write_text("new", encoding="utf-8")
            os.utime(dist_index, (1, 1))
            os.utime(source, (2, 2))

            issue = frontend_build_issue("web/dist", project_root=root)

        self.assertIn("stale", issue or "")

    def test_fresh_build_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dist_index = root / "web" / "dist" / "index.html"
            source = root / "web" / "src" / "App.tsx"
            dist_index.parent.mkdir(parents=True)
            source.parent.mkdir(parents=True)
            source.write_text("source", encoding="utf-8")
            dist_index.write_text("fresh", encoding="utf-8")
            os.utime(source, (1, 1))
            os.utime(dist_index, (2, 2))

            issue = frontend_build_issue("web/dist", project_root=root)

        self.assertIsNone(issue)


if __name__ == "__main__":
    unittest.main()
