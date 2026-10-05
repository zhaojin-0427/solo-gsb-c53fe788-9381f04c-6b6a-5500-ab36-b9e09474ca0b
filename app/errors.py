"""Domain errors mapped to HTTP status codes."""


class CalendarError(Exception):
    """Malformed or unsupported calendar content -> 422."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ConflictError(Exception):
    """Same UID + SEQUENCE but different semantic content -> 409."""

    def __init__(self, uid: str, rid: str, seq: int):
        self.uid = uid
        self.rid = rid
        self.seq = seq
        label = uid if rid == "" else f"{uid} (RECURRENCE-ID {rid})"
        super().__init__(
            f"conflicting content for {label} at SEQUENCE {seq}"
        )
