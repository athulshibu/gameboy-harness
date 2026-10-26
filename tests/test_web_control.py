"""Verify the local panel API and process lifecycle without a real ROM."""

import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend.emulator import SkyEmuError
from backend.emulator.process import ensure_skyemu
from tests.web_control import ControlServer, main, start_skyemu, stop_skyemu


class WebControlTests(unittest.TestCase):
    def setUp(self):
        self.emulator = Mock()
        self.emulator.get_frame.return_value = b"\x89PNG\r\n\x1a\nfixture"
        self.checkpoint = Path("data/sessions/demo/web-checkpoint.png")
        self.server = ControlServer(("127.0.0.1", 0), self.emulator, Path("demo.gba"), self.checkpoint)
        self.server.RequestHandlerClass.log_message = lambda *args: None
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, data=None, headers=None):
        options = {"Content-Type": "application/json", "X-Control-Panel": "1"}
        options.update(headers or {})
        request = Request(self.url + path, data=None if data is None else json.dumps(data).encode(), headers=options)
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, response.read()
        except HTTPError as exc:
            with exc:
                return exc.code, exc.read()

    def test_page_screen_and_status(self):
        status, page = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Python Control Panel", page)
        self.assertEqual(self.request("/screen"), (200, self.emulator.get_frame.return_value))
        state = json.loads(self.request("/api/status")[1])
        self.assertEqual(state["rom"], "demo.gba")
        self.assertTrue(state["playing"])
        self.assertEqual(state["last_command"], "None")
        self.assertEqual(self.request("/control.js")[0], 200)

    def test_web_input_uses_shared_frames_per_click_default(self):
        with patch("tests.web_control.FRAMES_PER_CLICK", 12):
            with ControlServer(("127.0.0.1", 0), self.emulator, Path("demo.gba"), self.checkpoint) as server:
                self.assertIn(b'value="12"', server.html)
                self.assertNotIn(b"{{FRAMES_PER_CLICK}}", server.html)

    def test_button_action_and_idle_frames(self):
        status, body = self.request("/api/action", {"buttons": ["UP"], "frames": 30})
        self.assertEqual(status, 200)
        self.server.playback._tick()
        self.emulator.set_buttons.assert_called_once_with(("Up",))
        self.emulator.step.assert_called_with(2)
        self.assertEqual(json.loads(body)["last_command"], "UP · 30 frames")
        self.request("/api/action", {"buttons": [], "frames": 2})
        self.assertEqual(self.server.playback.status()["queued_actions"], 1)

    def test_invalid_actions_never_reach_emulator(self):
        for payload in [[], {"buttons": "UP", "frames": 30}, {"buttons": ["X"], "frames": 1},
                        {"buttons": ["A"], "frames": True}, {"buttons": [], "frames": 3601}]:
            with self.subTest(payload=payload):
                self.assertEqual(self.request("/api/action", payload)[0], 400)
        self.emulator.press_buttons.assert_not_called()

    def test_save_and_restore_use_fixed_rom_checkpoint(self):
        self.assertEqual(self.request("/api/save", {})[0], 200)
        self.assertEqual(self.request("/api/load", {})[0], 200)
        self.emulator.save_state.assert_called_once_with(self.checkpoint)
        self.emulator.load_state.assert_called_once_with(self.checkpoint)

    def test_errors_do_not_change_last_successful_command(self):
        self.server.playback.error = "Disconnected"
        self.assertEqual(self.request("/api/action", {"buttons": ["A"], "frames": 1})[0], 502)
        self.assertEqual(self.server.last_command, "None")
        self.emulator.load_state.side_effect = FileNotFoundError
        status, body = self.request("/api/load", {})
        self.assertEqual(status, 400)
        self.assertIn("Save a state first", json.loads(body)["error"])

    def test_foreign_host_and_missing_command_header_are_rejected(self):
        self.assertEqual(self.request("/", headers={"Host": "foreign.example"})[0], 403)
        status, _ = self.request("/api/action", {"buttons": ["A"], "frames": 1}, {"X-Control-Panel": ""})
        self.assertEqual(status, 403)
        self.emulator.press_buttons.assert_not_called()

    def test_play_pause_and_speed_controls(self):
        self.assertEqual(self.request("/api/playback", {"playing": False})[0], 200)
        self.assertFalse(self.server.playback.playing)
        for speed in (2, 4, 1):
            self.assertEqual(self.request("/api/speed", {"speed": speed})[0], 200)
            self.assertEqual(self.server.playback.speed, speed)
        self.assertEqual(self.request("/api/speed", {"speed": 3})[0], 400)
        self.assertEqual(self.request("/api/playback", {"playing": "true"})[0], 400)

    def test_stream_sends_multiple_frames_without_extra_game_clocks(self):
        self.server.playback.get_frame()

        def read_frame(response):
            self.assertEqual(response.readline(), b"--frame\r\n")
            self.assertEqual(response.readline(), b"Content-Type: image/png\r\n")
            length = int(response.readline().split(b":")[1])
            self.assertEqual(response.readline(), b"\r\n")
            data = response.read(length)
            self.assertEqual(response.read(2), b"\r\n")
            return data

        with urlopen(self.url + "/stream", timeout=2) as first, \
                urlopen(self.url + "/stream", timeout=2) as second:
            self.assertIn("multipart/x-mixed-replace", first.headers["Content-Type"])
            self.assertEqual(read_frame(first), self.emulator.get_frame.return_value)
            read_frame(second)
            self.emulator.step.assert_not_called()
            self.emulator.get_frame.return_value = b"updated PNG"
            self.server.playback._tick()
            self.assertEqual(read_frame(first), b"updated PNG")
            self.assertEqual(read_frame(second), b"updated PNG")
        self.emulator.step.assert_called_once_with(2)


class ProcessTests(unittest.TestCase):
    @patch("backend.emulator.process.start_skyemu")
    @patch("backend.emulator.process.SkyEmu")
    def test_desktop_auto_starts_when_no_server_is_running(self, adapter, start):
        adapter.return_value.check_connection.side_effect = SkyEmuError("Not running")
        with TemporaryDirectory() as directory:
            executable = Path(directory) / "SkyEmu.exe"
            executable.touch()
            process = ensure_skyemu(executable, "demo.gba", 8081)
            self.assertIs(process, start.return_value)
            start.assert_called_once_with(executable, Path("demo.gba").resolve(), 8081)

    @patch("backend.emulator.process.start_skyemu")
    @patch("backend.emulator.process.SkyEmu")
    def test_existing_instance_is_not_owned(self, adapter, start):
        self.assertIsNone(ensure_skyemu("SkyEmu.exe", "demo.gba", 8081))
        start.assert_not_called()

    @patch("backend.emulator.process.port_available")
    @patch("backend.emulator.process.build_opener")
    @patch("backend.emulator.process.subprocess.Popen")
    def test_launch_preserves_paths_with_spaces(self, popen, opener, available):
        process = popen.return_value
        process.poll.return_value = None
        opener.return_value.open.return_value.__enter__.return_value.read.return_value = b"pong\x00"
        executable, rom = Path("C:/tools/Sky Emu.exe"), Path("C:/roms/Pokemon Emerald.gba")
        result = start_skyemu(executable, rom, 8080)
        self.assertIs(result, process)
        self.assertEqual(popen.call_args.args[0], [str(executable), "http_server", "8080", str(rom)])
        available.assert_called_once_with(8080)

    @patch("backend.emulator.process.stop_skyemu")
    @patch("backend.emulator.process.port_available")
    @patch("backend.emulator.process.subprocess.Popen")
    def test_startup_timeout_cleans_up_owned_process(self, popen, available, stop):
        with self.assertRaisesRegex(RuntimeError, "did not start"):
            start_skyemu(Path("SkyEmu.exe"), Path("demo.gba"), 8080, timeout=0)
        stop.assert_called_once_with(popen.return_value)

    def test_shutdown_escalates_when_process_will_not_exit(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("SkyEmu", 5), None]
        stop_skyemu(process)
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()


class LauncherTests(unittest.TestCase):
    def launch(self, attach=False):
        with TemporaryDirectory() as directory:
            rom = Path(directory) / "demo.gba"
            executable = Path(directory) / "SkyEmu.exe"
            rom.write_bytes(b"fixture")
            executable.write_bytes(b"fixture")
            argv = ["web_control.py", str(rom), "--skyemu", str(executable)]
            if attach:
                argv.append("--attach")
            with patch("sys.argv", argv), patch("tests.web_control.SkyEmu") as adapter, \
                    patch("tests.web_control.ControlServer") as server, \
                    patch("tests.web_control.start_skyemu") as start, \
                    patch("tests.web_control.stop_skyemu") as stop, patch("builtins.print"):
                server.return_value.__enter__.return_value.serve_forever.side_effect = KeyboardInterrupt
                main()
                adapter.return_value.check_connection.assert_called_once_with()
                adapter.return_value.load_rom.assert_called_once_with(str(rom))
                adapter.return_value.press_buttons.assert_called_once_with([], frames=300)
                adapter.return_value.get_frame.assert_called_once_with()
                if attach:
                    start.assert_not_called()
                    stop.assert_not_called()
                else:
                    start.assert_called_once_with(executable, rom, 8080)
                    stop.assert_called_once_with(start.return_value)

    def test_owned_emulator_boots_before_serving_and_stops_on_exit(self):
        self.launch()

    def test_attach_does_not_start_or_stop_user_emulator(self):
        self.launch(attach=True)


if __name__ == "__main__":
    unittest.main()
