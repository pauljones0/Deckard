
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk

class ColorButtonRow(Adw.ActionRow):
    """Row with an RGBA color button."""
    def __init__(self,
                 title: str | None = None,
                 subtitle: str | None = None,
                 default_color: tuple[int, int, int, int] = (0, 0, 0, 255),
                 ):
        super().__init__(title=title, subtitle=subtitle)  # ty: ignore[invalid-argument-type]  # gi stub: Adw string props accept None (PyGObject maps it to NULL, i.e. empty string)
        self.color_button = Gtk.ColorButton(valign=Gtk.Align.CENTER)

        self.add_suffix(self.color_button)

        self.color = default_color

    @property
    def color(self) -> tuple[int, int, int, int]:
        rgba = self.color_button.get_rgba()
        return self.convert_from_rgba(rgba)

    @color.setter
    def color(self, value: tuple[int, int, int, int]) -> None:
        rgba = self.convert_to_rgba(value)
        self.color_button.set_rgba(rgba)

        self.color_button.emit("color-set")

    def convert_from_rgba(self, color: Gdk.RGBA) -> tuple[int, int, int, int]:
        components = (color.red, color.green, color.blue, color.alpha)

        return self.normalize_to_255(components)

    def convert_to_rgba(self, color: tuple[int, int, int, int]) -> Gdk.RGBA:
        normalized = self.normalize_to_1(color)

        rgba = Gdk.RGBA()

        rgba.red = normalized[0]
        rgba.green = normalized[1]
        rgba.blue = normalized[2]
        rgba.alpha = normalized[3]

        return rgba

    def normalize_to_255(self, color: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
        red, green, blue, alpha = color
        return (round(red * 255), round(green * 255), round(blue * 255), round(alpha * 255))

    def normalize_to_1(self, color: tuple[int, int, int, int]) -> tuple[float, float, float, float]:
        red, green, blue, alpha = color
        return (red / 255.0, green / 255.0, blue / 255.0, alpha / 255.0)
