from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal


SCHEMA_VERSION = 2
DOWNLOAD_STATUSES = frozenset(
    {"queued", "downloading", "paused", "completed", "failed", "cancelled"}
)
DownloadStatus = Literal[
    "queued", "downloading", "paused", "completed", "failed", "cancelled"
]


def default_state_path(data_home: Path | str | None = None) -> Path:
    if data_home is None:
        configured = os.environ.get("XDG_DATA_HOME")
        data_home = Path(configured) if configured else Path.home() / ".local/share"
    return Path(data_home).expanduser() / "omawix/state.sqlite3"


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    id: str
    title: str
    summary: str = ""
    languages: tuple[str, ...] = ()
    name: str = ""
    flavour: str = ""
    category: str = ""
    tags: tuple[str, ...] = ()
    articleCount: int = 0
    mediaCount: int = 0
    updated: str = ""
    issued: str = ""
    thumbnailUrl: str = ""
    previewUrl: str = ""
    downloadUrl: str = ""
    sizeBytes: int = 0


@dataclass(frozen=True, slots=True)
class CachedCatalogPage:
    entries: tuple[CatalogEntry, ...]
    total_results: int
    start_index: int
    items_per_page: int
    cached_at: int


@dataclass(frozen=True, slots=True)
class DownloadRecord:
    entry_id: str
    download_url: str
    local_path: str
    status: DownloadStatus
    bytes_downloaded: int
    total_bytes: int | None
    error: str
    updated_at: int


class StateStore:
    """Small, thread-friendly SQLite store for catalog and download state."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        data_home: Path | str | None = None,
    ) -> None:
        if path is not None and data_home is not None:
            raise ValueError("pass either path or data_home, not both")
        self.path = Path(path).expanduser() if path is not None else default_state_path(data_home)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        self._migrate()
        self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _migrate(self) -> None:
        with closing(self._connect()) as connection, connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"state database version {version} is newer than supported version "
                    f"{SCHEMA_VERSION}"
                )
            if version < 1:
                connection.executescript(
                    """
                    CREATE TABLE catalog_entries (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        languages TEXT NOT NULL,
                        name TEXT NOT NULL,
                        flavour TEXT NOT NULL,
                        category TEXT NOT NULL,
                        tags TEXT NOT NULL,
                        article_count INTEGER NOT NULL,
                        media_count INTEGER NOT NULL,
                        updated TEXT NOT NULL,
                        issued TEXT NOT NULL,
                        thumbnail_url TEXT NOT NULL,
                        preview_url TEXT NOT NULL,
                        download_url TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        cached_at INTEGER NOT NULL
                    );

                    CREATE TABLE catalog_queries (
                        query_key TEXT PRIMARY KEY,
                        total_results INTEGER NOT NULL,
                        start_index INTEGER NOT NULL,
                        items_per_page INTEGER NOT NULL,
                        cached_at INTEGER NOT NULL
                    );

                    CREATE TABLE catalog_query_entries (
                        query_key TEXT NOT NULL REFERENCES catalog_queries(query_key)
                            ON DELETE CASCADE,
                        position INTEGER NOT NULL,
                        entry_id TEXT NOT NULL REFERENCES catalog_entries(id)
                            ON DELETE CASCADE,
                        PRIMARY KEY (query_key, position)
                    );

                    CREATE TABLE downloads (
                        entry_id TEXT PRIMARY KEY,
                        download_url TEXT NOT NULL,
                        local_path TEXT NOT NULL,
                        status TEXT NOT NULL,
                        bytes_downloaded INTEGER NOT NULL,
                        total_bytes INTEGER,
                        error TEXT NOT NULL,
                        updated_at INTEGER NOT NULL
                    );

                    PRAGMA user_version = 1;
                    """
                )
                version = 1
            if version < 2:
                connection.executescript(
                    """
                    CREATE TABLE transfer_states (
                        catalog_id TEXT PRIMARY KEY,
                        payload TEXT NOT NULL,
                        updated_at INTEGER NOT NULL
                    );

                    PRAGMA user_version = 2;
                    """
                )

    @staticmethod
    def _entry_values(entry: CatalogEntry, cached_at: int) -> tuple[object, ...]:
        return (
            entry.id,
            entry.title,
            entry.summary,
            json.dumps(entry.languages, ensure_ascii=False),
            entry.name,
            entry.flavour,
            entry.category,
            json.dumps(entry.tags, ensure_ascii=False),
            entry.articleCount,
            entry.mediaCount,
            entry.updated,
            entry.issued,
            entry.thumbnailUrl,
            entry.previewUrl,
            entry.downloadUrl,
            entry.sizeBytes,
            cached_at,
        )

    @staticmethod
    def _entry_from_row(row: sqlite3.Row) -> CatalogEntry:
        return CatalogEntry(
            id=row["id"],
            title=row["title"],
            summary=row["summary"],
            languages=tuple(json.loads(row["languages"])),
            name=row["name"],
            flavour=row["flavour"],
            category=row["category"],
            tags=tuple(json.loads(row["tags"])),
            articleCount=row["article_count"],
            mediaCount=row["media_count"],
            updated=row["updated"],
            issued=row["issued"],
            thumbnailUrl=row["thumbnail_url"],
            previewUrl=row["preview_url"],
            downloadUrl=row["download_url"],
            sizeBytes=row["size_bytes"],
        )

    def _upsert_entries(
        self,
        connection: sqlite3.Connection,
        entries: Iterable[CatalogEntry],
        cached_at: int,
    ) -> None:
        connection.executemany(
            """
            INSERT INTO catalog_entries (
                id, title, summary, languages, name, flavour, category, tags,
                article_count, media_count, updated, issued, thumbnail_url,
                preview_url, download_url, size_bytes, cached_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                summary = excluded.summary,
                languages = excluded.languages,
                name = excluded.name,
                flavour = excluded.flavour,
                category = excluded.category,
                tags = excluded.tags,
                article_count = excluded.article_count,
                media_count = excluded.media_count,
                updated = excluded.updated,
                issued = excluded.issued,
                thumbnail_url = excluded.thumbnail_url,
                preview_url = excluded.preview_url,
                download_url = excluded.download_url,
                size_bytes = excluded.size_bytes,
                cached_at = excluded.cached_at
            """,
            (self._entry_values(entry, cached_at) for entry in entries),
        )

    def cache_catalog_entries(self, entries: Iterable[CatalogEntry]) -> None:
        entries = tuple(entries)
        with closing(self._connect()) as connection, connection:
            self._upsert_entries(connection, entries, int(time.time()))

    def cache_catalog_page(
        self,
        query_key: str,
        entries: Iterable[CatalogEntry],
        *,
        total_results: int,
        start_index: int,
        items_per_page: int,
    ) -> None:
        entries = tuple(entries)
        cached_at = int(time.time())
        with closing(self._connect()) as connection, connection:
            self._upsert_entries(connection, entries, cached_at)
            connection.execute(
                """
                INSERT INTO catalog_queries (
                    query_key, total_results, start_index, items_per_page, cached_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(query_key) DO UPDATE SET
                    total_results = excluded.total_results,
                    start_index = excluded.start_index,
                    items_per_page = excluded.items_per_page,
                    cached_at = excluded.cached_at
                """,
                (query_key, total_results, start_index, items_per_page, cached_at),
            )
            connection.execute(
                "DELETE FROM catalog_query_entries WHERE query_key = ?", (query_key,)
            )
            connection.executemany(
                """
                INSERT INTO catalog_query_entries (query_key, position, entry_id)
                VALUES (?, ?, ?)
                """,
                ((query_key, position, entry.id) for position, entry in enumerate(entries)),
            )

    def get_cached_catalog_page(self, query_key: str) -> CachedCatalogPage | None:
        with closing(self._connect()) as connection, connection:
            metadata = connection.execute(
                "SELECT * FROM catalog_queries WHERE query_key = ?", (query_key,)
            ).fetchone()
            if metadata is None:
                return None
            rows = connection.execute(
                """
                SELECT entry.*
                FROM catalog_query_entries AS page
                JOIN catalog_entries AS entry ON entry.id = page.entry_id
                WHERE page.query_key = ?
                ORDER BY page.position
                """,
                (query_key,),
            ).fetchall()
        return CachedCatalogPage(
            entries=tuple(self._entry_from_row(row) for row in rows),
            total_results=metadata["total_results"],
            start_index=metadata["start_index"],
            items_per_page=metadata["items_per_page"],
            cached_at=metadata["cached_at"],
        )

    def query_catalog_entries(
        self,
        *,
        q: str | None = None,
        lang: str | None = None,
        category: str | None = None,
        maxsize: int | None = None,
        start: int = 0,
        count: int = 50,
    ) -> tuple[CatalogEntry, ...]:
        if start < 0 or count < 1:
            raise ValueError("start must be non-negative and count must be positive")
        if maxsize is not None and maxsize < 0:
            raise ValueError("maxsize must be non-negative")
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT * FROM catalog_entries ORDER BY updated DESC, title COLLATE NOCASE, id"
            ).fetchall()

        entries = [self._entry_from_row(row) for row in rows]
        if q:
            needle = q.casefold()
            entries = [
                entry
                for entry in entries
                if needle
                in " ".join(
                    (entry.title, entry.summary, entry.name, " ".join(entry.tags))
                ).casefold()
            ]
        if lang:
            language = lang.casefold()
            entries = [
                entry
                for entry in entries
                if language in {value.casefold() for value in entry.languages}
            ]
        if category:
            wanted_category = category.casefold()
            entries = [
                entry for entry in entries if entry.category.casefold() == wanted_category
            ]
        if maxsize is not None:
            entries = [entry for entry in entries if entry.sizeBytes <= maxsize]
        return tuple(entries[start : start + count])

    @staticmethod
    def _validate_download(
        status: str, bytes_downloaded: int, total_bytes: int | None
    ) -> None:
        if status not in DOWNLOAD_STATUSES:
            raise ValueError(f"unsupported download status: {status}")
        if bytes_downloaded < 0 or (total_bytes is not None and total_bytes < 0):
            raise ValueError("download byte counts must be non-negative")

    @staticmethod
    def _download_from_row(row: sqlite3.Row) -> DownloadRecord:
        return DownloadRecord(
            entry_id=row["entry_id"],
            download_url=row["download_url"],
            local_path=row["local_path"],
            status=row["status"],
            bytes_downloaded=row["bytes_downloaded"],
            total_bytes=row["total_bytes"],
            error=row["error"],
            updated_at=row["updated_at"],
        )

    def record_download(
        self,
        entry_id: str,
        download_url: str,
        local_path: Path | str,
        *,
        status: DownloadStatus = "queued",
        bytes_downloaded: int = 0,
        total_bytes: int | None = None,
        error: str = "",
    ) -> DownloadRecord:
        self._validate_download(status, bytes_downloaded, total_bytes)
        updated_at = int(time.time())
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO downloads (
                    entry_id, download_url, local_path, status, bytes_downloaded,
                    total_bytes, error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entry_id) DO UPDATE SET
                    download_url = excluded.download_url,
                    local_path = excluded.local_path,
                    status = excluded.status,
                    bytes_downloaded = excluded.bytes_downloaded,
                    total_bytes = excluded.total_bytes,
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                (
                    entry_id,
                    download_url,
                    str(local_path),
                    status,
                    bytes_downloaded,
                    total_bytes,
                    error,
                    updated_at,
                ),
            )
        record = self.get_download(entry_id)
        if record is None:  # pragma: no cover - the insert and read are one local DB
            raise RuntimeError("download record was not saved")
        return record

    def update_download_status(
        self,
        entry_id: str,
        status: DownloadStatus,
        *,
        bytes_downloaded: int | None = None,
        total_bytes: int | None = None,
        error: str | None = None,
    ) -> DownloadRecord:
        current = self.get_download(entry_id)
        if current is None:
            raise KeyError(entry_id)
        return self.record_download(
            current.entry_id,
            current.download_url,
            current.local_path,
            status=status,
            bytes_downloaded=(
                current.bytes_downloaded if bytes_downloaded is None else bytes_downloaded
            ),
            total_bytes=current.total_bytes if total_bytes is None else total_bytes,
            error=current.error if error is None else error,
        )

    def get_download(self, entry_id: str) -> DownloadRecord | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM downloads WHERE entry_id = ?", (entry_id,)
            ).fetchone()
        return None if row is None else self._download_from_row(row)

    def list_downloads(
        self, status: DownloadStatus | None = None
    ) -> tuple[DownloadRecord, ...]:
        if status is not None and status not in DOWNLOAD_STATUSES:
            raise ValueError(f"unsupported download status: {status}")
        sql = "SELECT * FROM downloads"
        parameters: tuple[str, ...] = ()
        if status is not None:
            sql += " WHERE status = ?"
            parameters = (status,)
        sql += " ORDER BY updated_at DESC, entry_id"
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(sql, parameters).fetchall()
        return tuple(self._download_from_row(row) for row in rows)

    def remove_download(self, entry_id: str) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM downloads WHERE entry_id = ?", (entry_id,)
            )
        return cursor.rowcount > 0

    def save_download(self, catalog_id: str, state: dict[str, object]) -> None:
        """Persist the opaque aria2 state needed to reconnect after restart."""
        if not catalog_id.strip():
            raise ValueError("catalog_id is required")
        payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO transfer_states (catalog_id, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(catalog_id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (catalog_id, payload, int(time.time())),
            )

    def load_download(self, catalog_id: str) -> dict[str, object] | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT payload FROM transfer_states WHERE catalog_id = ?",
                (catalog_id,),
            ).fetchone()
        if row is None:
            return None
        value = json.loads(row["payload"])
        return value if isinstance(value, dict) else None

    def list_download_states(self) -> tuple[dict[str, object], ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT payload FROM transfer_states ORDER BY updated_at DESC, catalog_id"
            ).fetchall()
        states = []
        for row in rows:
            value = json.loads(row["payload"])
            if isinstance(value, dict):
                states.append(value)
        return tuple(states)

    def delete_download_state(self, catalog_id: str) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM transfer_states WHERE catalog_id = ?", (catalog_id,)
            )
        return cursor.rowcount > 0
