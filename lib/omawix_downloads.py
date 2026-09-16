from __future__ import annotations

import base64
import hashlib
import ipaddress
import itertools
import json
import math
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Mapping, Protocol, Sequence


STATE_HOME = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
DEFAULT_STATE_DIR = STATE_HOME / "omawix"


MAX_METALINK_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_RPC_RESPONSE_BYTES = 1024 * 1024
TRUSTED_DOWNLOAD_DOMAIN = "download.kiwix.org"
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class DownloadError(RuntimeError):
    pass


class RPCError(DownloadError):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class DownloadValidationError(DownloadError, ValueError):
    pass


class PathSafetyError(DownloadValidationError):
    pass


class InsufficientSpaceError(DownloadValidationError):
    def __init__(self, required: int, available: int) -> None:
        super().__init__(
            f"Not enough free space: {required} bytes required, {available} available"
        )
        self.required = required
        self.available = available


class DownloadStateStore(Protocol):
    """Minimal persistence contract expected by DownloadManager."""

    def save_download(self, catalog_id: str, state: Mapping[str, Any]) -> None: ...

    def load_download(self, catalog_id: str) -> Mapping[str, Any] | None: ...

    def delete_download_state(self, catalog_id: str) -> bool: ...


@dataclass(frozen=True)
class MetalinkInfo:
    paths: tuple[Path, ...]
    total_size: int
    urls: tuple[str, ...]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def validate_public_https_url(
    url: str,
    *,
    purpose: str,
    resolver: Callable[..., Any] | None = None,
    trusted_download_host: bool = False,
) -> None:
    parsed = urllib.parse.urlsplit(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https" or not hostname:
        raise DownloadValidationError(f"{purpose} must use HTTPS")
    if hostname == "localhost":
        raise DownloadValidationError(f"{purpose} cannot target localhost")
    if trusted_download_host and not (
        hostname == TRUSTED_DOWNLOAD_DOMAIN
        or hostname.endswith(f".{TRUSTED_DOWNLOAD_DOMAIN}")
    ):
        raise DownloadValidationError(
            f"{purpose} must use an official Kiwix download host"
        )
    try:
        literal_address = ipaddress.ip_address(hostname)
    except ValueError:
        literal_address = None
    if literal_address is not None and not literal_address.is_global:
        raise DownloadValidationError(f"{purpose} cannot target a non-public address")
    if resolver is None or literal_address is not None:
        return
    try:
        addresses = {
            ipaddress.ip_address(item[4][0])
            for item in resolver(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        }
    except (OSError, ValueError) as error:
        raise DownloadValidationError(f"Could not resolve {purpose}: {error}") from error
    if not addresses or any(not address.is_global for address in addresses):
        raise DownloadValidationError(f"{purpose} resolved to a non-public address")


def resolve_output_path(destination: str | Path, output_name: str) -> Path:
    """Resolve a Metalink/aria2 output name and require it to stay in destination."""

    if not isinstance(output_name, str) or not output_name or "\x00" in output_name:
        raise PathSafetyError("Output path is empty or invalid")

    posix_path = PurePosixPath(output_name)
    windows_path = PureWindowsPath(output_name)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or ".." in posix_path.parts
        or ".." in windows_path.parts
    ):
        raise PathSafetyError(f"Output path escapes destination: {output_name!r}")

    root = Path(destination).expanduser().resolve()
    candidate = (root / Path(*posix_path.parts)).resolve()
    if candidate == root or not candidate.is_relative_to(root):
        raise PathSafetyError(f"Output path escapes destination: {output_name!r}")
    return candidate


def inspect_metalink(data: bytes, destination: str | Path) -> MetalinkInfo:
    """Parse Metalink bytes, calculate their size, and validate every output path."""

    if not isinstance(data, bytes) or not data:
        raise DownloadValidationError("Metalink response is empty")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise DownloadValidationError(f"Invalid Metalink XML: {error}") from error
    if _local_name(root.tag) != "metalink":
        raise DownloadValidationError("Document is not a Metalink")

    paths: list[Path] = []
    unique_paths: set[Path] = set()
    total_size = 0
    all_urls: list[str] = []
    for node in root.iter():
        if _local_name(node.tag) != "file":
            continue
        name = node.attrib.get("name", "")
        output_path = resolve_output_path(destination, name)
        if output_path in unique_paths:
            raise DownloadValidationError(
                f"Metalink contains duplicate output path: {name!r}"
            )
        unique_paths.add(output_path)
        paths.append(output_path)
        hashes = [
            child.text.strip()
            for child in node
            if _local_name(child.tag) == "hash"
            and child.attrib.get("type", "").lower() in {"sha-256", "sha256"}
            and child.text
        ]
        if not any(re.fullmatch(r"[0-9a-fA-F]{64}", digest) for digest in hashes):
            raise DownloadValidationError(
                f"Metalink file {name!r} has no valid SHA-256 digest"
            )
        urls = [
            child.text.strip()
            for child in node
            if _local_name(child.tag) == "url" and child.text
        ]
        if not urls:
            raise DownloadValidationError(f"Metalink file {name!r} has no download URLs")
        for url in urls:
            validate_public_https_url(url, purpose="Metalink download URL")
            all_urls.append(url)
        sizes = [
            child.text.strip()
            for child in node
            if _local_name(child.tag) == "size" and child.text
        ]
        if sizes:
            try:
                size = int(sizes[0])
            except ValueError as error:
                raise DownloadValidationError(
                    f"Invalid Metalink size for {name!r}"
                ) from error
            if size < 0:
                raise DownloadValidationError(
                    f"Invalid Metalink size for {name!r}"
                )
            total_size += size

    if not paths:
        raise DownloadValidationError("Metalink contains no files")
    return MetalinkInfo(tuple(paths), total_size, tuple(all_urls))


def filter_metalink(
    data: bytes, destination: str | Path, excluded_paths: set[Path]
) -> bytes:
    """Return a Metalink containing only files that still need downloading."""
    root = ET.fromstring(data)
    for node in list(root):
        if _local_name(node.tag) != "file":
            continue
        output_path = resolve_output_path(destination, node.attrib.get("name", ""))
        if output_path in excluded_paths:
            root.remove(node)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def sanitize_metalink_sources(data: bytes) -> bytes:
    """Keep only official Kiwix download URLs before handing data to aria2."""
    root = ET.fromstring(data)
    for parent in root.iter():
        for node in list(parent):
            if _local_name(node.tag) == "metaurl":
                parent.remove(node)
    for file_node in root.iter():
        if _local_name(file_node.tag) != "file":
            continue
        url_nodes = [node for node in file_node if _local_name(node.tag) == "url"]
        for url_node in url_nodes:
            url = (url_node.text or "").strip()
            try:
                validate_public_https_url(
                    url,
                    purpose="Metalink download URL",
                    trusted_download_host=True,
                )
            except DownloadValidationError:
                file_node.remove(url_node)
        if not any(_local_name(node.tag) == "url" for node in file_node):
            raise DownloadValidationError(
                "Metalink file has no official Kiwix download URL"
            )
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def validate_destination(
    destination: str | Path,
    required_bytes: int,
    disk_usage_fn: Callable[[str | os.PathLike[str]], Any] = shutil.disk_usage,
) -> Path:
    """Return a canonical writable destination with sufficient free space."""

    if isinstance(required_bytes, bool) or not isinstance(required_bytes, int):
        raise DownloadValidationError("Required size must be an integer")
    if required_bytes < 0:
        raise DownloadValidationError("Required size cannot be negative")

    path = Path(destination).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise DownloadValidationError(f"Destination does not exist: {path}") from error
    if not resolved.is_dir():
        raise DownloadValidationError(f"Destination is not a directory: {resolved}")
    if not os.access(resolved, os.W_OK):
        raise DownloadValidationError(f"Destination is not writable: {resolved}")

    available = int(disk_usage_fn(resolved).free)
    if available < required_bytes:
        raise InsufficientSpaceError(required_bytes, available)
    return resolved


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def normalize_status(
    raw: Mapping[str, Any], destination: str | Path | None = None
) -> dict[str, Any]:
    """Convert aria2's string-heavy tellStatus response to UI-neutral values."""

    status_map = {
        "active": "downloading",
        "waiting": "queued",
        "paused": "paused",
        "complete": "completed",
        "error": "error",
        "removed": "cancelled",
    }
    raw_status = str(raw.get("status", "unknown")).lower()
    total = _nonnegative_int(raw.get("totalLength"))
    completed = _nonnegative_int(raw.get("completedLength"))
    speed = _nonnegative_int(raw.get("downloadSpeed"))
    remaining = max(0, total - completed)
    if total:
        progress = min(1.0, completed / total)
    else:
        progress = 1.0 if raw_status == "complete" else 0.0
    eta = math.ceil(remaining / speed) if remaining and speed else None

    paths: list[str] = []
    files = raw.get("files", [])
    if isinstance(files, Sequence) and not isinstance(files, (str, bytes)):
        for file_record in files:
            if not isinstance(file_record, Mapping):
                continue
            raw_path = file_record.get("path")
            if not raw_path:
                continue
            if destination is None:
                path = Path(str(raw_path)).expanduser().resolve()
            else:
                root = Path(destination).expanduser().resolve()
                supplied = Path(str(raw_path)).expanduser()
                path = supplied.resolve() if supplied.is_absolute() else (root / supplied).resolve()
                if path == root or not path.is_relative_to(root):
                    raise PathSafetyError(
                        f"aria2 reported a path outside destination: {raw_path!r}"
                    )
            paths.append(str(path))

    error_message = str(raw.get("errorMessage") or "")
    error_code = str(raw.get("errorCode") or "")
    return {
        "gid": str(raw.get("gid") or ""),
        "status": status_map.get(raw_status, raw_status),
        "total_bytes": total,
        "completed_bytes": completed,
        "progress": progress,
        "speed_bytes_per_second": speed,
        "eta_seconds": eta,
        "path": paths[0] if paths else None,
        "paths": paths,
        "error": error_message or (f"aria2 error {error_code}" if error_code else None),
    }


def allocate_local_port() -> int:
    """Reserve an ephemeral loopback port long enough to discover its number."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def json_rpc_transport(
    endpoint: str, payload: Mapping[str, Any], timeout: float
) -> Mapping[str, Any]:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _NO_PROXY_OPENER.open(request, timeout=timeout) as response:
        body = response.read(MAX_RPC_RESPONSE_BYTES + 1)
    if len(body) > MAX_RPC_RESPONSE_BYTES:
        raise RPCError(f"aria2 RPC response exceeds {MAX_RPC_RESPONSE_BYTES} bytes")
    decoded = json.loads(body.decode("utf-8"))
    if not isinstance(decoded, Mapping):
        raise RPCError("aria2 returned a non-object JSON-RPC response")
    return decoded


class Aria2RPCClient:
    def __init__(
        self,
        endpoint: str,
        secret: str,
        *,
        transport: Callable[[str, Mapping[str, Any], float], Mapping[str, Any]] = json_rpc_transport,
        timeout: float = 5.0,
    ) -> None:
        self.endpoint = endpoint
        self._secret = secret
        self._transport = transport
        self.timeout = timeout
        self._ids = itertools.count(1)
        self._id_lock = threading.Lock()

    def call(self, method: str, params: Sequence[Any] = ()) -> Any:
        with self._id_lock:
            request_id = next(self._ids)
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": [f"token:{self._secret}", *list(params)],
        }
        try:
            response = self._transport(self.endpoint, payload, self.timeout)
        except RPCError:
            raise
        except Exception as error:
            raise RPCError(f"aria2 RPC request failed: {error}") from error
        rpc_error = response.get("error")
        if isinstance(rpc_error, Mapping):
            code = rpc_error.get("code")
            raise RPCError(
                str(rpc_error.get("message") or "aria2 RPC error"),
                int(code) if isinstance(code, int) else None,
            )
        if "result" not in response:
            raise RPCError("aria2 returned an invalid JSON-RPC response")
        return response["result"]


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, resolver: Callable[..., Any] | None) -> None:
        super().__init__()
        self._resolver = resolver

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        validate_public_https_url(
            new_url,
            purpose="Metalink redirect",
            resolver=self._resolver,
            trusted_download_host=True,
        )
        return super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )


def fetch_metalink(
    url: str,
    *,
    opener: Callable[..., Any] | None = None,
    timeout: float = 30.0,
    resolver: Callable[..., Any] | None = None,
) -> bytes:
    validate_public_https_url(
        url,
        purpose="Metalink URL",
        resolver=resolver,
        trusted_download_host=True,
    )
    parsed = urllib.parse.urlparse(url)
    if not parsed.path.lower().endswith(".meta4"):
        raise DownloadValidationError("Catalog URL must point to a .meta4 file")

    request = urllib.request.Request(
        url,
        headers={"Accept": "application/metalink4+xml, application/xml", "User-Agent": "Omawix/1"},
        method="GET",
    )
    try:
        open_request = opener or urllib.request.build_opener(
            _SafeRedirectHandler(resolver)
        ).open
        response = open_request(request, timeout=timeout)
        try:
            final_url = response.geturl() if hasattr(response, "geturl") else url
            validate_public_https_url(
                final_url,
                purpose="Metalink redirect",
                resolver=resolver,
                trusted_download_host=True,
            )
            data = response.read(MAX_METALINK_RESPONSE_BYTES + 1)
            if len(data) > MAX_METALINK_RESPONSE_BYTES:
                raise DownloadError(
                    f"Metalink response exceeds {MAX_METALINK_RESPONSE_BYTES} bytes"
                )
        finally:
            response.close()
    except (DownloadError, DownloadValidationError):
        raise
    except Exception as error:
        raise DownloadError(f"Could not fetch Metalink: {error}") from error
    if not isinstance(data, bytes):
        raise DownloadError("Metalink response was not bytes")
    return data


class DownloadManager:
    """Own an aria2c process and expose synchronous operations for a GTK worker."""

    _STATUS_KEYS = [
        "gid",
        "status",
        "totalLength",
        "completedLength",
        "downloadSpeed",
        "errorCode",
        "errorMessage",
        "files",
    ]

    def __init__(
        self,
        state_store: DownloadStateStore,
        *,
        state_dir: str | Path = DEFAULT_STATE_DIR,
        max_concurrent_downloads: int = 3,
        aria2c_path: str = "aria2c",
        process_factory: Callable[..., Any] = subprocess.Popen,
        rpc_transport: Callable[[str, Mapping[str, Any], float], Mapping[str, Any]] = json_rpc_transport,
        url_opener: Callable[..., Any] | None = None,
        port_allocator: Callable[[], int] = allocate_local_port,
        disk_usage_fn: Callable[[str | os.PathLike[str]], Any] = shutil.disk_usage,
        host_resolver: Callable[..., Any] = socket.getaddrinfo,
        sleep_fn: Callable[[float], None] = time.sleep,
        rpc_timeout: float = 5.0,
        start_timeout: float = 5.0,
    ) -> None:
        self._validate_concurrency(max_concurrent_downloads)
        self.state_store = state_store
        self.state_dir = Path(state_dir).expanduser()
        self.max_concurrent_downloads = max_concurrent_downloads
        self.aria2c_path = aria2c_path
        self._process_factory = process_factory
        self._rpc_transport = rpc_transport
        self._url_opener = url_opener
        self._port_allocator = port_allocator
        self._disk_usage = disk_usage_fn
        self._host_resolver = host_resolver
        self._sleep = sleep_fn
        self._rpc_timeout = rpc_timeout
        self._start_timeout = start_timeout
        self._process: Any | None = None
        self._client: Aria2RPCClient | None = None
        self.endpoint: str | None = None
        self.secret: str | None = None
        self._lock = threading.RLock()

    @staticmethod
    def _validate_concurrency(value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("max_concurrent_downloads must be a positive integer")

    def __enter__(self) -> DownloadManager:
        self.start()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.shutdown()

    def start(self) -> None:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return

            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.state_dir.chmod(0o700)
            session_path = self.state_dir / "aria2.session"
            session_path.touch(exist_ok=True, mode=0o600)
            session_path.chmod(0o600)
            port = self._port_allocator()
            secret = secrets.token_urlsafe(32)
            endpoint = f"http://127.0.0.1:{port}/jsonrpc"
            rpc_config_path = self.state_dir / "aria2-rpc.conf"
            descriptor = os.open(
                rpc_config_path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            try:
                os.write(descriptor, f"rpc-secret={secret}\n".encode("utf-8"))
            finally:
                os.close(descriptor)
            rpc_config_path.chmod(0o600)
            command = [
                self.aria2c_path,
                f"--conf-path={rpc_config_path}",
                "--enable-rpc=true",
                "--rpc-listen-all=false",
                f"--rpc-listen-port={port}",
                "--check-integrity=true",
                "--continue=true",
                f"--input-file={session_path}",
                f"--save-session={session_path}",
                "--save-session-interval=30",
                "--auto-file-renaming=false",
                "--allow-overwrite=false",
                f"--max-concurrent-downloads={self.max_concurrent_downloads}",
                f"--stop-with-process={os.getpid()}",
            ]
            try:
                process = self._process_factory(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                rpc_config_path.unlink(missing_ok=True)
                raise
            client = Aria2RPCClient(
                endpoint,
                secret,
                transport=self._rpc_transport,
                timeout=self._rpc_timeout,
            )
            self._process = process
            self._client = client
            self.endpoint = endpoint
            self.secret = secret

            deadline = time.monotonic() + self._start_timeout
            last_error: Exception | None = None
            while time.monotonic() <= deadline:
                if process.poll() is not None:
                    last_error = DownloadError(
                        f"aria2c exited during startup with code {process.returncode}"
                    )
                    break
                try:
                    client.call("aria2.getVersion")
                    rpc_config_path.unlink(missing_ok=True)
                    return
                except RPCError as error:
                    last_error = error
                    self._sleep(0.05)

            self._stop_process(process)
            self._clear_process()
            rpc_config_path.unlink(missing_ok=True)
            raise DownloadError(f"aria2c RPC did not become ready: {last_error}")

    def fetch_catalog(self, url: str) -> bytes:
        return fetch_metalink(
            url, opener=self._url_opener, resolver=self._host_resolver
        )

    def add_catalog(
        self,
        catalog_id: str,
        meta4_url: str,
        destination: str | Path,
        *,
        expected_size: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> list[str]:
        with self._lock:
            self._require_catalog_id(catalog_id)
            if self.state_store.load_download(catalog_id) is not None:
                raise DownloadError(f"Catalog already has download state: {catalog_id}")
            metalink, root, required, info = self._prepare(
                meta4_url, destination, expected_size, allow_existing=False
            )
            return self._submit(
                catalog_id,
                meta4_url,
                metalink,
                root,
                required,
                expected_size,
                metadata or {},
                info,
            )

    def poll(self, catalog_id: str) -> list[dict[str, Any]]:
        with self._lock:
            record = self._load_record(catalog_id)
            client = self._client_or_raise()
            destination = str(record["destination"])
            statuses = []
            missing_gid = False
            for gid in record["gids"]:
                try:
                    raw = client.call("aria2.tellStatus", [gid, self._STATUS_KEYS])
                except RPCError as error:
                    if error.code != 1 or str(record.get("status")) not in {
                        "queued", "downloading", "paused"
                    }:
                        raise
                    missing_gid = True
                    continue
                if not isinstance(raw, Mapping):
                    raise RPCError("aria2.tellStatus returned an invalid result")
                status = normalize_status(raw, destination)
                status["catalog_id"] = catalog_id
                statuses.append(status)
            if missing_gid:
                record = self._recover_record(record, statuses)
                if not record["gids"]:
                    return [{
                        "gid": "",
                        "catalog_id": catalog_id,
                        "status": "completed",
                        "completed_bytes": int(record.get("required_bytes") or 0),
                        "total_bytes": int(record.get("required_bytes") or 0),
                        "speed_bytes_per_second": 0,
                        "paths": list(record.get("output_paths") or []),
                        "error": None,
                    }]
                statuses = []
                for gid in record["gids"]:
                    raw = client.call("aria2.tellStatus", [gid, self._STATUS_KEYS])
                    if not isinstance(raw, Mapping):
                        raise RPCError("aria2.tellStatus returned an invalid result")
                    status = normalize_status(raw, destination)
                    status["catalog_id"] = catalog_id
                    statuses.append(status)
            aggregate = self._aggregate_status(statuses)
            updated = dict(record)
            updated["status"] = aggregate
            updated["completed_bytes"] = sum(item["completed_bytes"] for item in statuses)
            updated["total_bytes"] = sum(item["total_bytes"] for item in statuses)
            updated["speed_bytes_per_second"] = sum(
                item["speed_bytes_per_second"] for item in statuses
            )
            updated["paths"] = [
                path for item in statuses for path in item.get("paths", [])
            ]
            if aggregate == "completed":
                updated["paths"] = list(record.get("output_paths") or updated["paths"])
            updated["gid_states"] = {
                item["gid"]: item for item in statuses if item.get("gid")
            }
            errors = [item["error"] for item in statuses if item.get("error")]
            updated["error"] = errors[0] if errors else ""
            self.state_store.save_download(catalog_id, updated)
            return statuses

    def pause(self, catalog_id: str) -> None:
        self._apply_to_catalog(catalog_id, "aria2.pause")

    def resume(self, catalog_id: str) -> None:
        self._apply_to_catalog(catalog_id, "aria2.unpause")

    def cancel(self, catalog_id: str, *, delete_files: bool = True) -> list[Path]:
        """Cancel a transfer and remove only files reported inside its destination."""
        with self._lock:
            record = self._load_record(catalog_id)
            self._stop_record_gids(record)

            removed: list[Path] = []
            if delete_files:
                root = Path(str(record["destination"])).expanduser().resolve()
                owned_paths = record.get("output_paths", [])
                if not isinstance(owned_paths, Sequence) or isinstance(owned_paths, (str, bytes)):
                    owned_paths = []
                for raw_path in set(owned_paths):
                    path = Path(raw_path).resolve()
                    if path == root or not path.is_relative_to(root):
                        raise PathSafetyError(
                            f"Refusing to remove path outside destination: {path}"
                        )
                    for candidate in (path, Path(str(path) + ".aria2")):
                        try:
                            candidate.unlink()
                        except FileNotFoundError:
                            continue
                        removed.append(candidate)
            metalink_path = record.get("metalink_path")
            if isinstance(metalink_path, str):
                try:
                    Path(metalink_path).unlink()
                except FileNotFoundError:
                    pass
            self.state_store.delete_download_state(catalog_id)
            return removed

    def retry(self, catalog_id: str) -> list[str]:
        with self._lock:
            record = self._load_record(catalog_id)
            if record.get("status") not in {"error", "cancelled"}:
                self.poll(catalog_id)
                record = self._load_record(catalog_id)
                if record.get("status") not in {"error", "cancelled"}:
                    raise DownloadError("Only failed or cancelled downloads can be retried")

            statuses = self._stop_record_gids(record)

            meta4_url = str(record["meta4_url"])
            destination = str(record["destination"])
            expected_size = record.get("expected_size")
            metalink, root, required, info = self._prepare(
                meta4_url,
                destination,
                expected_size if isinstance(expected_size, int) else None,
                allow_existing=True,
            )
            persistent_metalink = metalink
            completed_paths = self._completed_paths(record, statuses)
            expected_paths = set(info.paths)
            completed_paths.intersection_update(expected_paths)
            if completed_paths == expected_paths:
                completed = dict(record)
                completed["gids"] = []
                completed["status"] = "completed"
                completed["paths"] = [str(path) for path in info.paths]
                completed["completed_paths"] = completed["paths"]
                completed["error"] = ""
                self.state_store.save_download(catalog_id, completed)
                return []
            metalink = filter_metalink(metalink, root, completed_paths)
            pending_info = inspect_metalink(metalink, root)
            metadata = record.get("metadata")
            if not isinstance(metadata, Mapping):
                metadata = {}
            return self._submit(
                catalog_id,
                meta4_url,
                metalink,
                root,
                required,
                expected_size if isinstance(expected_size, int) else None,
                metadata,
                pending_info,
                all_output_paths=info.paths,
                completed_paths=completed_paths,
                persistent_metalink=persistent_metalink,
            )

    def _stop_record_gids(self, record: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Stop every known aria2 job before its files or state can be reused."""
        client = self._client_or_raise()
        destination = str(record["destination"])
        statuses: list[dict[str, Any]] = []
        for gid in record["gids"]:
            try:
                raw = client.call("aria2.tellStatus", [gid, self._STATUS_KEYS])
            except RPCError as error:
                if error.code == 1:
                    continue
                raise
            if not isinstance(raw, Mapping):
                raise RPCError("aria2.tellStatus returned an invalid result")
            status = str(raw.get("status") or "")
            statuses.append(normalize_status(raw, destination))
            method = (
                "aria2.removeDownloadResult"
                if status in {"complete", "error", "removed"}
                else "aria2.forceRemove"
            )
            try:
                client.call(method, [gid])
            except RPCError as error:
                if error.code != 1:
                    raise
        return statuses

    @staticmethod
    def _completed_paths(
        record: Mapping[str, Any], statuses: Sequence[Mapping[str, Any]]
    ) -> set[Path]:
        completed: set[Path] = set()
        persisted = record.get("completed_paths")
        if isinstance(persisted, Sequence) and not isinstance(persisted, (str, bytes)):
            completed.update(
                path
                for raw_path in persisted
                if (path := Path(str(raw_path)).resolve()).is_file()
            )
        previous = record.get("gid_states")
        combined: list[Mapping[str, Any]] = []
        if isinstance(previous, Mapping):
            combined.extend(
                value for value in previous.values() if isinstance(value, Mapping)
            )
        combined.extend(statuses)
        for status in combined:
            if status.get("status") != "completed":
                continue
            paths = status.get("paths")
            if isinstance(paths, Sequence) and not isinstance(paths, (str, bytes)):
                completed.update(
                    path
                    for raw_path in paths
                    if (path := Path(str(raw_path)).resolve()).is_file()
                )
        return completed

    def set_max_concurrent_downloads(self, value: int) -> None:
        self._validate_concurrency(value)
        with self._lock:
            self._client_or_raise().call(
                "aria2.changeGlobalOption", [{"max-concurrent-downloads": str(value)}]
            )
            self.max_concurrent_downloads = value

    def shutdown(self) -> None:
        with self._lock:
            process = self._process
            client = self._client
            if process is None:
                return

            first_error: Exception | None = None
            if process.poll() is None and client is not None:
                for method in ("aria2.pauseAll", "aria2.saveSession", "aria2.shutdown"):
                    try:
                        client.call(method)
                    except Exception as error:
                        if first_error is None:
                            first_error = error
            self._stop_process(process)
            self._clear_process()
            if first_error is not None:
                raise DownloadError(f"aria2c shutdown was incomplete: {first_error}") from first_error

    def _prepare(
        self,
        meta4_url: str,
        destination: str | Path,
        expected_size: int | None,
        *,
        allow_existing: bool,
    ) -> tuple[bytes, Path, int, MetalinkInfo]:
        root = validate_destination(destination, 0, self._disk_usage)
        metalink = sanitize_metalink_sources(self.fetch_catalog(meta4_url))
        info = inspect_metalink(metalink, root)
        for url in info.urls:
            validate_public_https_url(
                url, purpose="Metalink download URL", resolver=self._host_resolver
            )
        if not allow_existing:
            collisions = [
                path for path in info.paths
                if path.exists() or Path(str(path) + ".aria2").exists()
            ]
            if collisions:
                raise DownloadValidationError(
                    f"Download target already exists: {collisions[0]}"
                )
        if expected_size is not None:
            if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 0:
                raise DownloadValidationError("Expected size must be a non-negative integer")
            required = max(expected_size, info.total_size)
        else:
            required = info.total_size
        space_required = required
        if allow_existing:
            allocated = 0
            for path in info.paths:
                try:
                    details = path.stat()
                except FileNotFoundError:
                    continue
                blocks = getattr(details, "st_blocks", 0) * 512
                allocated += min(details.st_size, blocks) if blocks else details.st_size
            space_required = max(0, required - allocated)
        root = validate_destination(root, space_required, self._disk_usage)
        return metalink, root, required, info

    def _submit(
        self,
        catalog_id: str,
        meta4_url: str,
        metalink: bytes,
        destination: Path,
        required: int,
        expected_size: int | None,
        metadata: Mapping[str, Any],
        info: MetalinkInfo,
        *,
        all_output_paths: Sequence[Path] | None = None,
        completed_paths: set[Path] | None = None,
        persistent_metalink: bytes | None = None,
    ) -> list[str]:
        options = {
            "dir": str(destination),
            "check-integrity": "true",
            "continue": "true",
            "auto-file-renaming": "false",
            "allow-overwrite": "false",
        }
        metalink_dir = self.state_dir / "metalinks"
        metalink_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        metalink_dir.chmod(0o700)
        metalink_name = hashlib.sha256(catalog_id.encode("utf-8")).hexdigest() + ".meta4"
        metalink_path = metalink_dir / metalink_name
        metalink_path.write_bytes(persistent_metalink or metalink)
        metalink_path.chmod(0o600)
        encoded = base64.b64encode(metalink).decode("ascii")
        result = self._client_or_raise().call("aria2.addMetalink", [encoded, options])
        if (
            not isinstance(result, Sequence)
            or isinstance(result, (str, bytes))
            or not result
            or any(not isinstance(gid, str) or not gid for gid in result)
        ):
            raise RPCError("aria2.addMetalink returned invalid GIDs")
        gids = list(result)
        state = {
            "catalog_id": catalog_id,
            "gids": gids,
            "meta4_url": meta4_url,
            "destination": str(destination),
            "required_bytes": required,
            "expected_size": expected_size,
            "metadata": dict(metadata),
            "metalink_path": str(metalink_path),
            "output_paths": [
                str(path)
                for path in (
                    all_output_paths if all_output_paths is not None else info.paths
                )
            ],
            "completed_paths": [str(path) for path in sorted(completed_paths or set())],
            "gid_states": {},
        }
        try:
            self.state_store.save_download(catalog_id, state)
        except Exception:
            client = self._client_or_raise()
            for gid in gids:
                try:
                    client.call("aria2.forceRemove", [gid])
                except RPCError:
                    pass
            raise
        return gids

    def _recover_record(
        self,
        record: Mapping[str, Any],
        observed_statuses: Sequence[Mapping[str, Any]] = (),
    ) -> Mapping[str, Any]:
        """Recreate an RPC-added Metalink job whose GIDs did not survive restart."""
        metalink_path = record.get("metalink_path")
        destination = record.get("destination")
        catalog_id = record.get("catalog_id")
        if not all(isinstance(value, str) and value for value in (
            metalink_path, destination, catalog_id
        )):
            raise DownloadError("Download cannot be recovered because its Metalink is missing")
        try:
            metalink = sanitize_metalink_sources(Path(metalink_path).read_bytes())
        except OSError as error:
            raise DownloadError(f"Download Metalink could not be read: {error}") from error
        info = inspect_metalink(metalink, destination)
        for url in info.urls:
            validate_public_https_url(
                url, purpose="Metalink download URL", resolver=self._host_resolver
            )
        completed_paths = self._completed_paths(record, observed_statuses)
        expected_paths = set(info.paths)
        completed_paths.intersection_update(expected_paths)
        self._stop_record_gids(record)
        if completed_paths == expected_paths:
            updated = dict(record)
            updated["gids"] = []
            updated["status"] = "completed"
            updated["paths"] = [str(path) for path in info.paths]
            updated["completed_paths"] = updated["paths"]
            updated["error"] = ""
            self.state_store.save_download(catalog_id, updated)
            return updated
        metalink = filter_metalink(metalink, destination, completed_paths)
        inspect_metalink(metalink, destination)
        options = {
            "dir": destination,
            "check-integrity": "true",
            "continue": "true",
            "auto-file-renaming": "false",
            "allow-overwrite": "false",
        }
        encoded = base64.b64encode(metalink).decode("ascii")
        result = self._client_or_raise().call("aria2.addMetalink", [encoded, options])
        if not isinstance(result, Sequence) or isinstance(result, (str, bytes)) or not result:
            raise RPCError("aria2.addMetalink returned invalid recovery GIDs")
        updated = dict(record)
        updated["gids"] = list(result)
        updated["status"] = "queued"
        updated["error"] = ""
        updated["output_paths"] = [str(path) for path in info.paths]
        updated["completed_paths"] = [str(path) for path in sorted(completed_paths)]
        updated["gid_states"] = {}
        self.state_store.save_download(catalog_id, updated)
        return updated

    def _apply_to_catalog(self, catalog_id: str, method: str) -> None:
        with self._lock:
            record = self._load_record(catalog_id)
            client = self._client_or_raise()
            for gid in record["gids"]:
                client.call(method, [gid])

    @staticmethod
    def _aggregate_status(statuses: Sequence[Mapping[str, Any]]) -> str:
        values = {str(item.get("status", "")) for item in statuses}
        for status in ("error", "downloading", "queued", "paused", "cancelled"):
            if status in values:
                return status
        return "completed" if values == {"completed"} else "unknown"

    def _load_record(self, catalog_id: str) -> Mapping[str, Any]:
        self._require_catalog_id(catalog_id)
        record = self.state_store.load_download(catalog_id)
        if not isinstance(record, Mapping):
            raise DownloadError(f"No download state for catalog: {catalog_id}")
        gids = record.get("gids")
        destination = record.get("destination")
        if (
            not isinstance(gids, Sequence)
            or isinstance(gids, (str, bytes))
            or not gids
            or any(not isinstance(gid, str) or not gid for gid in gids)
            or not isinstance(destination, str)
        ):
            raise DownloadError(f"Invalid download state for catalog: {catalog_id}")
        return record

    @staticmethod
    def _require_catalog_id(catalog_id: str) -> None:
        if not isinstance(catalog_id, str) or not catalog_id.strip():
            raise DownloadValidationError("Catalog ID is required")

    def _client_or_raise(self) -> Aria2RPCClient:
        if self._client is None or self._process is None or self._process.poll() is not None:
            raise DownloadError("Download manager is not running")
        return self._client

    @staticmethod
    def _stop_process(process: Any) -> None:
        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            process.terminate()
        try:
            process.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    def _clear_process(self) -> None:
        self._process = None
        self._client = None
        self.endpoint = None
        self.secret = None


__all__ = [
    "Aria2RPCClient",
    "DownloadError",
    "DownloadManager",
    "DownloadStateStore",
    "DownloadValidationError",
    "InsufficientSpaceError",
    "MetalinkInfo",
    "PathSafetyError",
    "fetch_metalink",
    "inspect_metalink",
    "normalize_status",
    "resolve_output_path",
    "validate_destination",
]
