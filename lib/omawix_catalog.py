from __future__ import annotations

import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Callable

from omawix_state import CatalogEntry, StateStore


CATALOG_BASE_URL = "https://library.kiwix.org/catalog/v2"
MAX_CATALOG_RESPONSE_BYTES = 20 * 1024 * 1024
_THUMBNAIL_REL = "http://opds-spec.org/image/thumbnail"
_IMAGE_REL = "http://opds-spec.org/image"
_ACQUISITION_REL = "http://opds-spec.org/acquisition/open-access"


class CatalogError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CatalogQuery:
    q: str | None = None
    lang: str | None = None
    category: str | None = None
    maxsize: int | None = None
    start: int = 0
    count: int = 50

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError("start must be non-negative")
        if self.count < 1:
            raise ValueError("count must be positive")
        if self.maxsize is not None and self.maxsize < 0:
            raise ValueError("maxsize must be non-negative")

    def parameters(self) -> dict[str, str | int]:
        parameters: dict[str, str | int] = {}
        if self.q:
            parameters["q"] = self.q
        if self.lang:
            parameters["lang"] = self.lang
        if self.category:
            parameters["category"] = self.category
        if self.maxsize is not None:
            parameters["maxsize"] = self.maxsize
        parameters["start"] = self.start
        parameters["count"] = self.count
        return parameters


@dataclass(frozen=True, slots=True)
class CatalogPage:
    entries: tuple[CatalogEntry, ...]
    total_results: int
    start_index: int
    items_per_page: int
    from_cache: bool = False
    error: str | None = None


@dataclass(frozen=True, slots=True)
class NavigationItem:
    id: str
    title: str
    code: str
    count: int | None
    url: str


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local_name(child.tag) == name]


def _text(element: ET.Element, name: str) -> str:
    children = _children(element, name)
    if not children:
        return ""
    return " ".join("".join(children[0].itertext()).split())


def _integer(value: str | None, default: int = 0) -> int:
    try:
        return max(0, int(value or ""))
    except ValueError:
        return default


def _resolve_url(document_url: str, href: str) -> str:
    if not href:
        return ""
    resolved = urllib.parse.urljoin(document_url, href)
    parsed = urllib.parse.urlsplit(resolved)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return resolved


def _split_values(value: str, pattern: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in re.split(pattern, value) if part.strip())


def _parse_entry(element: ET.Element, document_url: str) -> CatalogEntry | None:
    entry_id = _text(element, "id")
    title = _text(element, "title")
    if not entry_id or not title:
        return None

    thumbnail_url = ""
    preview_url = ""
    download_url = ""
    size_bytes = 0
    for link in _children(element, "link"):
        rel = link.attrib.get("rel", "")
        media_type = link.attrib.get("type", "")
        url = _resolve_url(document_url, link.attrib.get("href", ""))
        if not url:
            continue
        if rel == _THUMBNAIL_REL or rel.endswith("/image/thumbnail"):
            thumbnail_url = url
        elif rel == _IMAGE_REL or rel.endswith("/image"):
            preview_url = url
        elif rel == _ACQUISITION_REL or "acquisition" in rel:
            download_url = url
            size_bytes = _integer(link.attrib.get("length"))
        elif media_type.split(";", 1)[0].strip().lower() == "text/html":
            preview_url = url

    languages = []
    for language in _children(element, "language"):
        languages.extend(_split_values("".join(language.itertext()), r"[,;\s]+"))

    return CatalogEntry(
        id=entry_id,
        title=title,
        summary=_text(element, "summary"),
        languages=tuple(dict.fromkeys(languages)),
        name=_text(element, "name"),
        flavour=_text(element, "flavour"),
        category=_text(element, "category"),
        tags=_split_values(_text(element, "tags"), r";"),
        articleCount=_integer(_text(element, "articleCount")),
        mediaCount=_integer(_text(element, "mediaCount")),
        updated=_text(element, "updated"),
        issued=_text(element, "issued"),
        thumbnailUrl=thumbnail_url,
        previewUrl=preview_url,
        downloadUrl=download_url,
        sizeBytes=size_bytes,
    )


def parse_opds_feed(data: bytes | str, document_url: str = CATALOG_BASE_URL) -> CatalogPage:
    root = ET.fromstring(data)
    entries = tuple(
        parsed
        for element in _children(root, "entry")
        if (parsed := _parse_entry(element, document_url)) is not None
    )
    total_text = _text(root, "totalResults")
    total_results = _integer(total_text) if total_text else -1
    start_index = _integer(_text(root, "startIndex"))
    items_per_page = _integer(_text(root, "itemsPerPage"), len(entries))
    return CatalogPage(entries, total_results, start_index, items_per_page)


def parse_navigation_feed(
    data: bytes | str,
    document_url: str = CATALOG_BASE_URL,
    *,
    kind: str,
) -> tuple[NavigationItem, ...]:
    if kind not in {"language", "category"}:
        raise ValueError("kind must be 'language' or 'category'")
    root = ET.fromstring(data)
    items = []
    for element in _children(root, "entry"):
        entry_id = _text(element, "id")
        title = _text(element, "title")
        links = _children(element, "link")
        link = next(
            (candidate for candidate in links if candidate.attrib.get("rel") == "subsection"),
            links[0] if links else None,
        )
        url = "" if link is None else _resolve_url(document_url, link.attrib.get("href", ""))
        if kind == "language":
            code = _text(element, "language")
        else:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            code = query.get("category", [title])[0]
        count_text = _text(element, "count")
        if entry_id and title and code:
            items.append(
                NavigationItem(
                    id=entry_id,
                    title=title,
                    code=code,
                    count=_integer(count_text) if count_text else None,
                    url=url,
                )
            )
    return tuple(items)


class CatalogClient:
    def __init__(
        self,
        state: StateStore | None = None,
        *,
        base_url: str = CATALOG_BASE_URL,
        timeout: float = 15.0,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.state = state if state is not None else StateStore()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen

    def _request(self, url: str) -> tuple[bytes, str]:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/atom+xml;profile=opds-catalog",
                "User-Agent": "Omawix/1",
            },
        )
        with self._opener(request, timeout=self.timeout) as response:
            final_url = response.geturl() if hasattr(response, "geturl") else url
            final = urllib.parse.urlsplit(final_url)
            if final.scheme.lower() != "https" or not final.netloc:
                raise CatalogError("catalog redirect must remain on HTTPS")
            data = response.read(MAX_CATALOG_RESPONSE_BYTES + 1)
            if len(data) > MAX_CATALOG_RESPONSE_BYTES:
                raise CatalogError(
                    f"catalog response exceeds {MAX_CATALOG_RESPONSE_BYTES} bytes"
                )
            return data, final_url

    def fetch_entries(
        self,
        *,
        q: str | None = None,
        lang: str | None = None,
        category: str | None = None,
        maxsize: int | None = None,
        start: int = 0,
        count: int = 50,
    ) -> CatalogPage:
        query = CatalogQuery(q, lang, category, maxsize, start, count)
        url = f"{self.base_url}/entries?{urllib.parse.urlencode(query.parameters())}"
        try:
            data, document_url = self._request(url)
            page = parse_opds_feed(data, document_url)
        except (OSError, ET.ParseError, ValueError) as error:
            cached = None if q else self.state.get_cached_catalog_page(url)
            if cached is None:
                raise CatalogError(f"could not fetch Kiwix catalog: {error}") from error
            return CatalogPage(
                entries=cached.entries,
                total_results=cached.total_results,
                start_index=cached.start_index,
                items_per_page=cached.items_per_page,
                from_cache=True,
                error=str(error),
            )

        if not q:
            self.state.cache_catalog_page(
                url,
                page.entries,
                total_results=page.total_results,
                start_index=page.start_index,
                items_per_page=page.items_per_page,
            )
        return page

    def _fetch_navigation(self, endpoint: str, kind: str) -> tuple[NavigationItem, ...]:
        url = f"{self.base_url}/{endpoint}"
        try:
            data, document_url = self._request(url)
            return parse_navigation_feed(data, document_url, kind=kind)
        except (OSError, ET.ParseError, ValueError) as error:
            raise CatalogError(f"could not fetch Kiwix {kind} list: {error}") from error

    def fetch_languages(self) -> tuple[NavigationItem, ...]:
        return self._fetch_navigation("languages", "language")

    def fetch_categories(self) -> tuple[NavigationItem, ...]:
        return self._fetch_navigation("categories", "category")
