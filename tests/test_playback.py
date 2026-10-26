"""Verify the emulation clock independently of connected viewers."""

from threading import Event
import unittest
from unittest.mock import Mock

from backend.emulator.playback import Playback


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.emulator = Mock()
        self.emulator.get_frame.return_value = b"PNG"
        self.player = Playback(self.emulator)

    def test_speed_scales_emulated_frames_without_changing_capture_rate(self):
        for speed in (1, 2, 4):
            self.player.set_speed(speed)
            self.player._tick()
            self.emulator.step.assert_called_with(2 * speed)
        self.assertEqual(self.emulator.get_frame.call_count, 3)

    def test_timed_input_is_exact_at_every_speed(self):
        for speed in (1, 2, 4):
            with self.subTest(speed=speed):
                emulator = Mock()
                player = Playback(emulator, playing=False)
                player.set_speed(speed)
                player.press_buttons(["UP"], frames=11)
                for _ in range(12):
                    player._tick()
                self.assertEqual(sum(call.args[0] for call in emulator.step.call_args_list), 11)
                emulator.set_buttons.assert_called_once_with(("Up",))
                emulator.release_buttons.assert_called_once_with()

    def test_long_input_produces_intermediate_images(self):
        self.player.press_buttons("A", frames=30)
        self.player._tick()
        self.player._tick()
        self.assertEqual(self.player.snapshot[0], 2)
        self.assertEqual(self.player.status()["held_frames"], 26)

    def test_reading_snapshot_does_not_step_game(self):
        self.player._tick()
        self.emulator.reset_mock()
        for _ in range(100):
            self.assertEqual(self.player.snapshot[1], b"PNG")
        self.emulator.step.assert_not_called()
        self.emulator.get_frame.assert_not_called()

    def test_pause_cancels_actions_and_releases_inputs(self):
        self.player.press_buttons("B", 60)
        self.player._tick()
        self.player.press_buttons("A", 30)
        self.player.set_playing(False)
        self.emulator.reset_mock()
        self.player._tick()
        self.emulator.step.assert_not_called()
        self.assertEqual(self.player.status()["queued_actions"], 0)
        self.assertEqual(self.player.status()["held_frames"], 0)

    def test_restore_clears_pending_input_and_refreshes_screen(self):
        self.player.press_buttons("A", 60)
        self.player.load_state("checkpoint.png")
        self.emulator.load_state.assert_called_once_with("checkpoint.png")
        self.assertEqual(self.player.status()["queued_actions"], 0)
        self.assertEqual(self.player.snapshot[0], 1)

    def test_invalid_speed_has_no_effect(self):
        for speed in (0, 3, 8, True, 2.0, "2"):
            with self.assertRaises(ValueError):
                self.player.set_speed(speed)
        self.assertEqual(self.player.speed, 1)

    def test_background_failure_pauses_and_exposes_error(self):
        failed = Event()

        def fail(frames):
            failed.set()
            raise RuntimeError("Disconnected")

        self.emulator.step.side_effect = fail
        self.player.start()
        self.assertTrue(failed.wait(timeout=2))
        self.player.close()
        self.assertFalse(self.player.playing)
        self.assertEqual(self.player.error, "Disconnected")
        self.emulator.release_buttons.assert_called()
