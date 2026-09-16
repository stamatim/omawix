from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from omawix_state import CatalogEntry, StateStore, default_state_path


class StateStoreTests(unittest.TestCase):
    def test_repairs_private_state_directory_and_database_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "omawix"
            state_dir.mkdir(mode=0o755)
            path = state_dir / "state.sqlite3"
            path.touch(mode=0o644)

            StateStore(path)

            self.assertEqual(state_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_uses_xdg_data_home_and_migrates_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"XDG_DATA_HOME": temporary}
        ):
            expected = Path(temporary) / "omawix/state.sqlite3"
            store = StateStore()

            self.assertEqual(default_state_path(), expected)
            self.assertEqual(store.path, expected)
            with closing(sqlite3.connect(expected)) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertEqual(version, 2)
            self.assertIn("catalog_entries", tables)
            self.assertIn("downloads", tables)
            self.assertIn("transfer_states", tables)

    def test_caches_an_exact_catalog_page(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "state.sqlite3")
            entry = CatalogEntry(
                id="urn:uuid:book",
                title="Wikipedia",
                languages=("eng",),
                category="wikipedia",
                tags=("_pictures:yes",),
                sizeBytes=100,
            )

            store.cache_catalog_page(
                "query", [entry], total_results=12, start_index=5, items_per_page=1
            )
            cached = store.get_cached_catalog_page("query")

            self.assertIsNotNone(cached)
            assert cached is not None
            self.assertEqual(cached.entries, (entry,))
            self.assertEqual(cached.total_results, 12)
            self.assertEqual(cached.start_index, 5)

    def test_filters_cached_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "state.sqlite3")
            store.cache_catalog_entries(
                [
                    CatalogEntry(
                        id="one",
                        title="Wikipedia English",
                        languages=("eng",),
                        category="wikipedia",
                        sizeBytes=100,
                    ),
                    CatalogEntry(
                        id="two",
                        title="Wiktionary French",
                        languages=("fra",),
                        category="wiktionary",
                        sizeBytes=50,
                    ),
                ]
            )

            entries = store.query_catalog_entries(
                q="pedia", lang="eng", category="wikipedia", maxsize=100
            )

            self.assertEqual([entry.id for entry in entries], ["one"])

    def test_persists_download_mapping_and_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            store = StateStore(path)
            store.record_download(
                "book",
                "https://download.example/book.zim",
                Path(temporary) / "book.zim",
                total_bytes=200,
            )
            updated = store.update_download_status(
                "book", "downloading", bytes_downloaded=75
            )

            reopened = StateStore(path).get_download("book")
            self.assertEqual(updated.bytes_downloaded, 75)
            self.assertEqual(reopened, updated)
            self.assertEqual(store.list_downloads("downloading"), (updated,))

    def test_persists_opaque_transfer_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "state.sqlite3")
            payload = {
                "catalog_id": "book",
                "gids": ["abc"],
                "destination": temporary,
                "status": "paused",
            }

            store.save_download("book", payload)

            self.assertEqual(store.load_download("book"), payload)
            self.assertEqual(store.list_download_states(), (payload,))
            self.assertTrue(store.delete_download_state("book"))
            self.assertIsNone(store.load_download("book"))


if __name__ == "__main__":
    unittest.main()
