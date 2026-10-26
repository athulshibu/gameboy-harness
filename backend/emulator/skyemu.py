"""SkyEmu adapter using its HTTP Control Server (Python standard library only)."""

from __future__ import annotations

from collections.abc import Iterable
from http.client import HTTPException
import logging
import math
from pathlib import Path
from threading import RLock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener

from .base import Emulator


class SkyEmuError(RuntimeError):
    """SkyEmu could not be reached or rejected a command."""


class SkyEmu(Emulator):
    """Connect to a local SkyEmu instance with its HTTP server enabled.

    Start SkyEmu separately, enable the HTTP Control Server on ``port``, and
    disable Hardcore Mode. Call ``load_rom`` before playing. This adapter uses
    paused frame stepping; do not resume gameplay or send inputs from another
    client while it is controlling the emulator. Paths refer to local files.
    """

    BUTTONS = ("A", "B", "Up", "Down", "Left", "Right", "L", "R", "Start", "Select")

    def __init__(self, port: int = 8080, timeout: float = 30.0):
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("port must be an integer between 1 and 65535")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        self.base_url = f"http://127.0.0.1:{port}"
        self.port = port
        self.timeout = timeout
        # Local emulator traffic must not be routed through an environment proxy.
        self._http = build_opener(ProxyHandler({}))
        self._lock = RLock()

    def _request(self, endpoint: str, **params: str | int) -> bytes:
        url = f"{self.base_url}/{endpoint}"
        if params:
            url += "?" + urlencode(params)
        try:
            with self._http.open(url, timeout=self.timeout) as response:
                return response.read()
        except (URLError, OSError, HTTPException) as exc:
            if isinstance(exc, HTTPError):
                exc.close()
            raise SkyEmuError(
                f"SkyEmu /{endpoint} request failed at {self.base_url}. "
                f"Connection details: {exc}. "
                "Keep SkyEmu running with its HTTP Control Server enabled on this port. "
                "Stopping tests/web_control.py also stops the SkyEmu process it launched. "
                "A timed-out command may have executed; it was not retried."
            ) from exc

    def check_connection(self) -> None:
        """Check that a running SkyEmu HTTP server is reachable."""
        with self._lock:
            result = self._request("ping").strip(b"\x00 \t\r\n")
            if result != b"pong":
                raise SkyEmuError(f"Expected SkyEmu at {self.base_url}, received {result[:100]!r}")

    def _command(self, endpoint: str, **params: str | int) -> None:
        # v5 includes the C string's terminating NUL in command responses.
        result = self._request(endpoint, **params).decode("utf-8", errors="replace").strip("\x00 \t\r\n")
        if result.lower() != "ok":
            raise SkyEmuError(f"SkyEmu /{endpoint} failed: {result[:300]!r}")

    @staticmethod
    def _existing_file(path: str | Path) -> Path:
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"File does not exist: {resolved}")
        return resolved

    def load_rom(self, path: str) -> None:
        """Load a local ROM and pause it for frame-controlled gameplay."""
        rom = self._existing_file(path)
        with self._lock:
            self._command("load_rom", path=str(rom), pause=1)
            self.release_buttons()

    def get_frame(self) -> bytes:
        """Return the current screen as PNG bytes, without advancing gameplay."""
        with self._lock:
            frame = self._request("screen", format="png")
            if not frame.startswith(b"\x89PNG\r\n\x1a\n"):
                raise SkyEmuError("SkyEmu did not return a PNG frame. Load a ROM first.")
            return frame

    def press_buttons(self, buttons: str | Iterable[str], frames: int) -> None:
        """Hold a button or simultaneous buttons for frames, then release all.

        Names are case-insensitive. An empty iterable advances frames with no
        input. Frames must be a positive integer representable by SkyEmu.
        """
        if isinstance(frames, bool) or not isinstance(frames, int) or not 1 <= frames <= 2**31 - 1:
            raise ValueError("frames must be an integer between 1 and 2147483647")
        if isinstance(buttons, str):
            buttons = [buttons]
        names = {name.casefold(): name for name in self.BUTTONS}
        selected = set()
        for button in buttons:
            if not isinstance(button, str) or button.casefold() not in names:
                raise ValueError(f"Unknown GBA button: {button!r}. Choose from {self.BUTTONS}")
            selected.add(names[button.casefold()])
        with self._lock:
            try:
                self._command("input", **{name: int(name in selected) for name in self.BUTTONS})
                self._command("step", frames=frames)
            except BaseException:
                # Preserve the original failure even if cleanup also fails.
                try:
                    self.release_buttons()
                except SkyEmuError:
                    logging.getLogger(__name__).exception("Could not release SkyEmu buttons")
                raise
            else:
                self.release_buttons()

    def release_buttons(self) -> None:
        """Clear every GBA button on the HTTP controller without stepping."""
        with self._lock:
            self._command("input", **dict.fromkeys(self.BUTTONS, 0))

    @classmethod
    def normalize_buttons(cls, buttons: str | Iterable[str]) -> tuple[str, ...]:
        if isinstance(buttons, str):
            buttons = [buttons]
        names = {name.casefold(): name for name in cls.BUTTONS}
        selected = set()
        for button in buttons:
            if not isinstance(button, str) or button.casefold() not in names:
                raise ValueError(f"Unknown GBA button: {button!r}")
            selected.add(names[button.casefold()])
        return tuple(name for name in cls.BUTTONS if name in selected)

    def set_buttons(self, buttons: str | Iterable[str]) -> None:
        """Set persistent inputs without advancing emulation."""
        selected = self.normalize_buttons(buttons)
        with self._lock:
            self._command("input", **{name: int(name in selected) for name in self.BUTTONS})

    def step(self, frames: int = 1) -> None:
        """Advance a bounded batch while preserving the current inputs."""
        if type(frames) is not int or not 1 <= frames <= 2**31 - 1:
            raise ValueError("frames must be an integer between 1 and 2147483647")
        with self._lock:
            self._command("step", frames=frames)

    def save_state(self, path: str | Path) -> None:
        """Save a SkyEmu PNG state, creating missing parent directories."""
        destination = Path(path).expanduser().resolve()
        if destination.suffix.lower() != ".png":
            raise ValueError("SkyEmu save-state paths must end in .png")
        with self._lock:
            destination.parent.mkdir(parents=True, exist_ok=True)
            self._command("save", path=str(destination))

    def load_state(self, path: str | Path) -> None:
        """Restore a local SkyEmu state and clear HTTP controller inputs."""
        source = self._existing_file(path)
        with self._lock:
            self.release_buttons()
            self._command("load", path=str(source))
