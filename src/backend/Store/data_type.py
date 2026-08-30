from enum import StrEnum


class DataType(StrEnum):
    """Remote decoding mode and cache-key discriminator."""
    TEXT = "text"
    CONTENT = "content"
