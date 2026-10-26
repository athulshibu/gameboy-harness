"""Small Tk desktop controller: python -m scripts.emulator_app."""

from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from backend.emulator import SkyEmu
from backend.emulator.constants import FRAMES_PER_CLICK, PREVIEW_INTERVAL_MS
from backend.emulator.playback import Playback
from backend.emulator.process import DEFAULT_EXECUTABLE, ensure_skyemu, stop_skyemu


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class EmulatorApp:
    """Keep Tk on the main thread and emulator requests on a single worker."""

    def __init__(self, root: tk.Tk, emulator: SkyEmu, boot_frames: int = 300,
                 executable=DEFAULT_EXECUTABLE, attach=False):
        self.root = root
        self.emulator = emulator
        self.boot_frames = boot_frames
        self.executable = executable
        self.attach = attach
        self._process = None
        self._session = None
        self._preview_sequence = -1
        self._shutdown_future = None
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._pending: Future | None = None
        self._closing = False
        self._image: tk.PhotoImage | None = None
        self._rom_loaded = False
        self._controls: list[tuple[ttk.Button, bool]] = []
        self._status = tk.StringVar(value="Load a ROM to begin. SkyEmu will start automatically.")
        self._frames = tk.StringVar(value=str(FRAMES_PER_CLICK))
        self._speed = tk.StringVar(value="1")
        self._rom_name = tk.StringVar(value="No ROM loaded")

        root.title("GBA Controller — SkyEmu")
        root.minsize(620, 620)
        root.protocol("WM_DELETE_WINDOW", self.close)
        outer = ttk.Frame(root, padding=16)
        outer.pack(fill="both", expand=True)

        toolbar = ttk.Frame(outer)
        toolbar.pack(fill="x")
        self._button(toolbar, "Load ROM…", self.load_rom, needs_rom=False).pack(side="left")
        self._button(toolbar, "Capture screen", self.capture).pack(side="left", padx=8)
        self._playback_button = self._button(toolbar, "Pause", self.toggle_playback)
        self._playback_button.pack(side="left")
        ttk.Label(outer, textvariable=self._rom_name).pack(anchor="w", pady=8)

        self._screen = tk.Label(
            outer, text="Game screen", background="#111827", foreground="white",
            anchor="center",
        )
        self._screen.pack(fill="both", expand=True, pady=(0, 12))

        duration = ttk.Frame(outer)
        duration.pack(fill="x", pady=(0, 12))
        ttk.Label(duration, text="Frames per press:").pack(side="left")
        ttk.Spinbox(duration, from_=1, to=3600, width=8, textvariable=self._frames).pack(side="left", padx=8)
        ttk.Label(duration, text="Duration in game frames").pack(side="left")
        ttk.Label(duration, text="Speed:").pack(side="left", padx=(12, 0))
        self._speed_combo = ttk.Combobox(duration, textvariable=self._speed, values=("1", "2", "4"), width=3, state="disabled")
        self._speed_combo.pack(side="left", padx=6)
        self._speed_combo.bind("<<ComboboxSelected>>", lambda event: self.change_speed())

        controller = ttk.Frame(outer)
        controller.pack()
        for label, row, column in [
            ("Up", 0, 1), ("Left", 1, 0), ("Down", 1, 1), ("Right", 1, 2),
            ("B", 1, 4), ("A", 0, 5), ("Start", 2, 4), ("Select", 2, 5),
            ("L", 0, 0), ("R", 0, 2),
        ]:
            self._button(controller, label.upper(), lambda name=label: self.press([name])).grid(
                row=row, column=column, padx=4, pady=4, sticky="ew",
            )

        actions = ttk.Frame(outer)
        actions.pack(pady=12)
        self._button(actions, "Advance without input", lambda: self.press([])).pack(side="left", padx=4)
        self._button(actions, "Release buttons", self.release).pack(side="left", padx=4)
        saves = ttk.Frame(outer)
        saves.pack()
        self._button(saves, "Save state…", self.save_state).pack(side="left", padx=4)
        self._button(saves, "Restore state…", self.load_state).pack(side="left", padx=4)
        ttk.Label(outer, textvariable=self._status, wraplength=580).pack(anchor="w", pady=(12, 0))
        self._update_controls()
        self.root.after(PREVIEW_INTERVAL_MS, self._refresh_preview)

    def _show_frame(self, frame):
        self._image = tk.PhotoImage(master=self.root, data=frame, format="png").zoom(2)
        self._screen.configure(image=self._image, text="")

    def _refresh_preview(self):
        if self._closing:
            return
        if self._rom_loaded and self._session is not None:
            sequence, frame = self._session.snapshot
            if frame and sequence != self._preview_sequence:
                self._show_frame(frame)
                self._preview_sequence = sequence
            self._playback_button.configure(text="Pause" if self._session.playing else "Play")
            if self._session.error:
                self._status.set(self._session.error)
        self.root.after(PREVIEW_INTERVAL_MS, self._refresh_preview)

    def _button(self, parent, text, command, needs_rom=True):
        button = ttk.Button(parent, text=text, command=command)
        self._controls.append((button, needs_rom))
        return button

    def _update_controls(self):
        for button, needs_rom in self._controls:
            enabled = not self._closing and self._pending is None and (self._rom_loaded or not needs_rom)
            button.configure(state="normal" if enabled else "disabled")
        enabled = not self._closing and self._pending is None and self._rom_loaded
        self._speed_combo.configure(state="readonly" if enabled else "disabled")

    def _submit(self, operation: Callable[[], bytes | None], status: str, on_success=None):
        if self._pending is not None or self._closing:
            return
        self._status.set("Working…")
        self._pending = self._executor.submit(operation)
        self._update_controls()
        self.root.after(30, lambda: self._poll(status, on_success))

    def _poll(self, status, on_success):
        if not self._pending.done():
            self.root.after(30, lambda: self._poll(status, on_success))
            return
        future, self._pending = self._pending, None
        if self._closing:
            self._destroy()
            return
        try:
            frame = future.result()
            if on_success is not None:
                on_success()
            if frame is not None:
                self._show_frame(frame)
            self._status.set(status)
        except Exception as exc:
            self._status.set("Operation failed. See error details; commands are not retried automatically.")
            messagebox.showerror("SkyEmu error", str(exc), parent=self.root)
        self._update_controls()

    def load_rom(self):
        path = filedialog.askopenfilename(
            parent=self.root, title="Choose Pokémon Emerald ROM", initialdir=PROJECT_ROOT,
            filetypes=[("Game Boy Advance ROM", "*.gba"), ("All files", "*.*")],
        )
        if not path:
            return
        speed = int(self._speed.get())

        def operation():
            if self._session is not None:
                self._session.close()
                self._session = None
            process = ensure_skyemu(self.executable, path, self.emulator.port, attach=self.attach)
            if process is not None:
                self._process = process
            self.emulator.load_rom(path)
            # Games can remain black during their first frame; allow time to boot.
            self.emulator.press_buttons([], frames=self.boot_frames)
            session = Playback(self.emulator)
            session.set_speed(speed)
            session.start()
            self._session = session
            return session.snapshot[1]

        def loaded():
            self._rom_loaded = True
            self._rom_name.set(Path(path).name)
            self._preview_sequence = -1

        # A failed ROM switch must not leave controls enabled for the previous ROM.
        self._rom_loaded = False
        self._rom_name.set("Loading ROM…")
        self._image = None
        self._screen.configure(image="", text="Game screen")
        self._submit(operation, "ROM loaded. Live playback is running.", loaded)

    def capture(self):
        self._submit(self._session.get_frame, "Screen captured.")

    def toggle_playback(self):
        playing = not self._session.playing
        self._submit(lambda: self._session.set_playing(playing),
                     "Live playback resumed." if playing else "Paused. Buttons advance the selected frames.")

    def change_speed(self):
        speed = int(self._speed.get())
        self._submit(lambda: self._session.set_speed(speed), f"Emulation speed set to {speed}x.")

    def press(self, buttons):
        try:
            frames = int(self._frames.get())
            if not 1 <= frames <= 3600:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid duration", "Enter a whole number from 1 to 3600 frames.", parent=self.root)
            return

        def operation():
            self._session.press_buttons(buttons, frames=frames)

        label = "+".join(buttons) if buttons else "No input"
        self._submit(operation, f"{label}: queued {frames} frames.")

    def release(self):
        self._submit(self._session.release_buttons, "Buttons released.")

    def save_state(self):
        directory = PROJECT_ROOT / "data" / "sessions"
        directory.mkdir(parents=True, exist_ok=True)
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Save emulator state", initialdir=directory,
            initialfile="emerald-checkpoint.png", defaultextension=".png",
            filetypes=[("SkyEmu save state", "*.png")],
        )
        if path:
            self._submit(lambda: self._session.save_state(path), f"State saved: {Path(path).name}")

    def load_state(self):
        directory = PROJECT_ROOT / "data" / "sessions"
        path = filedialog.askopenfilename(
            parent=self.root, title="Restore emulator state for the loaded ROM",
            initialdir=directory if directory.exists() else PROJECT_ROOT,
            filetypes=[("SkyEmu save state", "*.png")],
        )
        if not path:
            return

        def operation():
            self._session.load_state(path)

        self._submit(operation, f"State restored: {Path(path).name}")

    def close(self):
        self._closing = True
        self._update_controls()
        if self._pending is not None:
            self._status.set("Finishing the current operation before closing…")
            self._update_controls()
        else:
            self._destroy()

    def _destroy(self):
        if self._shutdown_future is not None:
            return
        self._shutdown_future = self._executor.submit(self._shutdown)
        self.root.after(30, self._finish_close)

    def _shutdown(self):
        try:
            if self._session is not None:
                self._session.close()
        finally:
            if self._process is not None:
                stop_skyemu(self._process)

    def _finish_close(self):
        if not self._shutdown_future.done():
            self.root.after(30, self._finish_close)
            return
        self._executor.shutdown(wait=False)
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="Desktop GBA controller with automatic SkyEmu startup and live playback.")
    parser.add_argument("--port", type=int, default=8081, help="SkyEmu HTTP port (default: 8081; web launcher uses 8080)")
    parser.add_argument("--skyemu", type=Path, default=DEFAULT_EXECUTABLE, help="SkyEmu executable")
    parser.add_argument("--attach", action="store_true", help="Require an existing SkyEmu instead of starting one")
    parser.add_argument("--boot-frames", type=int, default=300, help="Frames to run after loading (default: 300)")
    args = parser.parse_args()
    if not 1 <= args.boot_frames <= 3600:
        parser.error("--boot-frames must be between 1 and 3600")
    emulator = SkyEmu(port=args.port)
    root = tk.Tk()
    EmulatorApp(root, emulator, boot_frames=args.boot_frames, executable=args.skyemu, attach=args.attach)
    root.mainloop()


if __name__ == "__main__":
    main()
