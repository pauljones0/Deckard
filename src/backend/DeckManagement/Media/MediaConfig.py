from dataclasses import dataclass
from typing import Any

from src.backend.DeckManagement.media_loop import MEDIA_LOOP_FPS


@dataclass(slots=True)
class MediaConfig:
    """The media configuration extracted from a page or state dictionary."""
    path: str | None = None
    loop: bool = True
    fps: int = MEDIA_LOOP_FPS
    fill_mode: str | None = None
    size: float | None = None
    valign: float | None = None
    halign: float | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MediaConfig":
        """Create media configuration from page JSON kebab-case keys."""
        return cls(
            path=d.get("path"),
            loop=d.get("loop", True),
            fps=d.get("fps", MEDIA_LOOP_FPS),
            fill_mode=d.get("fill-mode"),
            size=d.get("size"),
            valign=d.get("valign"),
            halign=d.get("halign"),
        )
