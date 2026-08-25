from enum import StrEnum


class LabelPosition(StrEnum):
    """The three label slots on a key, and the keys they take in a page json.

    StrEnum members are real strings, so a member reads and compares as its
    value: a page file that stores "top" still matches LabelPosition.TOP, and
    a member writes back as its plain string. The values are frozen because a
    rename would orphan every stored label.
    """
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
