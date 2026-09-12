"""Typed errors for the operational-data boundary."""


class DataError(Exception):
    """Base error for operational data operations."""


class EventValidationError(DataError):
    """A source record does not satisfy the normalized event contract."""


class EventConflictError(DataError):
    """An immutable identity was reused with different content."""


class SnapshotConflictError(DataError):
    """A snapshot identity was reused with different content."""


class DataFormatError(DataError):
    """An input document is not supported JSON or JSONL."""
