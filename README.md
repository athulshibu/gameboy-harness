# Universal AI GBA Gaming Agent

## Local web control panel

From the project root, specify your own GBA ROM:

```powershell
python tests/web_control.py "C:\path\to\Pokemon Emerald.gba"
```

This manual harness launches the bundled `SkyEmu-v5-Windows.exe` in HTTP server
mode, loads the ROM, and serves the panel at **http://localhost:9000**. Open that
address in your browser. SkyEmu runs in the background; its game screen appears
in the panel. No Python dependencies or frontend build are required.

The panel streams live video continuously, with Play/Pause and **1×, 2×, 4×**
speed controls. The shared playback clock targets about 60 game frames/s at 1×
and up to 30 video updates/s. Faster speeds advance more game frames per update;
achieved speed depends on your CPU. Multiple browser tabs share the same clock.
The stream currently has no audio.

Emulation timing is defined in `backend/emulator/constants.py`. Change
`FRAMES_PER_SECOND` there and restart the controllers; playback, web streaming,
and desktop preview timing all derive from that value.
`FRAMES_PER_CLICK` sets the initial **Frames per press** value in both apps;
you can still override it in the controls while playing.

Directional buttons, A, B, START, and SELECT are held for the selected number of
emulated frames, with intermediate frames streamed as the action runs. Inputs
are queued and released automatically. Pause cancels queued and held inputs;
while paused, button actions run their chosen frames and pause again. At 1×,
30 frames is about half a second; at 2× a quarter second; at 4× an eighth second.
**Save screenshot** downloads a PNG without interrupting the stream.

The launcher runs 300 boot frames before starting playback. Change this with
`--boot-frames 600`.
Save state overwrites `data/sessions/<ROM filename without extension>/web-checkpoint.png`;
restore loads that checkpoint for the same ROM.

Use **Ctrl+C** in the terminal to stop both the panel and the SkyEmu process
started by the harness. Close any other SkyEmu instance using port 8080 first,
or select different ports:

```powershell
python tests/web_control.py "roms/game.gba" --skyemu "C:\tools\SkyEmu.exe" --emulator-port 8081 --port 9000
```

If you already opened SkyEmu and enabled its HTTP server, attach the web panel
to that instance with `python tests/web_control.py "Pokemon_Emerald.gba" --attach`.
Attach mode leaves SkyEmu open when the panel stops. Use one controller per
emulator instance. Use **Wait** to advance an intro while paused.

## Desktop controller

Run this command from the project root:

```powershell
python -m scripts.emulator_app
```

Requires Python 3.10+ with Tkinter (included in standard Windows Python
installations); no pip packages are needed. Click **Load ROM…** and select a
`.gba` file. The app automatically starts the bundled SkyEmu on **port 8081**
and displays live playback. It works independently of the web app, which uses
emulator port 8080. Closing the desktop app stops only the emulator it started.

Use `--skyemu "C:\tools\SkyEmu.exe"` for another executable or `--port 8082`
for another port. To require an existing emulator instead of starting one, use
`--attach --port 8080`; keep that instance running while using the controller.

Click **Load ROM…** and choose your Pokémon Emerald `.gba` file. No ROM is
included. The window displays the screen at 2× size. Set **Frames per press**
(1–3600), then click A, B, START, or a direction. Each click queues an action
for that many game frames and releases the buttons afterward. **Speed** selects
1×, 2×, or 4×. **Pause/Play** controls continuous gameplay.
**Advance without input** advances time with no buttons held; **Capture
screen** refreshes without advancing time. Loading a ROM advances 300 initial
frames to get past the black boot screen (configurable with `--boot-frames`).
Playback starts automatically after loading.

**Save state…** writes a SkyEmu state PNG; **Restore state…** loads it and
refreshes the display. Load the matching ROM before restoring a state. These
are emulator checkpoints; you can also use the game's own save menu through
the button controls.

## SkyEmu adapter

Requires Python 3.10 or newer. No third-party Python packages are required.
Start SkyEmu, enable **Enable HTTP Control Server** in its advanced settings,
set its port to **8080**, and disable Hardcore Mode. Leave SkyEmu running.

Alternatively, start the bundled Windows executable in HTTP server mode from
PowerShell, replacing the ROM path with a file you own:

```powershell
.\SkyEmu-v5-Windows.exe http_server 8080 "C:\path\to\game.gba"
```

Run Python from the project root:

```python
from pathlib import Path
from backend.emulator import SkyEmu

emulator = SkyEmu(port=8080)
emulator.load_rom("roms/game.gba")
emulator.press_buttons("Start", frames=1)
emulator.press_buttons(["Right", "A"], frames=60)
emulator.press_buttons([], frames=30)  # Advance without buttons.

Path("data/screenshots").mkdir(parents=True, exist_ok=True)
Path("data/screenshots/frame.png").write_bytes(emulator.get_frame())

emulator.save_state("data/sessions/checkpoint.png")
emulator.load_state("data/sessions/checkpoint.png")
emulator.release_buttons()
```

`get_frame()` returns PNG bytes. Button names are case-insensitive: A, B, Up,
Down, Left, Right, L, R, Start, Select. Each action holds the selected buttons
for the requested positive number of frames, then clears the HTTP inputs.
Gameplay remains paused between actions. Avoid manually resuming the emulator
or controlling it from another client during an automated session.

Paths are resolved locally: SkyEmu must run on the same machine. Save states use
`.png` files with embedded emulator state; their parent directories are created
automatically. Connection failures and rejected commands raise `SkyEmuError`.
Commands are not automatically retried because a timeout may occur after an
action has executed. Button release is attempted after a failed action, but
cannot be guaranteed if the server is unreachable.

API reference: [SkyEmu v5 HTTP Control Server](https://github.com/skylersaleh/SkyEmu/blob/v5/docs/HTTP_CONTROL_SERVER.md).

Run the HTTP adapter tests (no ROM or running SkyEmu required):

```powershell
python -m unittest discover -s tests -v
```
