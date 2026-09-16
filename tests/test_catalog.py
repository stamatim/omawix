from __future__ import annotations

import io
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from omawix_catalog import (
    MAX_CATALOG_RESPONSE_BYTES,
    CatalogClient,
    CatalogError,
    parse_navigation_feed,
    parse_opds_feed,
)
from omawix_state import StateStore


ENTRY_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:dc="http://purl.org/dc/terms/"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <opensearch:totalResults>7</opensearch:totalResults>
  <opensearch:startIndex>2</opensearch:startIndex>
  <opensearch:itemsPerPage>1</opensearch:itemsPerPage>
  <entry>
    <id>urn:uuid:abc</id>
    <title>Wikipedia &amp; Friends</title>
    <summary>Free knowledge</summary>
    <language>eng,fra</language>
    <name>wikipedia_en_all</name>
    <flavour>maxi</flavour>
    <category>wikipedia</category>
    <tags>wikipedia;_pictures:yes</tags>
    <articleCount>123</articleCount>
    <mediaCount>45</mediaCount>
    <updated>2025-01-02T00:00:00Z</updated>
    <dc:issued>2025-01-01T00:00:00Z</dc:issued>
    <link rel="http://opds-spec.org/image/thumbnail" href="/thumb/abc" />
    <link type="text/html" href="/content/abc" />
    <link rel="http://opds-spec.org/acquisition/open-access"
          href="//download.example/abc.zim.meta4" length="987654" />
  </entry>
</feed>
"""

NAVIGATION_FEED = b"""<feed xmlns="http://www.w3.org/2005/Atom"
    xmlns:dc="http://purl.org/dc/terms/"
    xmlns:thr="http://purl.org/syndication/thread/1.0">
  <entry>
    <id>english</id><title>English</title><dc:language>eng</dc:language>
    <thr:count>42</thr:count>
    <link rel="subsection" href="/catalog/v2/entries?lang=eng" />
  </entry>
</feed>"""


class FakeResponse(io.BytesIO):
    def __init__(self, payload: bytes, url: str) -> None:
        super().__init__(payload)
        self._url = url

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class CatalogParsingTests(unittest.TestCase):
    def test_parses_namespaced_feed_and_resolves_links(self) -> None:
        page = parse_opds_feed(
            ENTRY_FEED, "https://library.kiwix.org/catalog/v2/entries?count=1"
        )

        self.assertEqual(page.total_results, 7)
        self.assertEqual(page.start_index, 2)
        entry = page.entries[0]
        self.assertEqual(entry.title, "Wikipedia & Friends")
        self.assertEqual(entry.languages, ("eng", "fra"))
        self.assertEqual(entry.articleCount, 123)
        self.assertEqual(entry.issued, "2025-01-01T00:00:00Z")
        self.assertEqual(entry.thumbnailUrl, "https://library.kiwix.org/thumb/abc")
        self.assertEqual(entry.previewUrl, "https://library.kiwix.org/content/abc")
        self.assertEqual(entry.downloadUrl, "https://download.example/abc.zim.meta4")
        self.assertEqual(entry.sizeBytes, 987654)

    def test_parses_language_navigation(self) -> None:
        items = parse_navigation_feed(
            NAVIGATION_FEED,
            "https://library.kiwix.org/catalog/v2/languages",
            kind="language",
        )

        self.assertEqual(items[0].code, "eng")
        self.assertEqual(items[0].count, 42)
        self.assertEqual(
            items[0].url, "https://library.kiwix.org/catalog/v2/entries?lang=eng"
        )


class CatalogClientTests(unittest.TestCase):
    def test_rejects_a_catalog_redirect_to_http(self) -> None:
        def opener(_request: object, *, timeout: float) -> FakeResponse:
            return FakeResponse(ENTRY_FEED, "http://library.kiwix.org/catalog/v2/entries")

        with tempfile.TemporaryDirectory() as temporary:
            client = CatalogClient(
                StateStore(Path(temporary) / "state.sqlite3"), opener=opener
            )

            with self.assertRaisesRegex(CatalogError, "HTTPS"):
                client.fetch_entries()

    def test_does_not_persist_free_text_search_queries(self) -> None:
        def opener(request: object, *, timeout: float) -> FakeResponse:
            return FakeResponse(ENTRY_FEED, request.full_url)  # type: ignore[attr-defined]

        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "state.sqlite3")
            client = CatalogClient(store, opener=opener)
            client.fetch_entries(q="private search", count=1)
            search_url = (
                "https://library.kiwix.org/catalog/v2/entries?"
                "q=private+search&start=0&count=1"
            )

            self.assertIsNone(store.get_cached_catalog_page(search_url))

    def test_rejects_an_oversized_catalog_response(self) -> None:
        payload = b"x" * (MAX_CATALOG_RESPONSE_BYTES + 1)

        def opener(request: object, *, timeout: float) -> FakeResponse:
            return FakeResponse(payload, request.full_url)  # type: ignore[attr-defined]

        with tempfile.TemporaryDirectory() as temporary:
            client = CatalogClient(
                StateStore(Path(temporary) / "state.sqlite3"), opener=opener
            )

            with self.assertRaisesRegex(CatalogError, "exceeds"):
                client.fetch_entries()

    def test_sends_all_server_side_filters(self) -> None:
        requested = []

        def opener(request: object, *, timeout: float) -> FakeResponse:
            requested.append((request.full_url, timeout))  # type: ignore[attr-defined]
            return FakeResponse(ENTRY_FEED, request.full_url)  # type: ignore[attr-defined]

        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "state.sqlite3")
            client = CatalogClient(store, opener=opener, timeout=3)
            page = client.fetch_entries(
                q="free knowledge",
                lang="eng",
                category="wikipedia",
                maxsize=1000000,
                start=2,
                count=1,
            )

            parsed = urllib.parse.urlsplit(requested[0][0])
            parameters = urllib.parse.parse_qs(parsed.query)
            self.assertEqual(
                parameters,
                {
                    "q": ["free knowledge"],
                    "lang": ["eng"],
                    "category": ["wikipedia"],
                    "maxsize": ["1000000"],
                    "start": ["2"],
                    "count": ["1"],
                },
            )
            self.assertFalse(page.from_cache)
            self.assertEqual(requested[0][1], 3)

    def test_returns_matching_cached_page_when_network_fails(self) -> None:
        calls = 0

        def opener(request: object, *, timeout: float) -> FakeResponse:
            nonlocal calls
            calls += 1
            if calls == 1:
                return FakeResponse(ENTRY_FEED, request.full_url)  # type: ignore[attr-defined]
            raise urllib.error.URLError("offline")

        with tempfile.TemporaryDirectory() as temporary:
            client = CatalogClient(
                StateStore(Path(temporary) / "state.sqlite3"), opener=opener
            )
            fresh = client.fetch_entries(lang="eng", start=2, count=1)
            cached = client.fetch_entries(lang="eng", start=2, count=1)

            self.assertEqual(cached.entries, fresh.entries)
            self.assertTrue(cached.from_cache)
            self.assertIn("offline", cached.error or "")


if __name__ == "__main__":
    unittest.main()
