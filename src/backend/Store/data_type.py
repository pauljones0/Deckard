from enum import StrEnum


class DataType(StrEnum):
    """Whether a remote fetch reads a file as decoded text or as raw bytes.

    The value doubles as a cache-key field, so a text and a binary fetch of
    one path never collide on one cache file. StrEnum members are real
    strings, so the value flows into the cache string unchanged.
    """
    TEXT = "text"
    CONTENT = "content"
