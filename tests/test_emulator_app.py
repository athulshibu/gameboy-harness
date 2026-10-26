"""Test controller scheduling without opening a desktop window."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
import unittest
from unittest.mock import Mock, patch

from scripts.emulator_app import EmulatorApp
from backend.emulator.constants import FRAMES_PER_CLICK


class EmulatorAppTests(unittest.TestCase):
    def setUp(self):
        self.app = EmulatorApp.__new__(EmulatorApp)
        self.app.root = Mock()
        self.app.emulator = Mock()
        self.app.emulator.port = 8081
        self.app._session = Mock()
        self.app._session.press_buttons.return_value = None
        self.app._process = None
        self.app._shutdown_future = None
        self.app.executable = "SkyEmu.exe"
        self.app.attach = False
        self.app._executor = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.app._executor.shutdown)
        self.app._pending = None
        self.app._closing = False
        self.app._rom_loaded = True
        self.app._status = Mock()
        self.app._frames = Mock()
        self.app._frames.get.return_value = str(FRAMES_PER_CLICK)
        self.app._speed = Mock()
        self.app._speed.get.return_value = "1"
        self.app._speed_combo = Mock()
        self.button = Mock()
        self.app._controls = [(self.button, True)]

    def finish(self):
        # Wait for the worker only in tests; production uses Tk's event loop.
        try:
            self.app._pending.result(timeout=2)
        except RuntimeError:
            pass
        callback = self.app.root.after.call_args.args[1]
        callback()

    def test_action_queues_duration_without_blocking_video(self):
        self.app.press(["Up"])
        self.finish()
        self.app._session.press_buttons.assert_called_once_with(["Up"], frames=FRAMES_PER_CLICK)
        self.app.emulator.get_frame.assert_not_called()
        self.button.configure.assert_called_with(state="normal")

    def test_invalid_duration_does_not_send_input(self):
        for value in ["0", "-1", "1.5", "abc", "3601"]:
            self.app._frames.get.return_value = value
            with patch("scripts.emulator_app.messagebox.showerror") as error:
                self.app.press(["A"])
                error.assert_called_once()
        self.app._session.press_buttons.assert_not_called()

    def test_rom_load_checks_connection_and_allows_game_to_boot(self):
        self.app.boot_frames = 300
        self.app._rom_name = Mock()
        self.app._screen = Mock()
        self.app.emulator.get_frame.return_value = b"PNG fixture"
        with patch("scripts.emulator_app.filedialog.askopenfilename", return_value="demo.gba"), \
                patch("scripts.emulator_app.tk.PhotoImage"), \
                patch("scripts.emulator_app.ensure_skyemu") as ensure, \
                patch("scripts.emulator_app.Playback") as playback:
            playback.return_value.snapshot = (1, b"PNG fixture")
            self.app.load_rom()
            self.finish()
            ensure.assert_called_once_with("SkyEmu.exe", "demo.gba", 8081, attach=False)
            self.assertIs(self.app._process, ensure.return_value)
            playback.return_value.start.assert_called_once_with()
        self.app.emulator.load_rom.assert_called_once_with("demo.gba")
        self.app.emulator.press_buttons.assert_called_once_with([], frames=300)
        self.assertTrue(self.app._rom_loaded)

    def test_failure_is_reported_and_controls_reenabled(self):
        self.app._session.get_frame.side_effect = RuntimeError("Server unavailable")
        with patch("scripts.emulator_app.messagebox.showerror") as error:
            self.app.capture()
            self.finish()
            self.assertEqual(error.call_args.args[1], "Server unavailable")
        self.assertIsNone(self.app._pending)
        self.button.configure.assert_called_with(state="normal")

    def test_close_waits_for_pending_action_without_duplicate_input(self):
        gate = Event()
        self.addCleanup(gate.set)

        def operation():
            gate.wait(timeout=2)

        self.app._submit(operation, "Done")
        ignored = Mock()
        self.app._submit(ignored, "Must not execute")
        self.app.close()
        self.app.root.destroy.assert_not_called()
        self.button.configure.assert_called_with(state="disabled")
        gate.set()
        self.finish()
        self.app._shutdown_future.result(timeout=2)
        self.app._finish_close()
        self.app.root.destroy.assert_called_once_with()
        ignored.assert_not_called()

    def test_preview_renders_new_frames_even_without_button_clicks(self):
        self.app._session.snapshot = (2, b"PNG fixture")
        self.app._session.error = None
        self.app._preview_sequence = 1
        self.app._playback_button = Mock()
        self.app._screen = Mock()
        with patch("scripts.emulator_app.tk.PhotoImage") as image:
            self.app._refresh_preview()
            self.app._refresh_preview()  # Same sequence must not be rendered again.
        image.assert_called_once_with(master=self.app.root, data=b"PNG fixture", format="png")

    def test_shutdown_stops_owned_process_only(self):
        with patch("scripts.emulator_app.stop_skyemu") as stop:
            self.app._shutdown()
            stop.assert_not_called()
            self.app._process = Mock()
            self.app._shutdown()
            stop.assert_called_once_with(self.app._process)

    def test_speed_selection_updates_playback(self):
        self.app._session.set_speed.return_value = None
        self.app._speed.get.return_value = "4"
        self.app.change_speed()
        self.finish()
        self.app._session.set_speed.assert_called_once_with(4)


if __name__ == "__main__":
    unittest.main()
