from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import omawix_library as library


class FilenameMetadataTests(unittest.TestCase):
    def test_parses_standard_kiwix_filename(self) -> None:
        result = library._filename_metadata(Path("wikipedia_en_all_maxi_2025-01.zim"))

        self.assertEqual(result["title"], "Wikipedia")
        self.assertEqual(result["language"], "English")
        self.assertEqual(result["edition"], "Full / Maxi")
        self.assertEqual(result["date"], "2025-01")

    def test_accepts_split_archive_first_part(self) -> None:
        self.assertTrue(library._is_primary_zim("wikipedia_en.zimaa"))
        self.assertFalse(library._is_primary_zim("wikipedia_en.zimab"))


class ConfigTests(unittest.TestCase):
    def test_saves_config_with_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary) / "omawix"
            config_path = config_dir / "config.json"
            with patch.object(library, "CONFIG_DIR", config_dir), patch.object(
                library, "CONFIG_PATH", config_path
            ):
                library.save_config(
                    {
                        "schemaVersion": library.CONFIG_SCHEMA_VERSION,
                        "downloadDir": temporary,
                        "libraryDirs": [temporary],
                    }
                )

            self.assertEqual(config_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)

    def test_migrates_existing_directories_and_adds_download_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_dir = root / "config"
            config_dir.mkdir()
            config_path = config_dir / "config.json"
            existing = root / "existing"
            legacy = root / "data" / "kiwix-desktop"
            config_path.write_text(
                '{"libraryDirs": ["%s", "%s"]}' % (existing, legacy),
                encoding="utf-8",
            )

            with patch.object(library, "CONFIG_PATH", config_path), patch.object(
                library, "DATA_HOME", root / "data"
            ), patch.object(library.Path, "home", return_value=root):
                config = library.load_config()

            self.assertEqual(config["schemaVersion"], library.CONFIG_SCHEMA_VERSION)
            self.assertEqual(config["downloadDir"], str(root / "Kiwix"))
            self.assertEqual(
                config["libraryDirs"],
                [str(existing), str(legacy), str(root / "Kiwix")],
            )

    def test_removes_library_directory_but_not_download_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            removable = root / "removable"
            download = root / "downloads"
            config = {
                "schemaVersion": library.CONFIG_SCHEMA_VERSION,
                "downloadDir": str(download),
                "libraryDirs": [str(removable), str(download)],
            }

            with patch.object(library, "load_config", return_value=config), patch.object(
                library, "save_config"
            ) as save:
                self.assertTrue(library.remove_library_dir(removable))
                self.assertFalse(library.remove_library_dir(download))

            self.assertEqual(config["libraryDirs"], [str(download)])
            save.assert_called_once_with(config)

    def test_setting_download_directory_also_adds_library_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "managed"
            config = {
                "schemaVersion": library.CONFIG_SCHEMA_VERSION,
                "downloadDir": str(root / "old"),
                "libraryDirs": [str(root / "external")],
            }

            with patch.object(library, "load_config", return_value=config), patch.object(
                library, "save_config"
            ) as save:
                self.assertTrue(library.set_download_dir(destination))

            self.assertEqual(config["downloadDir"], str(destination))
            self.assertIn(str(destination), config["libraryDirs"])
            save.assert_called_once_with(config)


class ArchiveFileTests(unittest.TestCase):
    def test_enumerates_only_exact_split_parts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary = root / "wikipedia_en.zimaa"
            second = root / "wikipedia_en.zimab"
            primary.write_bytes(b"first")
            second.write_bytes(b"second")
            (root / "wikipedia_en.zimaa.backup").write_bytes(b"backup")
            (root / "wikipedia_en_extra.zimab").write_bytes(b"other")

            self.assertEqual(library.archive_files(primary), [primary, second])
            self.assertEqual(library._split_archive_size(primary), 11)

    def test_trashes_all_split_parts_after_validating_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary = root / "wiktionary_en.zimaa"
            second = root / "wiktionary_en.zimab"
            primary.write_bytes(b"first")
            second.write_bytes(b"second")
            trash = Mock()

            result = library.trash_archive(primary, trash)

            self.assertEqual(result, [primary, second])
            self.assertEqual([call.args[0] for call in trash.call_args_list], result)

    def test_trash_rejects_missing_and_non_primary_paths(self) -> None:
        trash = Mock()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(FileNotFoundError):
                library.trash_archive(root / "missing.zim", trash)
            with self.assertRaises(ValueError):
                library.trash_archive(root / "archive.zimab", trash)
        trash.assert_not_called()

    def test_trash_validates_every_split_part_before_trashing(self) -> None:
        trash = Mock()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary = root / "archive.zimaa"
            primary.write_bytes(b"first")
            (root / "archive.zimab").mkdir()

            with self.assertRaises(OSError):
                library.trash_archive(primary, trash)

        trash.assert_not_called()


class CollectionTests(unittest.TestCase):
    def test_ignores_aria2_partial_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            zim = root / "wikipedia_en_all.zim"
            zim.write_bytes(b"partial")
            Path(str(zim) + ".aria2").write_bytes(b"control")

            files, error = library._scan_dir(root)

            self.assertEqual(files, [])
            self.assertIsNone(error)

    def test_ignores_split_archive_when_any_part_has_aria2_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            catalog_dir = data / "kiwix-desktop"
            catalog_dir.mkdir(parents=True)
            primary = root / "wikipedia_en_all.zimaa"
            second = root / "wikipedia_en_all.zimab"
            primary.write_bytes(b"first")
            second.write_bytes(b"partial")
            Path(str(second) + ".aria2").write_bytes(b"control")
            (catalog_dir / "library.xml").write_text(
                f'<library><book path="{primary}" title="Wikipedia" /></library>',
                encoding="utf-8",
            )

            with patch.object(library, "DATA_HOME", data), patch.object(
                library,
                "load_config",
                return_value={
                    "downloadDir": str(root),
                    "libraryDirs": [str(root)],
                },
            ):
                result = library.collect_library()

            self.assertEqual(result["bookCount"], 0)
            self.assertEqual(result["books"], [])

    def test_collects_files_and_uses_catalog_title(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            library_dir = root / "library"
            data.mkdir()
            library_dir.mkdir()
            zim = library_dir / "wikipedia_en_all_maxi_2025-01.zim"
            zim.write_bytes(b"zim-data")
            catalog_dir = data / "kiwix-desktop"
            catalog_dir.mkdir()
            (catalog_dir / "library.xml").write_text(
                f'<library><book path="{zim}" title="Wikipedia Offline" language="en" /></library>',
                encoding="utf-8",
            )

            with patch.object(library, "DATA_HOME", data), patch.object(
                library,
                "load_config",
                return_value={
                    "downloadDir": str(library_dir),
                    "libraryDirs": [str(library_dir)],
                },
            ):
                result = library.collect_library()

            self.assertEqual(result["bookCount"], 1)
            self.assertEqual(result["books"][0]["title"], "Wikipedia Offline")
            self.assertEqual(result["books"][0]["sizeBytes"], 8)
            self.assertEqual(result["books"][0]["origin"], "managed")

    def test_deduplicates_overlapping_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "nested"
            nested.mkdir()
            (nested / "wiktionary_en_all.zim").write_bytes(b"data")

            with patch.object(library, "DATA_HOME", root / "missing"), patch.object(
                library,
                "load_config",
                return_value={
                    "downloadDir": str(root / "managed"),
                    "libraryDirs": [str(root), str(nested)],
                },
            ):
                result = library.collect_library()

            self.assertEqual(result["bookCount"], 1)

    def test_reports_disk_stats_and_tool_availability(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            usage = Mock(total=1000, used=400, free=600)

            with patch.object(library, "DATA_HOME", root / "missing"), patch.object(
                library,
                "load_config",
                return_value={
                    "downloadDir": str(root),
                    "libraryDirs": [str(root)],
                },
            ), patch.object(library.shutil, "disk_usage", return_value=usage), patch.object(
                library.shutil,
                "which",
                side_effect=lambda command: f"/usr/bin/{command}"
                if command in {"kiwix-serve", "aria2c"}
                else None,
            ):
                result = library.collect_library()

            self.assertTrue(result["readerAvailable"])
            self.assertTrue(result["downloadAvailable"])
            self.assertNotIn("kiwixInstalled", result)
            self.assertEqual(
                result["libraryStats"],
                [
                    {
                        "path": str(root),
                        "totalBytes": 1000,
                        "usedBytes": 400,
                        "freeBytes": 600,
                    }
                ],
            )

    def test_catalog_ignores_non_primary_zim_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            catalog_dir = data / "kiwix-desktop"
            catalog_dir.mkdir(parents=True)
            continuation = root / "wikipedia_en.zimab"
            continuation.write_bytes(b"part")
            (catalog_dir / "library.xml").write_text(
                f'<library><book path="{continuation}" title="Not Primary" /></library>',
                encoding="utf-8",
            )

            with patch.object(library, "DATA_HOME", data):
                self.assertEqual(library._read_kiwix_catalog(), {})


if __name__ == "__main__":
    unittest.main()
