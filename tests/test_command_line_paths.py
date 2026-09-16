from __future__ import annotations

import runpy
import tempfile
import unittest
from pathlib import Path


APP_PATH = Path(__file__).resolve().parents[1] / "app" / "omawix"
resolve_command_line_path = runpy.run_path(str(APP_PATH))["resolve_command_line_path"]


class CommandLinePathTests(unittest.TestCase):
    def test_resolves_relative_path_from_remote_command_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            remote_cwd = Path(temporary) / "remote"
            remote_cwd.mkdir()

            result = resolve_command_line_path("archives/book.zim", str(remote_cwd))

            self.assertEqual(result, remote_cwd / "archives" / "book.zim")

    def test_preserves_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            absolute = Path(temporary) / "book.zim"

            result = resolve_command_line_path(str(absolute), "/unrelated")

            self.assertEqual(result, absolute)


if __name__ == "__main__":
    unittest.main()
