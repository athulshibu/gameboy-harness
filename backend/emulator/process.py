"""Start and stop only the SkyEmu process owned by a controller."""

from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener

from .skyemu import SkyEmu, SkyEmuError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EXECUTABLE = PROJECT_ROOT / "SkyEmu-v5-Windows.exe"


def port_available(port):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


def start_skyemu(executable: Path, rom: Path, port: int, timeout: float = 15):
    port_available(port)
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    process = subprocess.Popen(
        [str(executable), "http_server", str(port), str(rom)], cwd=PROJECT_ROOT, **options,
    )
    http = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"SkyEmu exited during startup (code {process.returncode}).")
            try:
                with http.open(f"http://127.0.0.1:{port}/ping", timeout=0.5) as response:
                    if response.read().strip(b"\x00 \r\n\t") == b"pong":
                        return process
            except (URLError, OSError) as exc:
                if isinstance(exc, HTTPError):
                    exc.close()
            time.sleep(0.1)
        raise RuntimeError(f"SkyEmu did not start its HTTP server on port {port} within {timeout}s.")
    except BaseException:
        stop_skyemu(process)
        raise


def ensure_skyemu(executable, rom, port, attach=False):
    """Connect to an existing server, or launch one and return its process."""
    try:
        SkyEmu(port=port, timeout=1).check_connection()
        return None
    except SkyEmuError:
        if attach:
            raise
    executable = Path(executable).expanduser().resolve()
    if not executable.is_file():
        raise FileNotFoundError(f"SkyEmu executable not found: {executable}. Use --skyemu PATH.")
    return start_skyemu(executable, Path(rom).resolve(), port)


def stop_skyemu(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
