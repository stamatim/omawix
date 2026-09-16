from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InstallScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "home"
        self.data = Path(self.temporary.name) / "data"
        self.bin = Path(self.temporary.name) / "bin"
        self.home.mkdir()
        self.bin.mkdir()
        for command in ("python3", "kiwix-serve", "aria2c"):
            executable = self.bin / command
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
        self.environment = {
            **os.environ,
            "HOME": str(self.home),
            "XDG_DATA_HOME": str(self.data),
            "PATH": f"{self.bin}:{os.environ['PATH']}",
        }

    def run_script(self, name: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(ROOT / name)],
            cwd=ROOT,
            env=self.environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_setup_refuses_to_overwrite_a_modified_installed_asset(self) -> None:
        self.assertEqual(self.run_script("setup").returncode, 0)
        desktop = self.data / "applications/io.github.stamatim.omawix.desktop"
        desktop.write_text("modified by another installer\n", encoding="utf-8")

        result = self.run_script("setup")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not owned", result.stderr)
        self.assertEqual(desktop.read_text(encoding="utf-8"), "modified by another installer\n")

    def test_setup_does_not_claim_an_identical_unowned_asset(self) -> None:
        desktop = self.data / "applications/io.github.stamatim.omawix.desktop"
        desktop.parent.mkdir(parents=True)
        desktop.write_bytes(
            (ROOT / "app/io.github.stamatim.omawix.desktop").read_bytes()
        )

        result = self.run_script("setup")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not owned", result.stderr)
        self.assertFalse((self.data / "omawix/install-manifest").exists())

    def test_remove_deletes_owned_files_but_preserves_modified_assets(self) -> None:
        self.assertEqual(self.run_script("setup").returncode, 0)
        desktop = self.data / "applications/io.github.stamatim.omawix.desktop"
        icon = self.data / "icons/hicolor/scalable/apps/io.github.stamatim.omawix.svg"
        desktop.write_text("modified by user\n", encoding="utf-8")

        result = self.run_script("remove-app")

        self.assertEqual(result.returncode, 0)
        self.assertTrue(desktop.exists())
        self.assertFalse(icon.exists())
        self.assertIn("Preserving modified or unowned file", result.stderr)


if __name__ == "__main__":
    unittest.main()
