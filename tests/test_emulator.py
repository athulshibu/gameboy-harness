"""Exercise the SkyEmu HTTP contract without requiring a copyrighted ROM."""

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from backend.emulator import Emulator, SkyEmu, SkyEmuError


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZ1sAAAAASUVORK5CYII="
)


class SkyEmuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                request = urlsplit(self.path)
                params = {key: values[0] for key, values in parse_qs(request.query).items()}
                self.server.requests.append((request.path, params))
                status, body = self.server.replies.get(
                    request.path, (200, PNG if request.path == "/screen" else b"ok\x00")
                )
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.server.requests = []
        self.server.replies = {}
        self.emulator = SkyEmu(port=self.server.server_port, timeout=2)
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_rom_path_is_encoded_and_loaded_paused(self):
        rom = self.root / "game & demo 한글.gba"
        rom.write_bytes(b"test fixture")
        self.emulator.load_rom(str(rom))
        self.assertIsInstance(self.emulator, Emulator)
        self.assertEqual(self.server.requests[0], ("/load_rom", {"path": str(rom), "pause": "1"}))
        self.assert_released()

    def assert_released(self):
        endpoint, inputs = self.server.requests[-1]
        self.assertEqual(endpoint, "/input")
        self.assertEqual(inputs, dict.fromkeys(SkyEmu.BUTTONS, "0"))

    def test_frame_returns_png_without_stepping(self):
        self.assertEqual(self.emulator.get_frame(), PNG)
        self.assertEqual(self.server.requests, [("/screen", {"format": "png"})])

    def test_connection_check_requires_skyemu_ping_response(self):
        self.server.replies["/ping"] = (200, b"pong\x00")
        self.emulator.check_connection()
        self.server.replies["/ping"] = (200, b"some other server")
        with self.assertRaisesRegex(SkyEmuError, "Expected SkyEmu"):
            self.emulator.check_connection()

    def test_frame_error_is_not_returned_as_image(self):
        self.server.replies["/screen"] = (200, b"Failed (no ROM loaded)\x00")
        with self.assertRaises(SkyEmuError):
            self.emulator.get_frame()

    def test_simultaneous_buttons_step_then_release(self):
        self.emulator.press_buttons(["a", "RIGHT"], 60)
        inputs = self.server.requests[0][1]
        self.assertEqual({name for name, value in inputs.items() if value == "1"}, {"A", "Right"})
        self.assertEqual(self.server.requests[1], ("/step", {"frames": "60"}))
        self.assert_released()

    def test_single_string_and_idle_frames(self):
        self.emulator.press_buttons("start", 1)
        self.assertEqual(self.server.requests[0][1]["Start"], "1")
        self.server.requests.clear()
        self.emulator.press_buttons([], 5)
        self.assertEqual(set(self.server.requests[0][1].values()), {"0"})
        self.assertEqual(self.server.requests[1], ("/step", {"frames": "5"}))

    def test_invalid_actions_do_not_send_requests(self):
        for buttons, frames in [(["X"], 1), ([None], 1), (["A"], 0), (["A"], -1),
                                (["A"], True), (["A"], 1.5), (["A"], 2**31)]:
            with self.subTest(buttons=buttons, frames=frames), self.assertRaises(ValueError):
                self.emulator.press_buttons(buttons, frames)
        self.assertEqual(self.server.requests, [])

    def test_step_failure_still_releases_buttons(self):
        self.server.replies["/step"] = (200, b"failed\x00")
        with self.assertRaisesRegex(SkyEmuError, "/step"):
            self.emulator.press_buttons("A", 4)
        self.assert_released()

    def test_http_error_still_releases_buttons(self):
        self.server.replies["/step"] = (500, b"error")
        with self.assertRaisesRegex(SkyEmuError, "request failed"):
            self.emulator.press_buttons("B", 4)
        self.assert_released()
        self.assertEqual(sum(endpoint == "/step" for endpoint, _ in self.server.requests), 1)

    def test_timeout_is_wrapped_without_retry(self):
        with patch.object(self.emulator._http, "open", side_effect=TimeoutError), \
                self.assertRaisesRegex(SkyEmuError, "not retried"):
            self.emulator.get_frame()

    def test_save_and_load_state_paths(self):
        state = self.root / "nested" / "checkpoint.png"
        self.emulator.save_state(state)
        self.assertTrue(state.parent.is_dir())
        self.assertEqual(self.server.requests[-1], ("/save", {"path": str(state)}))
        # Stand-in for the file that SkyEmu writes on the local filesystem.
        state.write_bytes(PNG)
        self.emulator.load_state(state)
        self.assertEqual(self.server.requests[-1], ("/load", {"path": str(state)}))
        self.assertEqual(self.server.requests[-2], ("/input", dict.fromkeys(SkyEmu.BUTTONS, "0")))

    def test_missing_files_and_invalid_state_extension(self):
        with self.assertRaises(FileNotFoundError):
            self.emulator.load_rom(str(self.root / "missing.gba"))
        with self.assertRaises(FileNotFoundError):
            self.emulator.load_state(self.root / "missing.png")
        with self.assertRaises(ValueError):
            self.emulator.save_state(self.root / "state.sav")
        self.assertEqual(self.server.requests, [])

    def test_save_and_load_rejections(self):
        state = self.root / "checkpoint.png"
        state.write_bytes(PNG)
        for endpoint, operation in [("/save", self.emulator.save_state), ("/load", self.emulator.load_state)]:
            self.server.replies[endpoint] = (200, b"failed\x00")
            with self.subTest(endpoint=endpoint), self.assertRaises(SkyEmuError):
                operation(state)


if __name__ == "__main__":
    unittest.main()
