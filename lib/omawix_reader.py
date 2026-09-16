from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import unicodedata
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote


_HOST = "127.0.0.1"
_ZIM_SUFFIXES = (".zimaa", ".zim")


def zim_name_from_path(path: str | os.PathLike[str]) -> str:
    """Return the URL identifier that Kiwix derives from a ZIM filename."""
    filename = Path(path).name
    lower_filename = filename.lower()
    for suffix in _ZIM_SUFFIXES:
        if lower_filename.endswith(suffix):
            filename = filename[: -len(suffix)]
            break
    else:
        raise ValueError(f"Not a .zim or .zimaa archive: {path}")

    normalized = unicodedata.normalize("NFKD", filename.lower())
    without_diacritics = "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )
    return without_diacritics.replace(" ", "_").replace("+", "plus")


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((_HOST, 0))
        return int(sock.getsockname()[1])


class ReaderServer:
    """Manage a localhost-only kiwix-serve process for one ZIM archive."""

    def __init__(
        self,
        archive_path: str | os.PathLike[str],
        *,
        executable: str | os.PathLike[str] | None = None,
        startup_timeout: float = 10.0,
        shutdown_timeout: float = 3.0,
        request_timeout: float = 0.25,
        popen_factory: Callable[..., Any] | None = None,
        urlopen_func: Callable[..., Any] | None = None,
        port_selector: Callable[[], int] | None = None,
        executable_finder: Callable[[str], str | None] | None = None,
        sleep_func: Callable[[float], None] | None = None,
        monotonic_func: Callable[[], float] | None = None,
    ) -> None:
        path = Path(archive_path).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"ZIM archive does not exist: {path}")
        if not path.is_file():
            raise ValueError(f"ZIM archive is not a file: {path}")
        zim_name = zim_name_from_path(path)

        if startup_timeout <= 0:
            raise ValueError("startup_timeout must be greater than zero")
        if shutdown_timeout <= 0:
            raise ValueError("shutdown_timeout must be greater than zero")
        if request_timeout <= 0:
            raise ValueError("request_timeout must be greater than zero")

        self.archive_path = path.resolve()
        self.zim_name = zim_name
        self.executable = os.fspath(executable) if executable is not None else None
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout
        self.request_timeout = request_timeout

        self._popen = popen_factory or subprocess.Popen
        self._urlopen = urlopen_func or urllib.request.urlopen
        self._select_port = port_selector or _available_port
        self._find_executable = executable_finder or shutil.which
        self._sleep = sleep_func or time.sleep
        self._monotonic = monotonic_func or time.monotonic
        self._process: Any | None = None
        self._port: int | None = None

    @property
    def process(self) -> Any | None:
        return self._process

    @property
    def port(self) -> int | None:
        return self._port

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def base_url(self) -> str:
        if self._port is None:
            raise RuntimeError("Reader server has not been started")
        return f"http://{_HOST}:{self._port}"

    @property
    def viewer_url(self) -> str:
        return f"{self.base_url}/viewer#{quote(self.zim_name, safe='')}"

    @property
    def content_url(self) -> str:
        return f"{self.base_url}/content/{quote(self.zim_name, safe='')}"

    @property
    def home_url(self) -> str:
        return self.content_url

    def start(self) -> ReaderServer:
        if self.is_running:
            return self
        self._process = None
        self._port = None

        executable = self.executable or self._find_executable("kiwix-serve")
        if not executable:
            raise FileNotFoundError(
                "kiwix-serve executable was not found in PATH; install kiwix-tools "
                "or pass executable=..."
            )

        port = self._select_port()
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f"Port selector returned an invalid port: {port!r}")
        self._port = port

        command = [
            os.fspath(executable),
            f"--address={_HOST}",
            f"--port={port}",
            "--blockexternal",
            "--nolibrarybutton",
            f"--attachToProcess={os.getpid()}",
            os.fspath(self.archive_path),
        ]
        try:
            self._process = self._popen(command)
        except OSError as error:
            self._port = None
            raise RuntimeError(f"Could not launch kiwix-serve: {error}") from error

        try:
            self._wait_until_ready()
        except Exception:
            self.stop()
            raise
        return self

    def _wait_until_ready(self) -> None:
        deadline = self._monotonic() + self.startup_timeout
        readiness_url = f"{self.base_url}/"

        while True:
            process = self._process
            if process is None:
                raise RuntimeError("kiwix-serve process is not available")
            return_code = process.poll()
            if return_code is not None:
                raise RuntimeError(
                    f"kiwix-serve exited before becoming ready (status {return_code})"
                )

            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"kiwix-serve did not become ready within {self.startup_timeout:g} seconds"
                )

            try:
                response = self._urlopen(
                    readiness_url, timeout=min(self.request_timeout, remaining)
                )
            except OSError:
                self._sleep(min(0.05, remaining))
                continue

            close = getattr(response, "close", None)
            if callable(close):
                close()
            return

    def stop(self) -> None:
        process = self._process
        self._process = None
        self._port = None
        if process is None or process.poll() is not None:
            return

        process.terminate()
        try:
            process.wait(timeout=self.shutdown_timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=self.shutdown_timeout)

    def restart(self) -> ReaderServer:
        self.stop()
        return self.start()

    def __enter__(self) -> ReaderServer:
        return self.start()

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.stop()


__all__ = ["ReaderServer", "zim_name_from_path"]
