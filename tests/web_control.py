"""Manual harness: python tests/web_control.py path/to/game.gba."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from threading import Event, RLock
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.emulator import SkyEmu, SkyEmuError
from backend.emulator.constants import FRAMES_PER_CLICK, FRAME_UPDATE_INTERVAL_SECONDS
from backend.emulator.playback import Playback
from backend.emulator.process import start_skyemu, stop_skyemu


class ControlServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, emulator, rom: Path, checkpoint: Path):
        self.emulator = emulator
        self.rom = rom
        self.checkpoint = checkpoint
        self.last_command = "None"
        self.command_lock = RLock()
        self.playback = Playback(emulator)
        self.stopping = Event()
        self.html = (
            Path(__file__).with_suffix(".html").read_text(encoding="utf-8")
            .replace("{{FRAMES_PER_CLICK}}", str(FRAMES_PER_CLICK))
            .encode("utf-8")
        )
        super().__init__(address, ControlHandler)

    def server_close(self):
        self.stopping.set()
        self.playback.close()
        super().server_close()


class ControlHandler(BaseHTTPRequestHandler):
    def _send(self, status, body, content_type="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _local_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in (f"localhost:{port}", f"127.0.0.1:{port}")

    def do_GET(self):
        if not self._local_host():
            self._send(403, {"error": "Use the localhost control panel address."})
            return
        path = self.path.split("?", 1)[0]
        try:
            if path == "/":
                self._send(200, self.server.html, "text/html; charset=utf-8")
            elif path == "/control.js":
                self._send(200, Path(__file__).with_suffix(".js").read_bytes(), "text/javascript; charset=utf-8")
            elif path == "/api/status":
                with self.server.command_lock:
                    self._send(200, {"rom": self.server.rom.name, "last_command": self.server.last_command,
                                     **self.server.playback.status()})
            elif path == "/screen":
                with self.server.command_lock:
                    frame = self.server.playback.get_frame()
                self._send(200, frame, "image/png")
            elif path == "/stream":
                self._stream()
            else:
                self._send(404, {"error": "Not found"})
        except (SkyEmuError, OSError) as exc:
            self._send(502, {"error": str(exc)})

    def _stream(self):
        self.connection.settimeout(5)
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        sequence = -1
        last_sent = 0
        try:
            while not self.server.stopping.is_set():
                current, frame = self.server.playback.snapshot
                if frame and (current != sequence or time.monotonic() - last_sent >= 1):
                    self.wfile.write(b"--frame\r\nContent-Type: image/png\r\nContent-Length: "
                                     + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                    self.wfile.flush()
                    sequence = current
                    last_sent = time.monotonic()
                self.server.stopping.wait(FRAME_UPDATE_INTERVAL_SECONDS)
        except OSError:
            pass

    def do_POST(self):
        # A custom header prevents another website from sending browser form commands.
        if not self._local_host() or self.headers.get("X-Control-Panel") != "1":
            self._send(403, {"error": "Send commands from the local control panel."})
            return
        if self.headers.get_content_type() != "application/json":
            self._send(415, {"error": "Expected application/json"})
            return
        if self.path not in ("/api/action", "/api/save", "/api/load", "/api/playback", "/api/speed"):
            self._send(404, {"error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 4096:
                raise ValueError("Request body must contain 1–4096 bytes")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            with self.server.command_lock:
                if self.path == "/api/action":
                    buttons, frames = payload.get("buttons"), payload.get("frames")
                    if not isinstance(buttons, list) or any(
                        not isinstance(button, str) or button.upper() not in {name.upper() for name in SkyEmu.BUTTONS}
                        for button in buttons
                    ):
                        raise ValueError("buttons must be a list of GBA button names")
                    if type(frames) is not int or not 1 <= frames <= 3600:
                        raise ValueError("frames must be a whole number from 1 to 3600")
                    self.server.playback.press_buttons(buttons, frames=frames)
                    command = f"{' + '.join(button.upper() for button in buttons) or 'WAIT'} · {frames} frames"
                elif self.path == "/api/save":
                    self.server.playback.save_state(self.server.checkpoint)
                    command = "SAVE STATE"
                elif self.path == "/api/load":
                    self.server.playback.load_state(self.server.checkpoint)
                    command = "RESTORE STATE"
                elif self.path == "/api/playback":
                    playing = payload.get("playing")
                    self.server.playback.set_playing(playing)
                    command = "PLAY" if playing else "PAUSE"
                else:
                    speed = payload.get("speed")
                    self.server.playback.set_speed(speed)
                    command = f"SPEED {speed}x"
                self.server.last_command = command
            self._send(200, {"last_command": command})
        except (ValueError, UnicodeError) as exc:
            self._send(400, {"error": str(exc)})
        except FileNotFoundError:
            self._send(400, {"error": "No checkpoint yet. Save a state first."})
        except (RuntimeError, OSError) as exc:
            self._send(502, {"error": str(exc)})


def main():
    parser = argparse.ArgumentParser(description="Load a GBA ROM in SkyEmu and serve a local web controller.")
    parser.add_argument("rom", type=Path, help="Path to your .gba ROM")
    parser.add_argument("--skyemu", type=Path, default=PROJECT_ROOT / "SkyEmu-v5-Windows.exe")
    parser.add_argument("--port", type=int, default=9000, help="Web control panel port (default: 9000)")
    parser.add_argument("--emulator-port", type=int, default=8080, help="SkyEmu HTTP port (default: 8080)")
    parser.add_argument("--attach", action="store_true", help="Use an already running SkyEmu; leave it open on exit")
    parser.add_argument("--boot-frames", type=int, default=300, help="Frames to run after loading (default: 300)")
    args = parser.parse_args()
    rom, executable = args.rom.expanduser().resolve(), args.skyemu.expanduser().resolve()
    if not rom.is_file() or rom.suffix.lower() != ".gba":
        parser.error(f"Specify an existing .gba file: {rom}")
    if not args.attach and not executable.is_file():
        parser.error(f"SkyEmu executable not found: {executable}; use --skyemu PATH")
    if not all(1 <= port <= 65535 for port in (args.port, args.emulator_port)) or args.port == args.emulator_port:
        parser.error("Choose distinct web and emulator ports between 1 and 65535")
    if not 1 <= args.boot_frames <= 3600:
        parser.error("--boot-frames must be between 1 and 3600")

    process = None
    try:
        emulator = SkyEmu(port=args.emulator_port)
        checkpoint = PROJECT_ROOT / "data" / "sessions" / rom.stem / "web-checkpoint.png"
        # Reserve the web port before launching the emulator; fail cleanly if occupied.
        with ControlServer(("127.0.0.1", args.port), emulator, rom, checkpoint) as server:
            if not args.attach:
                try:
                    process = start_skyemu(executable, rom, args.emulator_port)
                except OSError as exc:
                    raise RuntimeError(
                        f"Could not launch SkyEmu on port {args.emulator_port}: {exc}. "
                        "If SkyEmu is already open with HTTP enabled, add --attach."
                    ) from exc
            emulator.check_connection()
            emulator.load_rom(str(rom))
            emulator.press_buttons([], frames=args.boot_frames)
            emulator.get_frame()  # Verify capture before announcing that the panel is ready.
            server.playback.start()
            print(f"ROM: {rom.name}", flush=True)
            print(f"Control panel: http://localhost:{args.port}", flush=True)
            print(f"Checkpoint: {checkpoint}", flush=True)
            print(f"Booted {args.boot_frames} frames; live playback is running.", flush=True)
            print("Press Ctrl+C to stop the panel" + (" (attached SkyEmu stays open)." if args.attach else " and its SkyEmu process."), flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping control panel…")
    except (OSError, RuntimeError) as exc:
        parser.exit(1, f"Error: {exc}\n")
    finally:
        if process is not None:
            stop_skyemu(process)


if __name__ == "__main__":
    main()
