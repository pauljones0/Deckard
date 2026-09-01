from enum import StrEnum


class LabelPosition(StrEnum):
    """The three persisted label-slot keys.
    StrEnum preserves string comparison and serialization; renaming values orphans stored labels."""
    TOP = "top"
    CENTER = "center"
    BOTTOM = "bottom"


class Alignment(StrEnum):
    """Horizontal text alignment inside a label. The values match what
    Pillow's text drawing expects for its align argument."""
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"


class FillMode(StrEnum):
    """How a foreground image fills its tile. The values are the strings a
    page json stores under fill-mode."""
    STRETCH = "stretch"
    COVER = "cover"
    CONTAIN = "contain"
