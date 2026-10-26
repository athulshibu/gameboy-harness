"""Shared emulation and display timing. Restart controllers after changes."""

FRAMES_PER_SECOND = 60
FRAMES_PER_CLICK = 15

# Capture one image per batch, keeping video bandwidth below the emulation rate.
FRAMES_PER_UPDATE = 2
FRAME_UPDATE_INTERVAL_SECONDS = FRAMES_PER_UPDATE / FRAMES_PER_SECOND
PREVIEW_INTERVAL_MS = max(1, round(1000 * FRAME_UPDATE_INTERVAL_SECONDS))
