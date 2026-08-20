import enum


class ActionInputSupportStatus:
    def __init__(self, num: int) -> None:
        self.num = num

    def __int__(self) -> int:
        return self.num


class ActionInputSupport(enum.Enum):
    # A class that defines __eq__ loses the inherited hash unless it
    # restates it; without this the members cannot key a dict or a set.
    __hash__ = enum.Enum.__hash__

    UNSUPPORTED = ActionInputSupportStatus(0)
    UNTESTED = ActionInputSupportStatus(1)
    SUPPORTED = ActionInputSupportStatus(2)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, ActionInputSupport):
            # NotImplemented, not a raised or returned TypeError: Python
            # then raises for an ordering and answers False for equality.
            return NotImplemented
        return int(self.value) < int(other.value)
    
    def __gt__(self, other: object) -> bool:
        if not isinstance(other, ActionInputSupport):
            # NotImplemented, not a raised or returned TypeError: Python
            # then raises for an ordering and answers False for equality.
            return NotImplemented
        return int(self.value) > int(other.value)
    
    def __le__(self, other: object) -> bool:
        if not isinstance(other, ActionInputSupport):
            # NotImplemented, not a raised or returned TypeError: Python
            # then raises for an ordering and answers False for equality.
            return NotImplemented
        return int(self.value) <= int(other.value)
    
    def __ge__(self, other: object) -> bool:
        if not isinstance(other, ActionInputSupport):
            # NotImplemented, not a raised or returned TypeError: Python
            # then raises for an ordering and answers False for equality.
            return NotImplemented
        return int(self.value) >= int(other.value)
    
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ActionInputSupport):
            # NotImplemented, not a raised or returned TypeError: Python
            # then raises for an ordering and answers False for equality.
            return NotImplemented
        return int(self.value) == int(other.value)