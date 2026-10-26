"""Common interface for emulator implementations, including SkyEmu."""

from abc import ABC, abstractmethod


class Emulator(ABC):
    @abstractmethod
    def load_rom(self, path: str):
        pass

    @abstractmethod
    def get_frame(self):
        pass

    @abstractmethod
    def press_buttons(self, buttons, frames):
        pass

    @abstractmethod
    def release_buttons(self):
        pass

    @abstractmethod
    def save_state(self, path):
        pass

    @abstractmethod
    def load_state(self, path):
        pass
