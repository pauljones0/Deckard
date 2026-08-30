"""
Author: Core447
Year: 2026

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

---

Font resolution through fontconfig, in place of matplotlib.font_manager.

matplotlib's font_manager is a multi-megabyte import, and it runs a
synchronous system font scan on first use. fontconfig does the same job
better, because it is the system font database, and every font already goes
through it once through Pango and GTK. This module talks to fontconfig
directly through ctypes, and it falls back to the fc-match binary when the
shared library cannot load, e.g. on a minimal container image. Nothing here
imports matplotlib.

The weight scale mismatch is the biggest correctness risk here. Every weight
value that reaches fontconfig in this module goes through _ot_weight_to_fc
first.
"""
import ctypes
import ctypes.util
import functools
import subprocess
import threading

from fontTools.ttLib import TTFont
from typing import Any


# Exact FcWeightFromOpenTypeDouble anchors map OpenType/CSS 100-900 to fontconfig 0-215.
# Interpolate between them; raw OpenType values select the wrong fontconfig file.
_OT_TO_FC_WEIGHT = (
    (0, 0),
    (100, 0),
    (200, 40),
    (300, 50),
    (350, 55),
    (380, 75),
    (400, 80),
    (500, 100),
    (600, 180),
    (700, 200),
    (800, 205),
    (900, 210),
    (1000, 215),
)

# fontconfig FC_SLANT values.
_FC_SLANT_ROMAN = 0
_FC_SLANT_ITALIC = 100
_FC_SLANT_OBLIQUE = 110

FC_FAMILY = b"family"
FC_WEIGHT = b"weight"
FC_SLANT = b"slant"
FC_FILE = b"file"

_FC_MATCH_PATTERN = 0  # FcMatchKind.FcMatchPattern


def _ot_weight_to_fc(weight: int | None) -> int:
    """Map Pango/CSS weight 100-900 to fontconfig 0-215 by its interpolation table.
    Raw Pango/CSS values exceed fontconfig's scale and select the wrong file."""
    if weight is None:
        weight = 400
    weight = max(0, min(1000, weight))

    table = _OT_TO_FC_WEIGHT
    for (ot_lo, fc_lo), (ot_hi, fc_hi) in zip(table, table[1:]):
        if ot_lo <= weight <= ot_hi:
            span = ot_hi - ot_lo
            if span == 0:
                return fc_lo
            frac = (weight - ot_lo) / span
            return round(fc_lo + frac * (fc_hi - fc_lo))
    return table[-1][1]


def _style_to_fc_slant(style: str) -> int:
    if style == "italic":
        return _FC_SLANT_ITALIC
    if style == "oblique":
        return _FC_SLANT_OBLIQUE
    return _FC_SLANT_ROMAN


def _escape_fc_value(value: str) -> str:
    """Escape fontconfig pattern syntax for the fc-match subprocess fallback.
    The ctypes path sets fields directly and does not parse this string."""
    for ch in ("\\", ",", ":", "="):
        value = value.replace(ch, "\\" + ch)
    return value


class _FontConfig:
    """Lazily load the required fontconfig bindings and one process FcConfig.
    Serialize matches because label rendering is threaded and shared-config safety is unspecified."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # None means not loaded; _unavailable records permanent load failure.
        # c_void_p exposes the config as an integer address or None.
        self._lib: ctypes.CDLL | None = None
        self._config: int | None = None
        self._unavailable = False

    def _ensure_loaded(self) -> bool:
        if self._lib is not None:
            return True
        if self._unavailable:
            return False

        lib_name = ctypes.util.find_library("fontconfig")
        if not lib_name:
            self._unavailable = True
            return False

        try:
            lib = ctypes.CDLL(lib_name)

            lib.FcInitLoadConfigAndFonts.restype = ctypes.c_void_p
            lib.FcInitLoadConfigAndFonts.argtypes = []

            lib.FcPatternCreate.restype = ctypes.c_void_p
            lib.FcPatternCreate.argtypes = []

            lib.FcPatternAddString.restype = ctypes.c_int
            lib.FcPatternAddString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]

            lib.FcPatternAddInteger.restype = ctypes.c_int
            lib.FcPatternAddInteger.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]

            lib.FcConfigSubstitute.restype = ctypes.c_int
            lib.FcConfigSubstitute.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]

            lib.FcDefaultSubstitute.restype = None
            lib.FcDefaultSubstitute.argtypes = [ctypes.c_void_p]

            lib.FcFontMatch.restype = ctypes.c_void_p
            lib.FcFontMatch.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]

            lib.FcPatternGetString.restype = ctypes.c_int
            lib.FcPatternGetString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(ctypes.c_char_p)]

            lib.FcPatternDestroy.restype = None
            lib.FcPatternDestroy.argtypes = [ctypes.c_void_p]

            config = lib.FcInitLoadConfigAndFonts()
            if not config:
                self._unavailable = True
                return False

            self._lib = lib
            self._config = config
            return True
        except (OSError, AttributeError):
            self._unavailable = True
            return False

    def match(self, family: str, weight: int | None, style: str | None) -> dict[str, str | None] | None:
        """Return matched family and file fields, each optionally absent.
        Return None when fontconfig is unavailable so the caller uses fc-match."""
        with self._lock:
            if not self._ensure_loaded():
                return None

            lib = self._lib
            if lib is None:
                # Declare the defensive read so calls below narrow the loaded library.
                return None
            pattern = lib.FcPatternCreate()
            if not pattern:
                return None
            try:
                lib.FcPatternAddString(pattern, FC_FAMILY, family.encode("utf-8"))
                if weight is not None:
                    lib.FcPatternAddInteger(pattern, FC_WEIGHT, _ot_weight_to_fc(weight))
                if style is not None:
                    lib.FcPatternAddInteger(pattern, FC_SLANT, _style_to_fc_slant(style))

                lib.FcConfigSubstitute(self._config, pattern, _FC_MATCH_PATTERN)
                lib.FcDefaultSubstitute(pattern)

                result = ctypes.c_int(0)
                matched = lib.FcFontMatch(self._config, pattern, ctypes.byref(result))
                if not matched:
                    return None
                try:
                    return {
                        "family": self._get_string(matched, FC_FAMILY),
                        "file": self._get_string(matched, FC_FILE),
                    }
                finally:
                    lib.FcPatternDestroy(matched)
            finally:
                lib.FcPatternDestroy(pattern)

    def _get_string(self, pattern: Any, obj: bytes) -> str | None:
        lib = self._lib
        if lib is None:
            return None
        value = ctypes.c_char_p()
        res = lib.FcPatternGetString(pattern, obj, 0, ctypes.byref(value))
        if res != 0 or value.value is None:  # FcResultMatch == 0
            return None
        return value.value.decode("utf-8", errors="replace")


_fontconfig = _FontConfig()


def _match_via_subprocess(family: str, weight: int | None, style: str | None) -> dict[str, str | None] | None:
    """Run fc-match when the environment cannot load libfontconfig.
    The subprocess uses the same matcher as the ctypes path."""
    parts = [_escape_fc_value(family)]
    if weight is not None:
        parts.append(f"weight={_ot_weight_to_fc(weight)}")
    if style is not None:
        parts.append(f"slant={_style_to_fc_slant(style)}")
    pattern = ":".join(parts)

    try:
        proc = subprocess.run(
            ["fc-match", "-f", "%{family}|%{file}", pattern],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if proc.returncode != 0 or not proc.stdout:
        return None

    out = proc.stdout
    if "|" not in out:
        return {"family": None, "file": out or None}
    matched_family, _, matched_file = out.partition("|")
    return {"family": matched_family or None, "file": matched_file or None}


def _resolve_pattern(family: str, weight: int | None, style: str | None) -> dict[str, str | None] | None:
    result = _fontconfig.match(family, weight, style)
    if result is None:
        result = _match_via_subprocess(family, weight, style)
    return result


@functools.lru_cache(maxsize=256)
def resolve(family: str | None, weight: int | None = 400, style: str | None = "normal") -> str | None:
    """Resolve family, Pango/CSS weight, and normal, italic, or oblique style to a font file.
    Replace None with defaults and return None only when library and fc-match are unavailable."""
    if weight is None:
        weight = 400
    if style is None:
        style = "normal"
    if not family:
        family = "sans"

    result = _resolve_pattern(family, weight, style)
    if result is None:
        return None
    return result.get("file")


@functools.lru_cache(maxsize=1)
def fallback_font() -> str | None:
    """Resolve and cache the generic sans alias to a concrete family name."""
    result = _resolve_pattern("sans", None, None)
    if result is None:
        return "DejaVu Sans"
    return result.get("family") or "DejaVu Sans"


def font_name_from_path(font_path: str) -> str | None:
    """Read human family names from name IDs 1 and 16 with fontTools."""
    try:
        font = TTFont(font_path, fontNumber=0, lazy=True)
    except Exception:
        return None

    try:
        name_table = font["name"]
    except KeyError:
        return None

    # Maps a nameID to a (priority, family name) pair. See the priority
    # comment below.
    best: dict[int, tuple[tuple[int, int], str]] = {}
    for record in name_table.names:
        if record.nameID not in (1, 16):
            continue
        try:
            value = record.toUnicode()
        except Exception:
            continue
        if not value:
            continue
        # Prefer typographic family over font family, then Windows platform records.
        priority = (record.nameID, 1 if record.platformID == 3 else 0)
        if record.nameID not in best or priority > best[record.nameID][0]:
            best[record.nameID] = (priority, value)

    if 16 in best:
        return best[16][1]
    if 1 in best:
        return best[1][1]
    return None
