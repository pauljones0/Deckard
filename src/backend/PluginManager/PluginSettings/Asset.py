from os.path import isfile

from PIL import Image

from typing import Any

from src.backend.DeckManagement.Media.Media import Media

class Asset:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def get_values(self) -> Any:
        pass

    def to_json(self) -> Any:
        pass

    @classmethod
    def from_json(cls, *args: Any, **kwargs: Any) -> "Asset | None":
        return None

class Color(Asset):
    def __init__(self, color: tuple[int, int, int, int], *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._color: tuple[int, int, int, int] = color

    def get_values(self) -> tuple[int, int, int, int]:
        return self._color

    def to_json(self) -> "list[int]":
        return list(self._color)

    @classmethod
    def from_json(cls, *args: Any, **kwargs: Any) -> "Color":
        return cls(color=tuple(args[0]))

class Icon(Asset):
    def __init__(self, path: str, size: float = 1.0, valign: float = 0.0, halign: float = 0.0, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # All three stay None while the file is absent. A custom icon the user
        # deleted keeps its entry in the plugin's settings JSON, so these three
        # are optional and not late-initialized.
        self._path: str | None
        self._icon: Media | None
        self._rendered: Image.Image | None
        if isfile(path):
            self._path = path
            self._icon = Media.from_path(path, size=size, valign=valign, halign=halign)
            self._rendered = self._icon.get_final_media()
        else:
            self._path = None
            self._icon = None
            self._rendered = None

    def get_values(self) -> "tuple[Media | None, Image.Image | None]":
        return self._icon, self._rendered

    def to_json(self) -> "dict[str, Any]":
        icon = self._icon
        save_data = {
            "path": self._path,
            "size": icon.size if icon is not None else None,
            "halign": icon.halign if icon is not None else None,
            "valign": icon.valign if icon is not None else None,
        }
        return save_data

    @classmethod
    def from_json(cls, *args: Any, **kwargs: Any) -> "Icon":
        save_data: dict[str, Any] = args[0]

        # `or` and not a .get default: a deleted custom icon persists as a
        # null path, and a null reaching isfile() raises TypeError, which
        # fails the whole plugin's asset load on every later launch.
        path = save_data.get("path") or ""
        size = save_data.get("size") or 1.0
        halign = save_data.get("halign") or 0.00
        valign = save_data.get("valign") or 0.00

        return cls(path, size, halign, valign)