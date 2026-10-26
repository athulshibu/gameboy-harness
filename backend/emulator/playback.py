"""A single real-time emulation clock shared by all viewers of a session."""

from collections import deque
from threading import Event, RLock, Thread
import time

from .skyemu import SkyEmu
from .constants import FRAMES_PER_UPDATE, FRAME_UPDATE_INTERVAL_SECONDS


class Playback:
    """Advance gameplay and publish images at the shared timing constants.

    Timed inputs are processed in small frame batches so long button presses
    remain visible. Viewers read cached images and never advance the game.
    """

    def __init__(self, emulator, playing=True):
        self.emulator = emulator
        self.playing = playing
        self.speed = 1
        self.emulated_frames = 0
        self.error = None
        self._lock = RLock()
        self._stop = Event()
        self._thread = None
        self._queue = deque()
        self._remaining = 0
        self._snapshot = (0, b"")

    @property
    def snapshot(self):
        # Publish an immutable tuple atomically; Tk must never wait on network I/O.
        return self._snapshot

    def status(self):
        with self._lock:
            return {"playing": self.playing, "speed": self.speed, "error": self.error,
                    "frame_sequence": self._snapshot[0], "emulated_frames": self.emulated_frames,
                    "queued_actions": len(self._queue), "held_frames": self._remaining}

    def _capture(self):
        self._snapshot = (self._snapshot[0] + 1, self.emulator.get_frame())
        return self._snapshot[1]

    def start(self):
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("Playback already started")
            self._capture()
            self._thread = Thread(target=self._run, name="emulator-playback", daemon=True)
            self._thread.start()

    def press_buttons(self, buttons, frames):
        selected = SkyEmu.normalize_buttons(buttons)
        if type(frames) is not int or not 1 <= frames <= 3600:
            raise ValueError("frames must be a whole number from 1 to 3600")
        with self._lock:
            if self.error:
                raise RuntimeError(self.error)
            if len(self._queue) >= 32:
                raise ValueError("Input queue is full; wait for the current actions to finish")
            self._queue.append((selected, frames))

    def _clear_input(self):
        self._queue.clear()
        self._remaining = 0
        self.emulator.release_buttons()

    def set_playing(self, playing):
        if type(playing) is not bool:
            raise ValueError("playing must be true or false")
        with self._lock:
            self.playing = playing
            self._clear_input()
            self.error = None

    def release_buttons(self):
        with self._lock:
            self._clear_input()

    def set_speed(self, speed):
        if type(speed) is not int or speed not in (1, 2, 4):
            raise ValueError("speed must be 1, 2, or 4")
        with self._lock:
            self.speed = speed

    def get_frame(self):
        with self._lock:
            return self._capture()

    def save_state(self, path):
        with self._lock:
            self.emulator.save_state(path)

    def load_state(self, path):
        with self._lock:
            self._clear_input()
            self.emulator.load_state(path)
            self._capture()

    def _tick(self):
        with self._lock:
            if self.error:
                return
            if self._remaining == 0 and self._queue:
                buttons, self._remaining = self._queue.popleft()
                self.emulator.set_buttons(buttons)
            if not self.playing and self._remaining == 0:
                return
            batch = FRAMES_PER_UPDATE * self.speed
            frames = min(batch, self._remaining) if self._remaining else batch
            self.emulator.step(frames)
            self.emulated_frames += frames
            if self._remaining:
                self._remaining -= frames
                if self._remaining == 0:
                    self.emulator.release_buttons()
            self._capture()

    def _run(self):
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._tick()
            except Exception as exc:
                with self._lock:
                    self.error = str(exc)
                    self.playing = False
                    try:
                        self._clear_input()
                    except Exception:
                        pass  # Keep the original error for the UI; don't retry actions.
            self._stop.wait(max(0, FRAME_UPDATE_INTERVAL_SECONDS - (time.monotonic() - started)))

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        with self._lock:
            try:
                self._clear_input()
            except Exception:
                pass
