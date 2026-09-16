from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable


APP_ID = "io.github.stamatim.omawix"
CONFIG_SCHEMA_VERSION = 1
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "omawix"
CONFIG_PATH = CONFIG_DIR / "config.json"
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))

_EDITION_WORDS = {
    "all": "Full",
    "maxi": "Maxi",
    "mini": "Mini",
    "nopic": "No pictures",
    "novid": "No video",
    "top": "Top articles",
}

_LANGUAGE_NAMES = {
    "ar": "Arabic",
    "ara": "Arabic",
    "de": "German",
    "deu": "German",
    "en": "English",
    "eng": "English",
    "es": "Spanish",
    "spa": "Spanish",
    "fa": "Persian",
    "fr": "French",
    "fra": "French",
    "hi": "Hindi",
    "id": "Indonesian",
    "it": "Italian",
    "ita": "Italian",
    "ja": "Japanese",
    "jpn": "Japanese",
    "ko": "Korean",
    "nl": "Dutch",
    "pl": "Polish",
    "pt": "Portuguese",
    "por": "Portuguese",
    "ru": "Russian",
    "rus": "Russian",
    "sv": "Swedish",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "vi": "Vietnamese",
    "zh": "Chinese",
    "zho": "Chinese",
}


def default_download_dir() -> Path:
    return Path.home() / "Kiwix"


def default_library_dirs() -> list[Path]:
    candidates = [
        DATA_HOME / "kiwix-desktop",
        default_download_dir(),
        Path.home() / "Documents/Kiwix",
        Path.home() / "Downloads",
    ]
    return list(dict.fromkeys(path.expanduser() for path in candidates))


def load_config() -> dict[str, Any]:
    download_dir = str(default_download_dir().expanduser())
    defaults = {
        "schemaVersion": CONFIG_SCHEMA_VERSION,
        "downloadDir": download_dir,
        "libraryDirs": [str(path) for path in default_library_dirs()],
    }
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return defaults

    if not isinstance(raw, dict):
        return defaults

    configured_download_dir = raw.get("downloadDir")
    if isinstance(configured_download_dir, str) and configured_download_dir.strip():
        download_dir = str(Path(configured_download_dir).expanduser())

    dirs = raw.get("libraryDirs")
    if not isinstance(dirs, list):
        dirs = defaults["libraryDirs"]

    clean: list[str] = []
    for value in dirs:
        if isinstance(value, str) and value.strip():
            clean.append(str(Path(value).expanduser()))
    clean.append(download_dir)
    return {
        "schemaVersion": CONFIG_SCHEMA_VERSION,
        "downloadDir": download_dir,
        "libraryDirs": list(dict.fromkeys(clean)),
    }


def save_config(config: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    CONFIG_DIR.chmod(0o700)
    payload = json.dumps(config, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=CONFIG_DIR, delete=False
    ) as handle:
        handle.write(payload)
        temporary_path = Path(handle.name)
    temporary_path.replace(CONFIG_PATH)
    CONFIG_PATH.chmod(0o600)


def add_library_dir(path: Path) -> bool:
    resolved = str(path.expanduser().resolve())
    config = load_config()
    dirs = config["libraryDirs"]
    if resolved in dirs:
        return False
    dirs.append(resolved)
    save_config(config)
    return True


def remove_library_dir(path: Path) -> bool:
    resolved = str(path.expanduser().resolve())
    config = load_config()
    if resolved == str(Path(config["downloadDir"]).expanduser().resolve()):
        return False

    dirs = config["libraryDirs"]
    retained = [
        value
        for value in dirs
        if str(Path(value).expanduser().resolve()) != resolved
    ]
    if len(retained) == len(dirs):
        return False
    config["libraryDirs"] = retained
    save_config(config)
    return True


def set_download_dir(path: Path) -> bool:
    resolved = str(path.expanduser().resolve())
    config = load_config()
    changed = config["downloadDir"] != resolved
    config["downloadDir"] = resolved
    if resolved not in config["libraryDirs"]:
        config["libraryDirs"].append(resolved)
        changed = True
    if changed:
        save_config(config)
    return changed


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _library_xml_paths() -> list[Path]:
    return [
        DATA_HOME / "kiwix-desktop/library.xml",
        DATA_HOME / "library.xml",
    ]


def _read_kiwix_catalog() -> dict[Path, dict[str, str]]:
    records: dict[Path, dict[str, str]] = {}
    for library_path in _library_xml_paths():
        if not library_path.is_file():
            continue
        try:
            root = ET.parse(library_path).getroot()
        except (OSError, ET.ParseError):
            continue
        for node in root.iter():
            if _local_name(node.tag) != "book":
                continue
            attrs = {_local_name(key): value for key, value in node.attrib.items()}
            raw_path = attrs.get("path", "")
            if not raw_path:
                continue
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                path = library_path.parent / path
            if not _is_primary_zim(path.name):
                continue
            try:
                path = path.resolve()
            except OSError:
                continue
            if (
                _is_primary_zim(path.name)
                and path.is_file()
                and not _has_aria2_sidecar(path)
            ):
                records[path] = attrs
    return records


def _is_primary_zim(filename: str) -> bool:
    lower = filename.lower()
    return lower.endswith(".zim") or lower.endswith(".zimaa")


def _has_aria2_sidecar(path: Path) -> bool:
    if path.name.lower().endswith(".zim"):
        return Path(str(path) + ".aria2").exists()

    split_prefix = path.name[:-2].casefold()
    try:
        return any(
            candidate.name.casefold().startswith(split_prefix)
            and re.fullmatch(
                rf"{re.escape(split_prefix)}[a-z]{{2}}\.aria2",
                candidate.name.casefold(),
            )
            for candidate in path.parent.iterdir()
        )
    except OSError:
        return Path(str(path) + ".aria2").exists()


def _scan_dir(directory: Path) -> tuple[list[Path], str | None]:
    if not directory.is_dir():
        return [], None
    files: list[Path] = []
    errors = []

    def record_error(error: OSError) -> None:
        errors.append(f"{error.filename or directory}: {error.strerror or error}")

    for root, dirnames, filenames in os.walk(
        directory, followlinks=False, onerror=record_error
    ):
        dirnames[:] = [
            name
            for name in dirnames
            if not name.startswith(".") and name not in {"node_modules", "__pycache__"}
        ]
        for filename in filenames:
            if _is_primary_zim(filename):
                try:
                    path = (Path(root) / filename).resolve()
                    if _has_aria2_sidecar(path):
                        continue
                    if _is_primary_zim(path.name):
                        files.append(path)
                except OSError as error:
                    record_error(error)
    return files, errors[0] if errors else None


def _require_regular_file(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise FileNotFoundError(f"Archive file does not exist: {path}") from None
    if not stat.S_ISREG(mode):
        raise OSError(f"Archive path is not a regular file: {path}")


def archive_files(path: Path | str) -> list[Path]:
    primary = Path(path).expanduser()
    if not _is_primary_zim(primary.name):
        raise ValueError(f"Not a primary ZIM archive: {primary}")
    _require_regular_file(primary)
    if primary.name.lower().endswith(".zim"):
        return [primary]

    base = primary.name[:-2]
    part_pattern = re.compile(rf"{re.escape(base)}[A-Za-z]{{2}}")
    parts = [
        candidate
        for candidate in primary.parent.iterdir()
        if part_pattern.fullmatch(candidate.name)
    ]
    parts.sort(key=lambda candidate: candidate.name.casefold())
    for part in parts:
        _require_regular_file(part)
    return parts


def trash_archive(
    path: Path | str, trash_operation: Callable[[Path], None]
) -> list[Path]:
    files = archive_files(path)
    for archive_path in files:
        trash_operation(archive_path)
    return files


def _split_archive_size(path: Path) -> int:
    return sum(part.stat().st_size for part in archive_files(path))


def _filename_metadata(path: Path) -> dict[str, str]:
    stem = re.sub(r"\.zim(?:aa)?$", "", path.name, flags=re.IGNORECASE)
    parts = [part for part in stem.split("_") if part]
    date = ""
    if parts and re.fullmatch(r"20\d{2}-\d{2}", parts[-1]):
        date = parts.pop()

    source = parts.pop(0) if parts else stem
    source_title = source.replace("-", " ").title()
    language_code = ""
    if parts and re.fullmatch(r"[a-z]{2,3}(?:-[a-z]{2})?", parts[0], re.IGNORECASE):
        language_code = parts.pop(0).lower()

    edition = []
    for part in parts:
        edition.append(_EDITION_WORDS.get(part.lower(), part.replace("-", " ").title()))

    language = _LANGUAGE_NAMES.get(language_code, language_code.upper())
    return {
        "title": source_title or path.stem,
        "language": language,
        "languageCode": language_code,
        "edition": " / ".join(edition),
        "date": date,
    }


def _book_record(
    path: Path,
    catalog: dict[str, str] | None = None,
    download_dir: Path | None = None,
) -> dict[str, Any]:
    stat = path.stat()
    inferred = _filename_metadata(path)
    catalog = catalog or {}
    title = catalog.get("title") or catalog.get("name") or inferred["title"]
    language_code = catalog.get("language", "").split(",", 1)[0].strip().lower()
    language = _LANGUAGE_NAMES.get(language_code, language_code.upper())
    if not language:
        language = inferred["language"]

    description = catalog.get("description", "").strip()
    origin = "external"
    if download_dir is not None:
        try:
            if path.resolve().is_relative_to(download_dir.expanduser().resolve()):
                origin = "managed"
        except OSError:
            pass

    return {
        "title": (title.strip() or inferred["title"])[:240],
        "description": description[:500],
        "path": str(path),
        "filename": path.name,
        "directory": str(path.parent),
        "sizeBytes": _split_archive_size(path),
        "modifiedTs": int(stat.st_mtime),
        "language": language,
        "languageCode": language_code or inferred["languageCode"],
        "edition": inferred["edition"],
        "date": catalog.get("date", "") or inferred["date"],
        "origin": origin,
    }


def _library_stats(directory: Path) -> dict[str, Any]:
    stats: dict[str, Any] = {"path": str(directory)}
    try:
        usage = shutil.disk_usage(directory)
    except OSError as error:
        stats.update(
            totalBytes=None,
            usedBytes=None,
            freeBytes=None,
            error=error.strerror or str(error),
        )
    else:
        stats.update(
            totalBytes=usage.total,
            usedBytes=usage.used,
            freeBytes=usage.free,
        )
    return stats


def collect_library() -> dict[str, Any]:
    started = time.monotonic()
    config = load_config()
    directories = [Path(value).expanduser() for value in config["libraryDirs"]]
    download_dir = Path(config.get("downloadDir", default_download_dir())).expanduser()
    catalog = _read_kiwix_catalog()
    paths = set(catalog)
    errors = []

    for directory in directories:
        found, error = _scan_dir(directory)
        paths.update(found)
        if error:
            errors.append(error)

    books = []
    for path in paths:
        try:
            books.append(_book_record(path, catalog.get(path), download_dir))
        except OSError as error:
            errors.append(f"{path}: {error.strerror or error}")

    books.sort(key=lambda book: (book["title"].casefold(), book["path"].casefold()))
    return {
        "ok": True,
        "readerAvailable": shutil.which("kiwix-serve") is not None,
        "downloadAvailable": shutil.which("aria2c") is not None,
        "appInstalled": shutil.which("omawix") is not None,
        "bookCount": len(books),
        "totalBytes": sum(book["sizeBytes"] for book in books),
        "libraryDirs": [str(path) for path in directories],
        "downloadDir": str(download_dir),
        "libraryStats": [_library_stats(path) for path in directories],
        "books": books,
        "errors": errors,
        "scanDurationMs": round((time.monotonic() - started) * 1000),
    }
