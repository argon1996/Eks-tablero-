import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SecuritySurfaceTests(unittest.TestCase):
    def test_browser_ui_does_not_generate_downloads(self):
        javascript = (ROOT / "eks_console" / "web" / "app.js").read_text(encoding="utf-8")
        for forbidden in ("new Blob(", "createObjectURL(", ".download="):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, javascript)

    def test_web_assets_are_local(self):
        html = (ROOT / "eks_console" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r'(?:src|href)=["\']https?://', html, re.IGNORECASE))

    def test_shortcut_does_not_bypass_execution_policy(self):
        installer = (ROOT / "install-shortcut.ps1").read_text(encoding="utf-8")
        self.assertNotIn("ExecutionPolicy Bypass", installer)
        self.assertNotIn("$shortcut.TargetPath = $powershellPath", installer)
        self.assertIn("Get-Command pyw.exe", installer)


if __name__ == "__main__":
    unittest.main()
