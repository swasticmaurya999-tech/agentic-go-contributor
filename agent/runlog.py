"""Run logger.

Two channels:
- ``step``   : user-facing progress lines (printed to the console AND recorded).
- ``detail`` : verbose lines (recorded only, for run.log / debugging).

Everything is kept in ``lines`` so a full transcript can be written to run.log later.
"""
from __future__ import annotations


class RunLog:
    def __init__(self, console: bool = True):
        self.lines: list[str] = []
        self._console = None
        if console:
            try:
                from rich.console import Console

                self._console = Console()
            except Exception:
                self._console = None

    def _emit(self, text: str):
        try:
            if self._console is not None:
                self._console.print(text, highlight=False, soft_wrap=True)
            else:
                print(text)
        except UnicodeEncodeError:
            # Windows legacy consoles (cp1252) can't encode some chars (e.g. emoji
            # or en-dashes the model emits). Degrade gracefully instead of crashing.
            safe = text.encode("ascii", "replace").decode("ascii")
            try:
                print(safe)
            except Exception:
                pass

    def step(self, msg):
        """User-facing progress line (printed + recorded)."""
        text = str(msg)
        self.lines.append(text)
        self._emit(text)

    def detail(self, msg):
        """Verbose line for the transcript only (recorded, not printed)."""
        self.lines.append(str(msg))

    # Calling the logger directly prints a step (backwards-compatible).
    def __call__(self, msg):
        self.step(msg)

    def dump(self) -> str:
        return "\n".join(self.lines)
