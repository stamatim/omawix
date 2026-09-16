from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from omawix_reader import ReaderServer, zim_name_from_path


class ZimNameTests(unittest.TestCase):
    def test_derives_kiwix_name_from_split_archive(self) -> None:
        self.assertEqual(
            zim_name_from_path("Café + Guide.ZIMAA"), "cafe_plus_guide"
        )

    def test_rejects_other_extensions(self) -> None:
        with self.assertRaisesRegex(ValueError, r"\.zim or \.zimaa"):
            zim_name_from_path("archive.zip")


class ReaderServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.archive = Path(self.temporary.name) / "Café + Guide.zim"
        self.archive.write_bytes(b"zim")

    def make_process(self) -> Mock:
        process = Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        return process

    def test_validates_archive_path(self) -> None:
        with self.assertRaisesRegex(FileNotFoundError, "does not exist"):
            ReaderServer(Path(self.temporary.name) / "missing.zim")

        not_an_archive = Path(self.temporary.name) / "notes.txt"
        not_an_archive.write_text("notes", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, r"\.zim or \.zimaa"):
            ReaderServer(not_an_archive)

    def test_starts_with_safe_options_and_exposes_urls(self) -> None:
        process = self.make_process()
        popen = Mock(return_value=process)
        response = Mock()
        urlopen = Mock(return_value=response)
        server = ReaderServer(
            self.archive,
            executable="/usr/bin/kiwix-serve",
            popen_factory=popen,
            urlopen_func=urlopen,
            port_selector=lambda: 41234,
        )

        result = server.start()

        self.assertIs(result, server)
        popen.assert_called_once_with(
            [
                "/usr/bin/kiwix-serve",
                "--address=127.0.0.1",
                "--port=41234",
                "--blockexternal",
                "--nolibrarybutton",
                f"--attachToProcess={os.getpid()}",
                str(self.archive.resolve()),
            ]
        )
        self.assertEqual(server.viewer_url, "http://127.0.0.1:41234/viewer#cafe_plus_guide")
        self.assertEqual(server.content_url, "http://127.0.0.1:41234/content/cafe_plus_guide")
        self.assertEqual(server.home_url, server.content_url)
        self.assertEqual(urlopen.call_args.args[0], "http://127.0.0.1:41234/")
        self.assertLessEqual(urlopen.call_args.kwargs["timeout"], 0.25)
        response.close.assert_called_once_with()

    def test_reports_missing_executable_before_launch(self) -> None:
        popen = Mock()
        server = ReaderServer(
            self.archive,
            executable_finder=Mock(return_value=None),
            popen_factory=popen,
        )

        with self.assertRaisesRegex(FileNotFoundError, "kiwix-serve.*PATH"):
            server.start()

        popen.assert_not_called()

    def test_times_out_and_stops_process(self) -> None:
        process = self.make_process()
        monotonic = Mock(side_effect=[0.0, 0.0, 1.0])
        server = ReaderServer(
            self.archive,
            executable="kiwix-serve",
            startup_timeout=0.5,
            popen_factory=Mock(return_value=process),
            urlopen_func=Mock(side_effect=URLError("not ready")),
            port_selector=lambda: 41234,
            sleep_func=Mock(),
            monotonic_func=monotonic,
        )

        with self.assertRaisesRegex(TimeoutError, "within 0.5 seconds"):
            server.start()

        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=3.0)
        self.assertIsNone(server.process)
        self.assertIsNone(server.port)

    def test_stop_kills_process_that_does_not_terminate(self) -> None:
        process = self.make_process()
        process.wait.side_effect = [
            subprocess.TimeoutExpired("kiwix-serve", 0.1),
            0,
        ]
        server = ReaderServer(
            self.archive,
            executable="kiwix-serve",
            shutdown_timeout=0.1,
            popen_factory=Mock(return_value=process),
            urlopen_func=Mock(return_value=Mock()),
            port_selector=lambda: 41234,
        ).start()

        server.stop()

        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(process.wait.call_count, 2)
        self.assertFalse(server.is_running)

    def test_restart_stops_old_process_and_selects_a_new_port(self) -> None:
        first_process = self.make_process()
        second_process = self.make_process()
        popen = Mock(side_effect=[first_process, second_process])
        ports = iter([41001, 41002])
        server = ReaderServer(
            self.archive,
            executable="kiwix-serve",
            popen_factory=popen,
            urlopen_func=Mock(return_value=Mock()),
            port_selector=lambda: next(ports),
        ).start()

        result = server.restart()

        self.assertIs(result, server)
        first_process.terminate.assert_called_once_with()
        self.assertEqual(server.port, 41002)
        self.assertIs(server.process, second_process)
        self.assertEqual(popen.call_count, 2)


if __name__ == "__main__":
    unittest.main()
