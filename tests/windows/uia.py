"""
What the Windows tests do in place of a user: click Allow in Chrome's remote
debugging prompt, and take screenshots. Needs pywinauto and Pillow.
"""

from __future__ import annotations

import threading
from pathlib import Path


def note(msg: str) -> None:
    print(f"[windows] {msg}", flush=True)


class AllowClicker:
    """
    Click Allow in Chrome's "Allow remote debugging?" prompt whenever it
    appears, through UI Automation's Invoke pattern, until stopped.
    """

    def __init__(self, shots: Path | None = None) -> None:
        self.clicks = 0
        self.shots = shots
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "AllowClicker":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(10)

    def _run(self) -> None:
        from pywinauto import Desktop
        while not self._stop.is_set():
            try:
                for window in Desktop(backend="uia").windows():
                    if not window.element_info.class_name.startswith("Chrome_WidgetWin"):
                        continue
                    for button in window.descendants(control_type="Button", title="Allow"):
                        if self.shots is not None:
                            screenshot(self.shots / f"consent-{self.clicks + 1}.png")
                        button.invoke()
                        self.clicks += 1
                        note("clicked Allow in Chrome's remote debugging prompt")
            except Exception as exc:  # windows come and go while they are listed
                note(f"looking for the prompt: {type(exc).__name__}: {exc}")
            self._stop.wait(1)


def screenshot(path: Path) -> None:
    from PIL import ImageGrab
    path.parent.mkdir(parents=True, exist_ok=True)
    ImageGrab.grab(all_screens=True).save(path)
    note(f"screenshot {path.name}")
