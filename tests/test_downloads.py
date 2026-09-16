from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import omawix_downloads as downloads


def public_resolver(_host, port, **_kwargs):
    return [(downloads.socket.AF_INET, downloads.socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


class RPCAuthenticationTests(unittest.TestCase):
    def test_default_rpc_transport_bypasses_environment_proxies(self) -> None:
        response = Mock()
        response.read.return_value = b'{"jsonrpc":"2.0","id":1,"result":"ok"}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)

        with unittest.mock.patch.object(
            downloads._NO_PROXY_OPENER, "open", return_value=response
        ) as open_request:
            result = downloads.json_rpc_transport(
                "http://127.0.0.1:6800/jsonrpc",
                {"jsonrpc": "2.0", "id": 1, "method": "aria2.getVersion"},
                2.0,
            )

        self.assertEqual(result["result"], "ok")
        open_request.assert_called_once()

    def test_secret_is_the_first_rpc_parameter(self) -> None:
        requests = []

        def transport(endpoint, payload, timeout):
            requests.append((endpoint, payload, timeout))
            return {"jsonrpc": "2.0", "id": payload["id"], "result": "ok"}

        client = downloads.Aria2RPCClient(
            "http://127.0.0.1:6800/jsonrpc",
            "random-secret",
            transport=transport,
        )

        result = client.call("aria2.tellStatus", ["gid-1", ["status"]])

        self.assertEqual(result, "ok")
        self.assertEqual(requests[0][1]["method"], "aria2.tellStatus")
        self.assertEqual(
            requests[0][1]["params"],
            ["token:random-secret", "gid-1", ["status"]],
        )

    def test_rpc_errors_are_normalized(self) -> None:
        def transport(_endpoint, _payload, _timeout):
            return {"error": {"code": 1, "message": "bad gid"}}

        client = downloads.Aria2RPCClient("http://localhost", "secret", transport=transport)

        with self.assertRaises(downloads.RPCError) as raised:
            client.call("aria2.tellStatus")

        self.assertEqual(raised.exception.code, 1)


class StatusNormalizationTests(unittest.TestCase):
    def test_normalizes_progress_speed_eta_and_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = downloads.normalize_status(
                {
                    "gid": "abc",
                    "status": "active",
                    "totalLength": "1000",
                    "completedLength": "250",
                    "downloadSpeed": "200",
                    "files": [{"path": str(Path(temporary) / "book.zim")}],
                },
                temporary,
            )

        self.assertEqual(result["status"], "downloading")
        self.assertEqual(result["total_bytes"], 1000)
        self.assertEqual(result["completed_bytes"], 250)
        self.assertEqual(result["progress"], 0.25)
        self.assertEqual(result["speed_bytes_per_second"], 200)
        self.assertEqual(result["eta_seconds"], 4)
        self.assertTrue(result["path"].endswith("book.zim"))

    def test_complete_zero_length_download_has_full_progress(self) -> None:
        result = downloads.normalize_status(
            {"gid": "abc", "status": "complete", "files": []}
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["progress"], 1.0)
        self.assertIsNone(result["eta_seconds"])

    def test_aggregates_transfer_states(self) -> None:
        manager = object.__new__(downloads.DownloadManager)
        self.assertEqual(
            manager._aggregate_status([{"status": "completed"}, {"status": "downloading"}]),
            "downloading",
        )
        self.assertEqual(
            manager._aggregate_status([{"status": "paused"}, {"status": "queued"}]),
            "queued",
        )


class DestinationSpaceTests(unittest.TestCase):
    def test_accepts_destination_with_exact_required_space(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = downloads.validate_destination(
                temporary,
                4096,
                lambda _path: SimpleNamespace(free=4096),
            )

        self.assertTrue(result.is_absolute())

    def test_rejects_destination_without_enough_space(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(downloads.InsufficientSpaceError) as raised:
                downloads.validate_destination(
                    temporary,
                    4097,
                    lambda _path: SimpleNamespace(free=4096),
                )

        self.assertEqual(raised.exception.required, 4097)
        self.assertEqual(raised.exception.available, 4096)


class PathSafetyTests(unittest.TestCase):
    def test_rejects_an_oversized_metalink_response(self) -> None:
        response = Mock()
        response.geturl.return_value = "https://lb.download.kiwix.org/book.zim.meta4"
        response.read.return_value = b"x" * (downloads.MAX_METALINK_RESPONSE_BYTES + 1)
        response.close = Mock()

        with self.assertRaisesRegex(downloads.DownloadError, "exceeds"):
            downloads.fetch_metalink(
                "https://lb.download.kiwix.org/book.zim.meta4",
                opener=Mock(return_value=response),
            )

    def test_aria2_secret_is_not_exposed_in_process_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            process = Mock()
            process.poll.return_value = None
            process_factory = Mock(return_value=process)
            rpc = Mock(return_value={"jsonrpc": "2.0", "id": 1, "result": {}})
            manager = downloads.DownloadManager(
                Mock(),
                state_dir=temporary,
                process_factory=process_factory,
                rpc_transport=rpc,
                port_allocator=lambda: 41234,
            )

            manager.start()

            command = process_factory.call_args.args[0]
            self.assertFalse(any(argument.startswith("--rpc-secret=") for argument in command))
            config_argument = next(
                argument for argument in command if argument.startswith("--conf-path=")
            )
            config_path = Path(config_argument.split("=", 1)[1])
            self.assertFalse(config_path.exists())
            manager.shutdown()

    def test_accepts_nested_metalink_output_under_destination(self) -> None:
        metalink = b"""<?xml version="1.0"?>
        <metalink xmlns="urn:ietf:params:xml:ns:metalink">
          <file name="archives/book.zim"><size>123</size>
            <hash type="sha-256">0000000000000000000000000000000000000000000000000000000000000000</hash>
            <url>https://mirror.download.kiwix.org/book.zim</url>
          </file>
        </metalink>"""
        with tempfile.TemporaryDirectory() as temporary:
            info = downloads.inspect_metalink(metalink, temporary)
            root = Path(temporary).resolve()

        self.assertEqual(info.total_size, 123)
        self.assertEqual(info.paths, (root / "archives/book.zim",))

    def test_rejects_metalink_parent_traversal(self) -> None:
        metalink = b"""<metalink xmlns="urn:ietf:params:xml:ns:metalink">
          <file name="../outside.zim"><size>123</size></file>
        </metalink>"""
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(downloads.PathSafetyError):
                downloads.inspect_metalink(metalink, temporary)

    def test_rejects_aria2_path_outside_persisted_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            outside = Path(temporary).parent / "outside.zim"
            with self.assertRaises(downloads.PathSafetyError):
                downloads.normalize_status(
                    {
                        "gid": "abc",
                        "status": "active",
                        "files": [{"path": str(outside)}],
                    },
                    temporary,
                )

    def test_rejects_output_through_symlink_outside_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
            root = Path(temporary)
            (root / "link").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(downloads.PathSafetyError):
                downloads.resolve_output_path(root, "link/book.zim")

    def test_new_download_rejects_an_existing_target(self) -> None:
        metalink = b"""<metalink xmlns="urn:ietf:params:xml:ns:metalink">
          <file name="book.zim"><size>123</size>
            <hash type="sha-256">0000000000000000000000000000000000000000000000000000000000000000</hash>
            <url>https://mirror.download.kiwix.org/book.zim</url>
          </file>
        </metalink>"""
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / "book.zim").write_bytes(b"existing")
            manager = downloads.DownloadManager(Mock(), host_resolver=public_resolver)
            manager.fetch_catalog = lambda _url: metalink

            with self.assertRaisesRegex(
                downloads.DownloadValidationError, "already exists"
            ):
                manager._prepare(
                    "https://lb.download.kiwix.org/book.zim.meta4",
                    temporary,
                    123,
                    allow_existing=False,
                )

    def test_rejects_metalink_without_strong_integrity_metadata(self) -> None:
        metalink = b"""<metalink xmlns="urn:ietf:params:xml:ns:metalink">
          <file name="book.zim"><size>123</size>
            <url>https://mirror.download.kiwix.org/book.zim</url>
          </file>
        </metalink>"""

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(downloads.DownloadValidationError, "SHA-256"):
                downloads.inspect_metalink(metalink, temporary)

    def test_rejects_metalink_downloads_from_private_networks(self) -> None:
        metalink = b"""<metalink xmlns="urn:ietf:params:xml:ns:metalink">
          <file name="book.zim"><size>123</size>
            <hash type="sha-256">0000000000000000000000000000000000000000000000000000000000000000</hash>
            <url>https://127.0.0.1/book.zim</url>
          </file>
        </metalink>"""

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(downloads.DownloadValidationError, "non-public"):
                downloads.inspect_metalink(metalink, temporary)

    def test_rejects_metalink_hostnames_resolving_to_private_networks(self) -> None:
        metalink = b"""<metalink xmlns="urn:ietf:params:xml:ns:metalink">
          <file name="book.zim"><size>123</size>
            <hash type="sha-256">0000000000000000000000000000000000000000000000000000000000000000</hash>
            <url>https://mirror.download.kiwix.org/book.zim</url>
          </file>
        </metalink>"""
        with tempfile.TemporaryDirectory() as temporary:
            manager = downloads.DownloadManager(
                Mock(),
                host_resolver=lambda _host, port, **_kwargs: [
                    (downloads.socket.AF_INET, downloads.socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))
                ],
            )
            manager.fetch_catalog = lambda _url: metalink

            with self.assertRaisesRegex(downloads.DownloadValidationError, "resolved"):
                manager._prepare(
                    "https://lb.download.kiwix.org/book.zim.meta4",
                    temporary,
                    123,
                    allow_existing=False,
                )

    def test_rejects_duplicate_metalink_output_paths(self) -> None:
        metalink = b"""<metalink xmlns="urn:ietf:params:xml:ns:metalink">
          <file name="book.zim"><size>100</size>
            <hash type="sha-256">0000000000000000000000000000000000000000000000000000000000000000</hash>
            <url>https://mirror.download.kiwix.org/one.zim</url>
          </file>
          <file name="book.zim"><size>100</size>
            <hash type="sha-256">1111111111111111111111111111111111111111111111111111111111111111</hash>
            <url>https://mirror.download.kiwix.org/two.zim</url>
          </file>
        </metalink>"""

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(downloads.DownloadValidationError, "duplicate"):
                downloads.inspect_metalink(metalink, temporary)

    def test_sanitizes_metalink_to_official_kiwix_sources(self) -> None:
        metalink = b"""<metalink xmlns="urn:ietf:params:xml:ns:metalink">
          <file name="book.zim"><size>100</size>
            <hash type="sha-256">0000000000000000000000000000000000000000000000000000000000000000</hash>
            <metaurl mediatype="torrent">https://third-party.example/book.torrent</metaurl>
            <url>https://third-party.example/book.zim</url>
            <url>https://mirror.download.kiwix.org/book.zim</url>
          </file>
        </metalink>"""

        sanitized = downloads.sanitize_metalink_sources(metalink).decode("utf-8")

        self.assertNotIn("third-party.example", sanitized)
        self.assertNotIn("metaurl", sanitized)
        self.assertIn("mirror.download.kiwix.org", sanitized)

    def test_redirect_handler_rejects_private_destination_before_following(self) -> None:
        handler = downloads._SafeRedirectHandler(
            lambda _host, port, **_kwargs: [
                (downloads.socket.AF_INET, downloads.socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))
            ]
        )

        with self.assertRaises(downloads.DownloadValidationError):
            handler.redirect_request(
                Mock(),
                Mock(),
                302,
                "Found",
                {},
                "https://mirror.download.kiwix.org/private",
            )

    def test_retry_space_check_subtracts_existing_allocated_data(self) -> None:
        metalink = b"""<metalink xmlns="urn:ietf:params:xml:ns:metalink">
          <file name="book.zim"><size>1000</size>
            <hash type="sha-256">0000000000000000000000000000000000000000000000000000000000000000</hash>
            <url>https://mirror.download.kiwix.org/book.zim</url>
          </file>
        </metalink>"""
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / "book.zim").write_bytes(b"x" * 900)
            manager = downloads.DownloadManager(
                Mock(),
                disk_usage_fn=lambda _path: SimpleNamespace(free=100),
                host_resolver=public_resolver,
            )
            manager.fetch_catalog = lambda _url: metalink

            _data, _root, required, _info = manager._prepare(
                "https://lb.download.kiwix.org/book.zim.meta4",
                temporary,
                1000,
                allow_existing=True,
            )

            self.assertEqual(required, 1000)


class RecoveryTests(unittest.TestCase):
    def test_retry_submission_keeps_full_metalink_for_future_recovery(self) -> None:
        full_metalink = b"""<metalink xmlns="urn:ietf:params:xml:ns:metalink">
          <file name="book.zimaa"><size>5</size>
            <hash type="sha-256">0000000000000000000000000000000000000000000000000000000000000000</hash>
            <url>https://mirror.download.kiwix.org/book.zimaa</url>
          </file>
          <file name="book.zimab"><size>5</size>
            <hash type="sha-256">1111111111111111111111111111111111111111111111111111111111111111</hash>
            <url>https://mirror.download.kiwix.org/book.zimab</url>
          </file>
        </metalink>"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "book.zimaa"
            first.write_bytes(b"first")
            pending_metalink = downloads.filter_metalink(
                full_metalink, root, {first.resolve()}
            )
            store = Mock()
            client = Mock()
            client.call.return_value = ["pending-gid"]
            manager = downloads.DownloadManager(store, state_dir=root / "state")
            manager._client = client
            manager._process = Mock(poll=Mock(return_value=None))

            manager._submit(
                "book",
                "https://lb.download.kiwix.org/book.meta4",
                pending_metalink,
                root,
                10,
                10,
                {},
                downloads.inspect_metalink(pending_metalink, root),
                all_output_paths=downloads.inspect_metalink(full_metalink, root).paths,
                completed_paths={first.resolve()},
                persistent_metalink=full_metalink,
            )

            state = store.save_download.call_args.args[1]
            self.assertEqual(Path(state["metalink_path"]).read_bytes(), full_metalink)

    def test_recreates_rpc_metalink_when_saved_gid_is_gone(self) -> None:
        metalink = b"""<metalink xmlns="urn:ietf:params:xml:ns:metalink">
          <file name="book.zim"><size>123</size>
            <hash type="sha-256">0000000000000000000000000000000000000000000000000000000000000000</hash>
            <url>https://mirror.download.kiwix.org/book.zim</url>
          </file>
        </metalink>"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            metalink_path = root / "saved.meta4"
            metalink_path.write_bytes(metalink)
            store = Mock()
            client = Mock()

            def rpc(method, _params):
                if method == "aria2.tellStatus":
                    raise downloads.RPCError("missing", code=1)
                return ["new-gid"]

            client.call.side_effect = rpc
            manager = downloads.DownloadManager(store, host_resolver=public_resolver)
            manager._client = client
            process = Mock()
            process.poll.return_value = None
            manager._process = process
            record = {
                "catalog_id": "book",
                "gids": ["old-gid"],
                "destination": str(root),
                "metalink_path": str(metalink_path),
                "status": "paused",
            }

            recovered = manager._recover_record(record)

            self.assertEqual(recovered["gids"], ["new-gid"])
            self.assertEqual(recovered["status"], "queued")
            store.save_download.assert_called_once()

    def test_recovery_does_not_resubmit_a_completed_split_part(self) -> None:
        metalink = b"""<metalink xmlns="urn:ietf:params:xml:ns:metalink">
          <file name="book.zimaa"><size>5</size>
            <hash type="sha-256">0000000000000000000000000000000000000000000000000000000000000000</hash>
            <url>https://mirror.download.kiwix.org/book.zimaa</url>
          </file>
          <file name="book.zimab"><size>5</size>
            <hash type="sha-256">1111111111111111111111111111111111111111111111111111111111111111</hash>
            <url>https://mirror.download.kiwix.org/book.zimab</url>
          </file>
        </metalink>"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "book.zimaa"
            first.write_bytes(b"first")
            metalink_path = root / "saved.meta4"
            metalink_path.write_bytes(metalink)
            record = {
                "catalog_id": "book",
                "gids": ["completed-gid", "missing-gid"],
                "destination": str(root),
                "metalink_path": str(metalink_path),
                "status": "downloading",
                "output_paths": [str(first), str(root / "book.zimab")],
            }
            client = Mock()

            def rpc(method, params):
                if method == "aria2.tellStatus" and params[0] == "completed-gid":
                    return {
                        "gid": "completed-gid",
                        "status": "complete",
                        "totalLength": "5",
                        "completedLength": "5",
                        "files": [{"path": str(first)}],
                    }
                if method == "aria2.tellStatus":
                    raise downloads.RPCError("missing", code=1)
                if method == "aria2.removeDownloadResult":
                    return params[0]
                if method == "aria2.addMetalink":
                    decoded = downloads.base64.b64decode(params[0]).decode("utf-8")
                    self.assertNotIn('name="book.zimaa"', decoded)
                    self.assertIn('name="book.zimab"', decoded)
                    return ["replacement-gid"]
                raise AssertionError(method)

            client.call.side_effect = rpc
            store = Mock()
            manager = downloads.DownloadManager(store, host_resolver=public_resolver)
            manager._client = client
            manager._process = Mock(poll=Mock(return_value=None))

            recovered = manager._recover_record(
                record,
                [{"gid": "completed-gid", "status": "completed", "paths": [str(first)]}],
            )

            self.assertEqual(recovered["gids"], ["replacement-gid"])
            self.assertEqual(recovered["completed_paths"], [str(first)])

    def test_cancel_uses_the_gid_persisted_by_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = {
                "catalog_id": "book",
                "gids": ["new-gid"],
                "destination": temporary,
                "status": "paused",
                "output_paths": [],
            }
            store = Mock()
            store.load_download.side_effect = lambda _catalog_id: dict(state)
            client = Mock()
            client.call.side_effect = [
                {"gid": "new-gid", "status": "paused"},
                "new-gid",
            ]
            manager = downloads.DownloadManager(store)
            manager._client = client
            process = Mock()
            process.poll.return_value = None
            manager._process = process

            manager.cancel("book", delete_files=False)

            self.assertEqual(
                client.call.call_args_list[-1].args,
                ("aria2.forceRemove", ["new-gid"]),
            )
            store.delete_download_state.assert_called_once_with("book")

    def test_cancel_stops_every_gid_before_deleting_owned_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "book.zim"
            output.write_bytes(b"partial")
            record = {
                "catalog_id": "book",
                "gids": ["failed-gid", "active-gid"],
                "destination": temporary,
                "status": "error",
                "output_paths": [str(output)],
            }
            store = Mock()
            store.load_download.return_value = record
            client = Mock()

            def rpc(method, params):
                if method == "aria2.tellStatus":
                    return {
                        "gid": params[0],
                        "status": "error" if params[0] == "failed-gid" else "active",
                        "files": [{"path": str(output)}],
                    }
                return params[0]

            client.call.side_effect = rpc
            manager = downloads.DownloadManager(store)
            manager._client = client
            manager._process = Mock(poll=Mock(return_value=None))

            manager.cancel("book")

            self.assertFalse(output.exists())
            self.assertIn(
                (("aria2.removeDownloadResult", ["failed-gid"]), {}),
                client.call.call_args_list,
            )
            self.assertIn(
                (("aria2.forceRemove", ["active-gid"]), {}),
                client.call.call_args_list,
            )
            store.delete_download_state.assert_called_once_with("book")

    def test_cancel_preserves_files_and_state_when_a_gid_cannot_be_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "book.zim"
            output.write_bytes(b"partial")
            record = {
                "catalog_id": "book",
                "gids": ["active-gid"],
                "destination": temporary,
                "status": "error",
                "output_paths": [str(output)],
            }
            store = Mock()
            store.load_download.return_value = record
            client = Mock()

            def rpc(method, params):
                if method == "aria2.tellStatus":
                    return {
                        "gid": params[0],
                        "status": "active",
                        "files": [{"path": str(output)}],
                    }
                raise downloads.RPCError("could not stop")

            client.call.side_effect = rpc
            manager = downloads.DownloadManager(store)
            manager._client = client
            manager._process = Mock(poll=Mock(return_value=None))

            with self.assertRaises(downloads.RPCError):
                manager.cancel("book")

            self.assertTrue(output.exists())
            store.delete_download_state.assert_not_called()


if __name__ == "__main__":
    unittest.main()
